"""Audit F-04 regression tests: /mcp session ids are bound to their owner.

Covers ``docker/mcp-service/src/main.py::BoundedSessionManager`` (delegated to
by ``AuthMiddleware`` in ``src/auth.py``):

- a session id created by one account is refused with 403 (session untouched)
  when presented by another account — no cross-user session theft;
- a same-account session whose token hash has since changed is closed and
  answered 401, so no transport outlives the token that created it;
- a per-user cap (``MCP_MAX_SESSIONS_PER_USER``, default 16, clamped to
  ``1..max_sessions``) evicts the account's OWN least-recently-used session
  instead of letting one account pin the global session pool;
- token regeneration, password reset and account deletion (portal and admin
  routes) drop every session the account owned.
"""

import httpx
import pytest

from src.config import Settings
from src.main import BoundedSessionManager, create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_csrf

from test_audit_fixes_b import ALICE_PASSWORD, SECRET, CSRF_RE, login_via_form


def _make_settings(tmp_path, **overrides) -> Settings:
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    store.create_user("alice", ALICE_PASSWORD)
    store.create_user("bob", "bob-pw")
    store.create_user("root", "root-pw", role="admin")
    values = {
        "session_secret": SECRET,
        "data_dir": str(tmp_path),
        "users_file": users_file,
    }
    values.update(overrides)
    return Settings(**values)


def _bearer(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }


async def _init_session(client: httpx.AsyncClient, token: str, request_id: int = 1) -> str:
    """Initialize handshake for ``token``; returns the new session id."""
    res = await client.post(
        "/mcp",
        headers=_bearer(token),
        json={
            "jsonrpc": "2.0", "id": request_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    assert res.status_code == 200, res.text
    session_id = res.headers.get("mcp-session-id")
    assert session_id, "initialize must mint a session id"
    return session_id


def _make_app(tmp_path, **overrides):
    """Create the app; returns (app, session guard, app-owned user store)."""
    settings = _make_settings(tmp_path, **overrides)
    app = create_app(settings)
    return app, app.state.anki_session_guard, app.state.anki_store


# ---------------------------------------------------------------------------
# Cap configuration (constructor clamps MCP_MAX_SESSIONS_PER_USER)
# ---------------------------------------------------------------------------


class _FakeSDK:
    def __init__(self):
        self._server_instances = {}


def test_max_per_user_defaults_and_clamping(monkeypatch, tmp_path):
    store = UserStore(str(tmp_path / "u.json"))
    monkeypatch.delenv("MCP_MAX_SESSIONS_PER_USER", raising=False)
    assert BoundedSessionManager(_FakeSDK(), store, "/mcp", 64).max_per_user == 16
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "0")
    assert BoundedSessionManager(_FakeSDK(), store, "/mcp", 64).max_per_user == 1
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "-5")
    assert BoundedSessionManager(_FakeSDK(), store, "/mcp", 64).max_per_user == 1
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "99")
    assert BoundedSessionManager(_FakeSDK(), store, "/mcp", 4).max_per_user == 4
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "not-a-number")
    with pytest.raises(ValueError, match="MCP_MAX_SESSIONS_PER_USER"):
        BoundedSessionManager(_FakeSDK(), store, "/mcp", 64)


# ---------------------------------------------------------------------------
# Ownership binding on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_bound_to_creating_user_and_reusable(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")  # raw token, current hash
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            sid = await _init_session(client, alice_token)
            assert guard._session_owners[sid] == ("alice", store.get_user("alice").token_hash)

            res = await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 202


@pytest.mark.asyncio
async def test_cross_user_session_id_rejected_403_without_touching_session(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    bob_token = store.regenerate_token("bob")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            sid = await _init_session(client, alice_token)

            res = await client.post(
                "/mcp",
                headers={**_bearer(bob_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 403
            assert res.json() == {"error": "Session belongs to another user"}

            # Refused WITHOUT touching the other user's session: alice's
            # session stays bound and fully usable.
            assert guard._session_owners.get(sid) == ("alice", store.get_user("alice").token_hash)
            ok = await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert ok.status_code == 202

            # And bob can still open a session of his own.
            bob_sid = await _init_session(client, bob_token, request_id=9)
            assert guard._session_owners[bob_sid][0] == "bob"


@pytest.mark.asyncio
async def test_rotated_token_closes_stale_session_and_answers_401(tmp_path):
    app, guard, store = _make_app(tmp_path)
    old_token = store.regenerate_token("alice")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            sid = await _init_session(client, old_token)

            new_token = store.regenerate_token("alice")  # rotate under the session

            # New (valid) token + the session id created under the old one:
            # the stale transport is closed and the request answered 401.
            res = await client.post(
                "/mcp",
                headers={**_bearer(new_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 401
            assert sid not in guard._session_owners
            assert sid not in guard._sdk._server_instances

            # The client re-initializes cleanly under the rotated token.
            new_sid = await _init_session(client, new_token, request_id=2)
            assert guard._session_owners[new_sid] == ("alice", store.get_user("alice").token_hash)


@pytest.mark.asyncio
async def test_owner_table_reaped_for_sessions_the_sdk_discarded(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            # An owner entry whose session no longer exists (e.g. the SDK
            # reaped it by idle timeout) must not survive a request that
            # carries a session id.
            guard._session_owners["ghost"] = ("alice", "dead-hash")
            res = await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": "ghost"},
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            )
            assert res.status_code == 404  # SDK: unknown session id
            assert "ghost" not in guard._session_owners


# ---------------------------------------------------------------------------
# Per-user fairness: the cap evicts the caller's own LRU session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_user_cap_evicts_own_lru_session_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "3")
    monkeypatch.setenv("MCP_MAX_SESSIONS", "8")
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    bob_token = store.regenerate_token("bob")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            s1 = await _init_session(client, alice_token, request_id=1)
            s2 = await _init_session(client, alice_token, request_id=2)
            s3 = await _init_session(client, alice_token, request_id=3)
            bob_sid = await _init_session(client, bob_token, request_id=4)
            assert len(guard._session_owners) == 4

            # Touch s1 so s2 is alice's least-recently-used session…
            await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": s1},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            # …then open a 4th: her own LRU session is evicted, never bob's,
            # and the new session is bound normally (no global 503).
            s4 = await _init_session(client, alice_token, request_id=5)
            assert s2 not in guard._session_owners
            assert s2 not in guard._sdk._server_instances
            assert set(guard._owned_sessions("alice")) == {s1, s3, s4}
            assert guard._owned_sessions("bob") == [bob_sid]
            assert guard._session_owners[s4] == ("alice", store.get_user("alice").token_hash)


# ---------------------------------------------------------------------------
# Credential changes and deletion drop the account's sessions
# ---------------------------------------------------------------------------


async def _login(client: httpx.AsyncClient, username: str, password: str) -> None:
    res = await login_via_form(client, username, password)
    assert res.status_code in (303, 200), res.text


async def _csrf(client: httpx.AsyncClient) -> str:
    page = await client.get("/")
    assert page.status_code == 200
    match = CSRF_RE.search(page.text)
    assert match, "portal page must embed a CSRF token"
    return match.group(1)


@pytest.mark.asyncio
async def test_portal_regenerate_token_drops_user_sessions(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            sid = await _init_session(client, alice_token)
            assert guard._owned_sessions("alice") == [sid]

            await _login(client, "alice", ALICE_PASSWORD)
            res = await client.post(
                "/portal/regenerate-token",
                data={"csrf_token": await _csrf(client)},
            )
            assert res.status_code == 303
            assert guard._owned_sessions("alice") == []
            assert sid not in guard._sdk._server_instances


@pytest.mark.asyncio
async def test_admin_reset_password_and_token_rotation_drop_target_sessions(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await _login(client, "root", "root-pw")
            admin_csrf = await _csrf(client)

            # Password reset closes the target's sessions (not the admin's).
            sid = await _init_session(client, alice_token, request_id=1)
            res = await client.post(
                "/admin/reset-password",
                data={"csrf_token": admin_csrf, "username": "alice", "new_password": "brand-new-pw"},
            )
            assert res.status_code == 303
            assert guard._owned_sessions("alice") == []
            assert sid not in guard._sdk._server_instances

            # Token rotation likewise.
            sid = await _init_session(client, alice_token, request_id=2)
            res = await client.post(
                "/admin/regenerate-token",
                data={"csrf_token": admin_csrf, "username": "alice"},
            )
            assert res.status_code == 303
            assert guard._owned_sessions("alice") == []
            assert sid not in guard._sdk._server_instances


@pytest.mark.asyncio
async def test_admin_delete_user_drops_sessions(tmp_path):
    app, guard, store = _make_app(tmp_path)
    alice_token = store.regenerate_token("alice")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            sid = await _init_session(client, alice_token, request_id=1)
            await _login(client, "root", "root-pw")
            res = await client.post(
                "/admin/delete-user",
                data={
                    "csrf_token": await _csrf(client),
                    "username": "alice",
                    "confirm_username": "alice",
                },
            )
            assert res.status_code == 303
            assert guard._owned_sessions("alice") == []
            assert sid not in guard._sdk._server_instances
