"""Tests for the per-user MCP token authentication middleware (Task 5).

The middleware resolves a presented token via ``UserStore.authenticate_token``
to exactly one account; a valid token passes, anything else (missing header,
unknown token, malformed Authorization, non-ASCII bytes, query-parameter
tokens) fails closed with 401. The web routes and /health stay exempt.
"""

import httpx
import pytest
from src.config import Settings
from src.main import create_app
from src.users import UserStore

SECRET = "test-session-secret"


def _make_settings(tmp_path) -> Settings:
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(tmp_path / "users.json"),
    )


@pytest.fixture
def store(tmp_path):
    """UserStore with two accounts; returns (store, alice_token, bob_token)."""
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    _, alice_token = store.create_user("alice", "alice-pw")
    _, bob_token = store.create_user("bob", "bob-pw")
    return store, alice_token, bob_token


@pytest.fixture
def app(tmp_path, store):
    return create_app(_make_settings(tmp_path))


@pytest.fixture
def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )


# -- exempt paths ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_exempt_from_auth(client):
    res = await client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["service"] == "anki-mcp"
    assert data["anki_connected"] is True
    assert data["auth_enabled"] is True
    assert data["status"] == "healthy"
    assert "maintenance_mode" in data
    # The payload schema is the compat contract: exactly these keys.
    assert set(data) == {"status", "service", "anki_connected", "maintenance_mode", "auth_enabled"}


@pytest.mark.asyncio
async def test_health_endpoint_degraded_when_data_dir_missing(tmp_path, store):
    """A missing/unwritable data dir cannot host collections -> 503 degraded."""
    settings = Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path / "does-not-exist"),
        users_file=str(tmp_path / "users.json"),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        res = await client.get("/health")
    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "degraded"
    # Same payload keys as the healthy response
    assert set(data) == {"status", "service", "anki_connected", "maintenance_mode", "auth_enabled"}
    assert data["anki_connected"] is True  # compat key stays true


@pytest.mark.asyncio
async def test_health_endpoint_degraded_when_user_store_dead(app, store, monkeypatch):
    """A dead user store breaks auth for every account -> 503 degraded."""
    def broken():
        raise ValueError("User store is unreadable or corrupt")

    # The health endpoint probes the same store instance the app exposes.
    monkeypatch.setattr(app.state.anki_store, "list_users", broken)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        res = await client.get("/health")
    assert res.status_code == 503
    assert res.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_web_routes_exempt_from_auth(client):
    # The login page renders without any token; the portal root redirects to
    # it (session-cookie auth owns the web surface, not the middleware).
    login_res = await client.get("/login")
    assert login_res.status_code == 200
    root_res = await client.get("/")
    assert root_res.status_code == 303
    assert root_res.headers["location"] == "/login"


# -- rejection paths ------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_unauthenticated_request_rejected(client):
    res = await client.post("/mcp", json={})
    assert res.status_code == 401
    assert "Missing authentication token" in res.json().get("error", "")


@pytest.mark.asyncio
async def test_mcp_invalid_token_rejected(client, store):
    res = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer wrong-token"},
        json={},
    )
    assert res.status_code == 401
    assert "Invalid authentication token" in res.json().get("error", "")


@pytest.mark.asyncio
async def test_mcp_token_of_deleted_user_rejected(tmp_path, store):
    users_file = str(tmp_path / "users.json")
    store, alice_token, _ = store
    store.delete_user("alice")
    app = create_app(_make_settings(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        res = await client.post("/mcp", headers={"Authorization": f"Bearer {alice_token}"}, json={})
        assert res.status_code == 401
        assert "Invalid authentication token" in res.json().get("error", "")


@pytest.mark.asyncio
async def test_mcp_query_param_token_rejected(client, store):
    _, alice_token, _ = store
    async with client as c:
        res = await c.post(f"/mcp?token={alice_token}", json={})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_mcp_non_ascii_token_rejected_without_500(client):
    async with client as c:
        # Send the token as raw UTF-8 bytes, as it would arrive on the wire;
        # httpx rejects non-ASCII str header values before sending.
        res = await c.post(
            "/mcp",
            headers={"Authorization": "Bearer tökén".encode("utf-8")},
            json={},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_mcp_malformed_authorization_header_rejected(client):
    async with client as c:
        for header in ("Bearer", "Token secure-token", "Basic dXNlcjpwYXNz"):
            res = await c.post("/mcp", headers={"Authorization": header}, json={})
            assert res.status_code == 401


# -- acceptance paths ------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_bearer_token_accepted(app, store):
    _, alice_token, _ = store
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            res = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {alice_token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            # Middleware allowed the request through, so it's not 401
            assert res.status_code != 401


@pytest.mark.asyncio
async def test_mcp_api_key_header_accepted(app, store):
    _, _, bob_token = store
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            res = await client.post(
                "/mcp",
                headers={"X-API-Key": bob_token},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert res.status_code != 401


@pytest.mark.asyncio
async def test_mcp_any_user_token_authenticates_per_user(app, store):
    """Each account's own token authenticates; tokens are per-user, not global."""
    _, alice_token, bob_token = store
    assert alice_token != bob_token
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            for token in (alice_token, bob_token):
                res = await client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                assert res.status_code != 401


@pytest.mark.asyncio
async def test_regenerated_token_replaces_old_one(app, store):
    """After regeneration the old token is dead and the new one authenticates."""
    _, alice_token, _ = store
    # Regenerate via the app's own store instance (UserStore is single-instance
    # per file; a sibling instance would leave the app's in-memory copy stale).
    new_token = app.state.anki_store.regenerate_token("alice")
    assert new_token != alice_token
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            old_res = await client.post(
                "/mcp", headers={"Authorization": f"Bearer {alice_token}"}, json={}
            )
            assert old_res.status_code == 401
            assert "Invalid authentication token" in old_res.json().get("error", "")
            new_res = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {new_token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert new_res.status_code != 401


# -- route/exempt parity (final review): lock the allowlist against drift ------------


@pytest.mark.asyncio
async def test_web_route_table_matches_auth_exempt_set(app):
    """Every registered web route must be in the middleware's exempt set.

    The allowlist fails CLOSED on unknown paths (401), so a new web route
    added without updating WEB_AUTH_PATHS would 401 the whole web UI — this
    parity test turns that silent drift into a loud failure. /mcp and
    /health must never be in the exempt set.
    """
    from starlette.routing import Route

    from src.auth import WEB_AUTH_PATHS

    web_paths = {
        r.path for r in app.routes if isinstance(r, Route) and r.path not in ("/mcp", "/health")
    }
    assert web_paths == set(WEB_AUTH_PATHS), (
        f"route/exempt drift: routes not exempt (would 401): "
        f"{sorted(web_paths - set(WEB_AUTH_PATHS))}; exempt entries with no route: "
        f"{sorted(set(WEB_AUTH_PATHS) - web_paths)}"
    )
    assert "/mcp" not in WEB_AUTH_PATHS
    assert "/health" not in WEB_AUTH_PATHS


# -- challenge injection (audit F-03) -----------------------------------------------


@pytest.mark.asyncio
async def test_mcp_challenge_not_injectable_via_host_header(client):
    """A crafted Host header cannot break out of the quoted resource_metadata.

    The hostile value must collapse to the localhost fallback, leaving exactly
    one well-formed quoted attribute in the WWW-Authenticate challenge.
    """
    async with client as c:
        res = await c.post(
            "/mcp",
            headers={"Host": 'evil.com", error="invalid_token'},
            json={},
        )
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://localhost/.well-known/oauth-protected-resource"'
    )
