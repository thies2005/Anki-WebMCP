"""Phase 1 verification gate (audit F-01..F-05, Critical & High).

Covers exactly the Phase 1 gate scenarios from ``.zcode/remediation-spec.md``:

- F-01: deleting user ``alice`` and recreating the same username rejects the
  previous holder's browser session cookie, OAuth refresh grant and
  in-flight authorization code, and the tool-usage history for that username
  starts empty (``UserStore.create_user`` stamps both revocation epochs at
  creation; ``admin_delete_user`` drops the per-username OAuth/toollog/sync
  state).
- F-02: an authorization code minted before ``set_password()`` or
  ``regenerate_token()`` redeems at ``/oauth/token`` with 400 invalid_grant.
- F-03: a crafted ``Host`` header (quotes inside) and an untrusted
  ``X-Forwarded-Proto`` value hitting the /mcp 401 challenge and
  ``/.well-known/oauth-authorization-server`` fall back to host ``localhost``
  and scheme ``http`` — no injected quotes, no extra WWW-Authenticate
  parameters.
- F-04: cross-user /mcp session use (POST and DELETE) is refused 403; rotating
  a user's token closes their existing session (401 + table cleanup); the
  per-user cap evicts only the caller's own least-recently-used session while
  other users' sessions stay untouched.
- F-05: negative ``Content-Length``, an undeclared streamed body larger than
  ``MAX_FORM_BODY_BYTES``, and multipart file parts are each rejected (413 or
  400) on ``/login`` and ``/oauth/token``.

Same-second sequencing is made deterministic with a monkeypatched fake clock
(webauth stamps cookie issued-at at whole seconds and create_user floors the
revocation epochs to the same second on purpose, so wall-clock-dependent
tests would be flaky at real second boundaries).
"""

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.config import Settings
from src.main import MAX_FORM_BODY_BYTES, create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME

# POST /login requires the prelogin CSRF pair issued by GET /login; the shared
# helper lives with the Wave B regression tests (established import pattern).
from test_audit_fixes_b import ALICE_PASSWORD, SECRET, CSRF_RE, login_via_form
from test_mcp_protocol import _call_tool

REDIRECT = "https://claude.ai/api/mcp/auth/callback"
STATE = "st-4te-value"
ALICE_RESET_PASSWORD = "brand-new-alice-pw"

# The consent form's hidden inputs (params echo + CSRF token), parsed the same
# naive way as tests/test_oauth.py (values here never contain HTML specials).
HIDDEN_INPUT_RE = re.compile(r'<input type="hidden" name="([^"]*)" value="([^"]*)"')

# F-03 payload: breaks out of the quoted WWW-Authenticate attribute the way
# the audit demonstrated, plus a comma-list X-Forwarded-Proto an untrusted
# client could spray on a direct (non-proxy) hop.
CRAFTED_HOST = 'evil.com", error="invalid_token'
CRAFTED_FORWARDED_PROTO = "https, http"

EXPECTED_CHALLENGE = (
    'Bearer resource_metadata="http://localhost/.well-known/oauth-protected-resource"'
)


# ---------------------------------------------------------------------------
# Deterministic clock + app construction
# ---------------------------------------------------------------------------


class _Clock:
    """Controllable ``time.time`` replacement: frozen until advanced."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Freeze time.time() for the whole test (src.* reads it at call time)."""
    fake = _Clock()
    monkeypatch.setattr(time, "time", fake)
    return fake


def _make_app(
    tmp_path,
    users=(
        ("root", "root-pw", "admin"),
        ("alice", ALICE_PASSWORD),
        ("bob", "bob-pw"),
    ),
):
    """Store + accounts + app, built under whatever clock is currently installed."""
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    for username, password, *rest in users:
        store.create_user(username, password, role=rest[0] if rest else "user")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    return create_app(settings)


def _bearer(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }


async def _init_session(client: httpx.AsyncClient, token: str, request_id: int = 1) -> str:
    """Initialize handshake for ``token``; returns the minted session id."""
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


# ---------------------------------------------------------------------------
# OAuth client helpers (wire-level flow, as a real connector drives it)
# ---------------------------------------------------------------------------


def _pkce_pair():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _register_client(client: httpx.AsyncClient) -> str:
    res = await client.post(
        "/register",
        json={"client_name": "Claude", "redirect_uris": [REDIRECT]},
    )
    assert res.status_code == 201, res.text
    return res.json()["client_id"]


async def _consent_code(client: httpx.AsyncClient, client_id: str):
    """Consent to a PKCE authorize request; returns the unredeemed (code, verifier)."""
    verifier, challenge = _pkce_pair()
    page = await client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "mcp",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert page.status_code == 200, f"expected the consent page, got {page.status_code}"
    hidden = dict(HIDDEN_INPUT_RE.findall(page.text))
    assert "csrf_token" in hidden
    res = await client.post("/oauth/authorize", data={**hidden, "decision": "allow"})
    assert res.status_code == 302, res.text
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert query["state"] == [STATE]
    return query["code"][0], verifier


async def _exchange_code(client: httpx.AsyncClient, client_id: str, code: str, verifier: str):
    return await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )


async def _refresh(client: httpx.AsyncClient, client_id: str, refresh_token: str):
    return await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )


async def _delete_user_via_admin(client: httpx.AsyncClient, app, username: str) -> None:
    """Delete ``username`` through the real admin route (root must be logged in)."""
    page = await client.get("/admin")
    assert page.status_code == 200, page.text
    res = await client.post(
        "/admin/delete-user",
        data={
            "csrf_token": CSRF_RE.search(page.text).group(1),
            "username": username,
            "confirm_username": username,
        },
    )
    assert res.status_code == 303, res.text
    assert app.state.anki_store.get(username) is None


# ---------------------------------------------------------------------------
# F-01: account deletion + username reuse takeover
# ---------------------------------------------------------------------------


def test_create_user_stamps_revocation_epochs_at_creation(tmp_path, clock):
    """F-01 core: a fresh record's epochs sit AT its creation instant, so
    anything minted for a previous holder of the username predates them."""
    store = UserStore(str(tmp_path / "users.json"))
    user, raw_token = store.create_user("dave", "dave-pw")
    assert raw_token
    assert user.created_at > 0.0
    assert user.created_at == user.sessions_invalid_before == user.tokens_invalid_before
    assert user.created_at == float(int(clock.now))  # second-resolution, at creation


@pytest.mark.asyncio
async def test_recreated_user_rejects_pre_deletion_credentials_and_starts_empty(
    tmp_path, clock
):
    """Delete alice through the admin route, recreate the username: her
    pre-deletion browser cookie, OAuth refresh grant and unredeemed
    authorization code are all dead, and the tool-usage history for the
    username starts empty."""
    app = _make_app(tmp_path)
    store = app.state.anki_store
    tool_log = app.state.anki_tool_usage

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            # -- alice's pre-deletion credential set -----------------------
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
            assert old_cookie
            assert (await client.get("/")).status_code == 200  # cookie live pre-deletion

            client_id = await _register_client(client)
            code1, verifier1 = await _consent_code(client, client_id)
            token_res = await _exchange_code(client, client_id, code1, verifier1)
            assert token_res.status_code == 200, token_res.text
            old_refresh = token_res.json()["refresh_token"]
            assert old_refresh

            code2, verifier2 = await _consent_code(client, client_id)  # never redeemed

            # Real tool usage through /mcp so the logger holds history for alice.
            alice_token = store.peek_token("alice")
            headers = {"Authorization": f"Bearer {alice_token}",
                       "Accept": "application/json, text/event-stream"}
            alice_sid = await _init_session(client, alice_token, request_id=10)
            headers["mcp-session-id"] = alice_sid
            call = await _call_tool(
                client, headers, "search_notes", {"query": "pre-deletion-note"}, request_id=11
            )
            assert call["result"]["isError"] is not True
            assert await tool_log.recent("alice"), "pre-deletion history must exist"

            # -- delete through the real admin route, then recreate --------
            await login_via_form(client, "root", "root-pw")
            await _delete_user_via_admin(client, app, "alice")

            clock.advance(30)
            _user, new_alice_token = store.create_user("alice", ALICE_RESET_PASSWORD)
            assert new_alice_token != alice_token

            # (a) pre-deletion browser session cookie is dead for the new alice.
            client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
            bounced = await client.get("/")
            assert bounced.status_code == 303
            assert bounced.headers["location"] == "/login"

            # (b) pre-deletion OAuth refresh grant mints nothing.
            res = await _refresh(client, client_id, old_refresh)
            assert res.status_code == 400
            assert res.json()["error"] == "invalid_grant"
            assert "access_token" not in res.json()

            # (c) pre-deletion authorization code redeems to invalid_grant.
            res = await _exchange_code(client, client_id, code2, verifier2)
            assert res.status_code == 400
            assert res.json()["error"] == "invalid_grant"
            assert "access_token" not in res.json()

            # (d) the recreated username starts with an EMPTY tool history.
            assert await tool_log.recent("alice") == []

            # The new account itself works: fresh login accepted, portal serves.
            fresh = await login_via_form(client, "alice", ALICE_RESET_PASSWORD)
            assert fresh.status_code == 303
            assert (await client.get("/")).status_code == 200


@pytest.mark.asyncio
async def test_username_reuse_floor_holds_without_deletion_route_cleanup(
    tmp_path, clock
):
    """Defense-in-depth variant: delete and recreate through the STORE only
    (no admin route, so no OAuth/toollog drop_user wiring runs). The stale
    cookie, the still-stored refresh grant and the still-stored authorization
    code must STILL be rejected — by the created_at/epoch floors alone."""
    app = _make_app(tmp_path, users=(("root", "root-pw", "admin"), ("alice", ALICE_PASSWORD)))
    store = app.state.anki_store

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            old_cookie = client.cookies.get(SESSION_COOKIE_NAME)

            client_id = await _register_client(client)
            code, verifier = await _consent_code(client, client_id)
            token_res = await _exchange_code(client, client_id, code, verifier)
            assert token_res.status_code == 200
            old_refresh = token_res.json()["refresh_token"]

            old_user = store.get_user("alice")
            assert store.delete_user("alice") is True  # store-level: no cleanup wiring

            clock.advance(30)
            new_user, _new_token = store.create_user("alice", ALICE_RESET_PASSWORD)
            assert new_user.created_at > old_user.created_at

            # Old cookie: below the recreated account's floor -> bounced to login.
            client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
            bounced = await client.get("/")
            assert bounced.status_code == 303
            assert bounced.headers["location"] == "/login"

            # Old refresh grant: still in the store, but predates the new epoch.
            res = await _refresh(client, client_id, old_refresh)
            assert res.status_code == 400
            assert res.json()["error"] == "invalid_grant"

            # Old code: still redeemable shape-wise, but the F-02 cutoff (the
            # created_at floor) rejects it.
            res = await _exchange_code(client, client_id, code, verifier)
            assert res.status_code == 400
            assert res.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# F-02: authorization codes cannot outlive a revocation epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    ["set_password", "regenerate_token"],
    ids=["code-before-password-reset", "code-before-token-regeneration"],
)
async def test_code_minted_before_revocation_redeems_invalid_grant(
    tmp_path, clock, revocation
):
    app = _make_app(tmp_path)
    store = app.state.anki_store

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            client_id = await _register_client(client)
            code, verifier = await _consent_code(client, client_id)

            # The revocation lands strictly after the code was minted (fake
            # clock), yet within the code's 60s TTL — the epoch gate, not
            # expiry, must kill it.
            clock.advance(5)
            if revocation == "set_password":
                store.set_password("alice", ALICE_RESET_PASSWORD)
            else:
                store.regenerate_token("alice")

            res = await _exchange_code(client, client_id, code, verifier)
            assert res.status_code == 400
            body = res.json()
            assert body["error"] == "invalid_grant"
            assert "access_token" not in body and "refresh_token" not in body


# ---------------------------------------------------------------------------
# F-03: Host / X-Forwarded-Proto injection into challenge + discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crafted_host_and_untrusted_forwarded_proto_fall_back_to_localhost_http(
    tmp_path,
):
    app = _make_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            hostile_headers = {
                "host": CRAFTED_HOST,
                "x-forwarded-proto": CRAFTED_FORWARDED_PROTO,
            }

            # /mcp 401 challenge: exact expected value — the crafted host
            # cannot inject quotes/attributes, and the untrusted forwarded
            # proto cannot upgrade the scheme to https.
            res = await client.post("/mcp", headers=hostile_headers)
            assert res.status_code == 401
            assert res.headers["www-authenticate"] == EXPECTED_CHALLENGE
            assert "injected" not in res.headers["www-authenticate"]

            # Discovery metadata: host falls back to localhost, scheme to http.
            res = await client.get(
                "/.well-known/oauth-authorization-server", headers=hostile_headers
            )
            assert res.status_code == 200
            meta = res.json()
            assert meta["issuer"] == "http://localhost"
            for key, value in meta.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    assert value == "http://localhost" or value.startswith(
                        "http://localhost/"
                    ), (key, value)
                    assert '"' not in value, (key, value)

            # Untrusted X-Forwarded-Proto alone (valid Host) never upgrades
            # the scheme either.
            res = await client.get(
                "/.well-known/oauth-authorization-server",
                headers={"x-forwarded-proto": "https"},
            )
            assert res.status_code == 200
            assert res.json()["issuer"] == "http://testserver"


# ---------------------------------------------------------------------------
# F-04: /mcp session ids are bound to their owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_session_post_and_delete_refused_403(tmp_path):
    app = _make_app(tmp_path)
    store = app.state.anki_store
    guard = app.state.anki_session_guard
    alice_token = store.regenerate_token("alice")
    bob_token = store.regenerate_token("bob")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            sid = await _init_session(client, alice_token, request_id=1)

            # Bob cannot POST with alice's session id.
            res = await client.post(
                "/mcp",
                headers={**_bearer(bob_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 403
            assert res.json() == {"error": "Session belongs to another user"}

            # ...nor DELETE it.
            res = await client.delete(
                "/mcp", headers={**_bearer(bob_token), "mcp-session-id": sid}
            )
            assert res.status_code == 403
            assert res.json() == {"error": "Session belongs to another user"}

            # Both refusals left alice's session intact and usable.
            assert guard._session_owners[sid] == (
                "alice",
                store.get_user("alice").token_hash,
            )
            assert sid in guard._sdk._server_instances
            ok = await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert ok.status_code == 202


@pytest.mark.asyncio
async def test_regenerating_token_invalidates_users_existing_session(tmp_path):
    app = _make_app(tmp_path)
    store = app.state.anki_store
    guard = app.state.anki_session_guard
    old_token = store.regenerate_token("alice")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            sid = await _init_session(client, old_token, request_id=1)
            new_token = store.regenerate_token("alice")  # rotate under the session

            # The old session id presented under the (valid) new token: the
            # stale transport is closed and the request answered 401.
            res = await client.post(
                "/mcp",
                headers={**_bearer(new_token), "mcp-session-id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 401
            assert sid not in guard._session_owners
            assert sid not in guard._sdk._server_instances

            # A fresh session under the rotated token works normally.
            new_sid = await _init_session(client, new_token, request_id=2)
            assert guard._session_owners[new_sid] == (
                "alice",
                store.get_user("alice").token_hash,
            )


@pytest.mark.asyncio
async def test_per_user_cap_evicts_only_own_oldest_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_MAX_SESSIONS_PER_USER", "2")
    monkeypatch.setenv("MCP_MAX_SESSIONS", "8")
    app = _make_app(tmp_path)
    store = app.state.anki_store
    guard = app.state.anki_session_guard
    alice_token = store.regenerate_token("alice")
    bob_token = store.regenerate_token("bob")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            s1 = await _init_session(client, alice_token, request_id=1)
            s2 = await _init_session(client, alice_token, request_id=2)
            bob_sid = await _init_session(client, bob_token, request_id=3)
            assert len(guard._session_owners) == 3

            # Touch s1 so s2 becomes alice's least-recently-used session…
            touched = await client.post(
                "/mcp",
                headers={**_bearer(alice_token), "mcp-session-id": s1},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert touched.status_code == 202

            # …then open a third session: her OWN LRU session is evicted —
            # never bob's — and the open succeeds (no global 503).
            s3 = await _init_session(client, alice_token, request_id=4)
            assert s2 not in guard._session_owners
            assert s2 not in guard._sdk._server_instances
            assert set(guard._owned_sessions("alice")) == {s1, s3}
            assert guard._owned_sessions("bob") == [bob_sid]
            assert guard._session_owners[s3] == (
                "alice",
                store.get_user("alice").token_hash,
            )

            # Bob's session is untouched and still usable.
            res = await client.post(
                "/mcp",
                headers={**_bearer(bob_token), "mcp-session-id": bob_sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert res.status_code == 202


# ---------------------------------------------------------------------------
# F-05: true streaming body cap + multipart file parts banned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_content_length_rejected_on_login_and_token(tmp_path):
    app = _make_app(tmp_path)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            res = await client.post(
                "/login",
                content=b"",
                headers={
                    "content-length": "-5",
                    "content-type": "application/x-www-form-urlencoded",
                },
            )
            assert res.status_code == 413
            # A refused body must not authenticate anybody.
            assert SESSION_COOKIE_NAME not in res.headers.get("set-cookie", "")

            res = await client.post(
                "/oauth/token",
                content=b"",
                headers={
                    "content-length": "-5",
                    "content-type": "application/x-www-form-urlencoded",
                },
            )
            assert res.status_code == 413
            assert res.json()["error"] == "invalid_request"


async def _raw_asgi_post(app, path: str, chunks, headers):
    """Drive the ASGI app directly with a body that carries NO Content-Length
    header at all (the HTTP/2 / chunked-hop shape httpx cannot produce).
    Returns (status, body_bytes)."""
    pending = list(chunks)
    messages = []

    async def receive():
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,  # no content-length, no transfer-encoding
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], body


@pytest.mark.asyncio
async def test_undeclared_streamed_body_over_cap_rejected_on_login_and_token(tmp_path):
    """No Content-Length anywhere: the bytes actually streamed are cut off at
    MAX_FORM_BODY_BYTES, before any parser sees them."""
    app = _make_app(tmp_path)
    over_cap = [b"username=alice&password=x&", b"a" * (MAX_FORM_BODY_BYTES + 1)]
    assert sum(len(c) for c in over_cap) > MAX_FORM_BODY_BYTES
    urlencoded = [(b"content-type", b"application/x-www-form-urlencoded")]

    status, _body = await _raw_asgi_post(app, "/login", over_cap, urlencoded)
    assert status == 413

    status, body = await _raw_asgi_post(app, "/oauth/token", over_cap, urlencoded)
    assert status == 413
    assert json.loads(body)["error"] == "invalid_request"


def _multipart_file_body() -> bytes:
    boundary = "XbOuNdArY"
    return (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="username"\r\n\r\n'
        "alice\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="avatar"; filename="a.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        + "A" * 64
        + f"\r\n--{boundary}--\r\n"
    ).encode()


@pytest.mark.asyncio
async def test_multipart_file_parts_rejected_on_login_and_token(tmp_path):
    app = _make_app(tmp_path)
    body = _multipart_file_body()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            # max_files=0 refuses the file part at part-begin, before starlette
            # spools anything to /tmp on either unauthenticated route.
            res = await client.post(
                "/login",
                content=body,
                headers={"content-type": "multipart/form-data; boundary=XbOuNdArY"},
            )
            assert res.status_code in (400, 413)

            res = await client.post(
                "/oauth/token",
                content=body,
                headers={"content-type": "multipart/form-data; boundary=XbOuNdArY"},
            )
            assert res.status_code in (400, 413)
