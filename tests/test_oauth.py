"""Tests for the OAuth 2.1 authorization server (claude.ai custom connectors).

Drives the full wire flow a connector performs: RFC 9728 discovery, RFC 7591
dynamic client registration, consent-gated authorization through the REAL
portal login (src/webauth session cookie + CSRF), the PKCE S256 code
exchange, and refresh-token rotation — plus the middleware exemptions and
the WWW-Authenticate metadata pointer that unauthenticated OAuth clients
rely on to discover the server.
"""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.config import Settings
from src.main import create_app
from src.users import UserStore

# POST /login requires the prelogin CSRF pair issued by GET /login; the
# shared helper lives with the Wave B regression tests.
from test_audit_fixes_b import login_via_form

SECRET = "test-session-secret"
ALICE_PASSWORD = "correct-horse"
ALICE_REDIRECT = "https://claude.ai/api/mcp/auth/callback"
SECOND_REDIRECT = "https://claude.example.com/callback"
STATE = "st-4te-value"

# The consent form's hidden inputs (params echo + CSRF token), parsed the
# naive way: values chosen in these tests never contain HTML-special chars,
# so no entity round trip is needed (a real browser unescapes them anyway).
HIDDEN_INPUT_RE = re.compile(r'<input type="hidden" name="([^"]*)" value="([^"]*)"')


def _pkce_pair():
    """(verifier, S256 challenge) exactly as a real connector computes them."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    return Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=str(users_file))


@pytest.fixture
def app(tmp_path):
    return create_app(_make_settings(tmp_path))


@pytest.fixture
def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )


async def _register_client(client: httpx.AsyncClient, redirect_uris=None) -> httpx.Response:
    payload = {
        "client_name": "Claude",
        "redirect_uris": redirect_uris or [ALICE_REDIRECT],
    }
    return await client.post("/register", json=payload)


async def _consent_roundtrip(
    client: httpx.AsyncClient, client_id: str, decision: str = "allow"
):
    """Drive GET /oauth/authorize then submit the rendered consent form.

    The POST reuses the hidden inputs verbatim from the rendered HTML (plus
    the decision button value), so the test exercises the exact echo and
    re-validate round trip a browser performs. Returns (response, verifier).
    """
    verifier, challenge = _pkce_pair()
    page = await client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "scope": "mcp",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert page.status_code == 200, f"expected the consent page, got {page.status_code}"
    assert "requests access to your Anki collection" in page.text
    hidden = dict(HIDDEN_INPUT_RE.findall(page.text))
    assert "csrf_token" in hidden, "consent form must carry a CSRF token"
    res = await client.post("/oauth/authorize", data={**hidden, "decision": decision})
    return res, verifier


async def _exchange_code(client, client_id, code, verifier):
    return await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": ALICE_REDIRECT,
            "code_verifier": verifier,
        },
    )


async def _full_flow_tokens(app, client):
    """Log in, register, consent, and exchange; returns (token body, client_id)."""
    login = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert login.status_code == 303
    registered = (await _register_client(client)).json()["client_id"]
    res, verifier = await _consent_roundtrip(client, registered)
    assert res.status_code == 302, f"expected the code redirect, got {res.status_code}"
    query = parse_qs(urlparse(res.headers["location"]).query)
    code = query["code"][0]
    assert query["state"] == [STATE]
    token_res = await _exchange_code(client, registered, code, verifier)
    assert token_res.status_code == 200
    return token_res.json(), registered


# -- 1. discovery metadata ------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_endpoints_documented_shapes(client):
    res = await client.get("/.well-known/oauth-protected-resource")
    assert res.status_code == 200
    body = res.json()
    assert body["resource"] == "http://testserver/mcp"
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] == ["http://testserver"]

    res = await client.get("/.well-known/oauth-authorization-server")
    assert res.status_code == 200
    meta = res.json()
    assert meta["issuer"] == "http://testserver"
    assert meta["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert meta["token_endpoint"] == "http://testserver/oauth/token"
    assert meta["registration_endpoint"] == "http://testserver/register"
    assert meta["response_types_supported"] == ["code"]
    assert "authorization_code" in meta["grant_types_supported"]
    assert "refresh_token" in meta["grant_types_supported"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "mcp" in meta["scopes_supported"]


# -- 2. dynamic client registration ----------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_client_registration(client):
    res = await _register_client(client, [ALICE_REDIRECT, SECOND_REDIRECT])
    assert res.status_code == 201
    body = res.json()
    assert body["client_id"]
    assert body["client_name"] == "Claude"
    assert body["redirect_uris"] == [ALICE_REDIRECT, SECOND_REDIRECT]
    assert body["token_endpoint_auth_method"] == "none"

    # Malformed metadata (no redirect_uris) is refused with the RFC 7591 error.
    malformed = await client.post("/register", json={"client_name": "Claude"})
    assert malformed.status_code == 400
    assert malformed.json() == {"error": "invalid_client_metadata"}

    # A non-https (non-loopback) redirect URI is likewise invalid metadata.
    insecure = await _register_client(client, ["http://evil.example.com/callback"])
    assert insecure.status_code == 400
    assert insecure.json() == {"error": "invalid_client_metadata"}


# -- 3. full happy path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_full_authorization_code_flow(app, client):
    token_body, _client_id = await _full_flow_tokens(app, client)
    assert token_body["access_token"] == app.state.anki_store.peek_token("alice")
    assert token_body["token_type"] == "Bearer"
    assert token_body["scope"] == "mcp"
    assert token_body["refresh_token"]
    assert "expires_in" not in token_body  # the MCP token has no expiry


# -- 4. redirect_uri validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_unregistered_redirect_uri_is_refused_not_redirected(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    res = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": "https://evil.example.com/callback",
            "response_type": "code",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    # Open-redirect protection: plain 400, never a redirect to the attacker URL.
    assert res.status_code == 400
    assert "location" not in res.headers


# -- 5. single-use codes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_code_is_single_use(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    res, verifier = await _consent_roundtrip(client, registered)
    code = parse_qs(urlparse(res.headers["location"]).query)["code"][0]

    first = await _exchange_code(client, registered, code, verifier)
    assert first.status_code == 200
    second = await _exchange_code(client, registered, code, verifier)
    assert second.status_code == 400
    assert second.json() == {"error": "invalid_grant"}


# -- 6. PKCE enforcement -------------------------------------------------------------


@pytest.mark.asyncio
async def test_pkce_mismatch_rejected(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    res, _verifier = await _consent_roundtrip(client, registered)
    code = parse_qs(urlparse(res.headers["location"]).query)["code"][0]

    wrong = await _exchange_code(client, registered, code, "a" * 64)
    assert wrong.status_code == 400
    assert wrong.json() == {"error": "invalid_grant"}


# -- 7. refresh rotation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token_rotation(app, client):
    token_body, registered = await _full_flow_tokens(app, client)
    old_refresh = token_body["refresh_token"]

    res = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered,
            "refresh_token": old_refresh,
        },
    )
    assert res.status_code == 200
    rotated = res.json()
    assert rotated["access_token"] == app.state.anki_store.peek_token("alice")
    assert rotated["refresh_token"] != old_refresh

    # The old refresh token is dead (rotation burns it)...
    replay = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered,
            "refresh_token": old_refresh,
        },
    )
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}

    # ...and the newly issued one works.
    again = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered,
            "refresh_token": rotated["refresh_token"],
        },
    )
    assert again.status_code == 200
    assert again.json()["access_token"] == app.state.anki_store.peek_token("alice")


# -- 8. WWW-Authenticate metadata pointer ------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_401_points_at_protected_resource_metadata(client):
    res = await client.post("/mcp", json={})
    assert res.status_code == 401
    challenge = res.headers["www-authenticate"]
    assert challenge.startswith("Bearer resource_metadata=")
    assert ".well-known/oauth-protected-resource" in challenge


# -- 9. middleware exemptions --------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_endpoints_exempt_from_mcp_token_auth(client):
    # No MCP token anywhere in this test: discovery is public, registration
    # and the token endpoint answer as OAuth endpoints (201/400) — never with
    # the middleware's 401.
    assert (await client.get("/.well-known/oauth-protected-resource")).status_code == 200
    assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 200
    assert (await _register_client(client)).status_code == 201
    token_res = await client.post(
        "/oauth/token", data={"grant_type": "authorization_code"}
    )
    assert token_res.status_code == 400
    assert token_res.json() == {"error": "invalid_grant"}


# -- consent behaviors (session gate, deny path) ----------------------------------------------


@pytest.mark.asyncio
async def test_authorize_requires_portal_session(client):
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    res = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert res.status_code == 302
    # The login redirect carries the authorize URL as a validated `next`
    # parameter, so the claude.ai-initiated flow survives the login detour.
    location = res.headers["location"]
    assert location.startswith("/login?next=")
    from urllib.parse import unquote

    next_path = unquote(location.split("next=", 1)[1])
    assert next_path.startswith("/oauth/authorize?")
    assert f"client_id={registered}" in next_path


@pytest.mark.asyncio
async def test_login_next_roundtrip_lands_back_on_consent(client):
    """The claude.ai path end to end: authorize (cross-site, no session) ->
    login carrying next -> consent, without losing the OAuth request."""
    from test_audit_fixes_b import PRELOGIN_TOKEN_RE

    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    authorize_params = {
        "client_id": registered,
        "redirect_uri": ALICE_REDIRECT,
        "response_type": "code",
        "state": STATE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    bounce = await client.get("/oauth/authorize", params=authorize_params)
    assert bounce.status_code == 302
    # A browser follows the Location verbatim; decode once so httpx re-encodes
    # the parameter exactly once when building /login?next=...
    from urllib.parse import unquote

    next_path = unquote(bounce.headers["location"].split("next=", 1)[1])
    assert next_path.startswith("/oauth/authorize?")

    # Sign in through the REAL form rendered at /login?next=... (the hidden
    # next field must ride along exactly as the browser would submit it).
    client.cookies.clear()
    page = await client.get("/login", params={"next": next_path})
    assert page.status_code == 200
    hidden = dict(HIDDEN_INPUT_RE.findall(page.text))
    assert hidden.get("next"), "login form must carry the pending next path"
    prelogin = PRELOGIN_TOKEN_RE.search(page.text)
    assert prelogin
    import html as html_mod

    login = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": ALICE_PASSWORD,
            # A browser unescapes entities in field values; the next path
            # contains & (query separators), which the form renders as &amp;.
            "next": html_mod.unescape(hidden["next"]),
            "prelogin_token": prelogin.group(1),
        },
    )
    # Post-login redirect returns to the authorize flow, not the portal home.
    assert login.status_code == 303
    assert login.headers["location"].startswith("/oauth/authorize?")

    consent = await client.get(login.headers["location"])
    assert consent.status_code == 200
    assert "requests access to your Anki collection" in consent.text


@pytest.mark.asyncio
async def test_stale_session_after_password_reset_cannot_consent(app, client):
    """A session cookie captured before a password reset is dead for consent
    too, not just for the portal (stale-session-to-code-mint chain)."""
    await login_via_form(client, "alice", ALICE_PASSWORD)
    app.state.anki_store.set_password("alice", "new-password-after-reset")
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    res = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert res.status_code == 302
    assert res.headers["location"].startswith("/login")


@pytest.mark.asyncio
async def test_consent_page_shows_redirect_destination_host(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    page = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert "claude.ai" in page.text, "consent must show where the token is delivered"


@pytest.mark.asyncio
async def test_consent_deny_redirects_with_access_denied(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    res, _verifier = await _consent_roundtrip(client, registered, decision="deny")
    assert res.status_code == 302
    assert res.headers["location"] == f"{ALICE_REDIRECT}?error=access_denied&state={STATE}"


@pytest.mark.asyncio
async def test_consent_post_without_csrf_is_refused(client):
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    res = await client.post(
        "/oauth/authorize",
        data={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "decision": "allow",
        },
    )
    assert res.status_code == 403


# -- review-fix regressions: hardening the OAuth surface ------------------------


@pytest.mark.asyncio
async def test_register_refuses_oversized_bodies(client):
    # Header-level cap: refused before any JSON parsing allocates for it.
    res = await client.post(
        "/register",
        content=b"x",
        headers={"content-length": str(1024 * 1024)},
    )
    assert res.status_code == 413


@pytest.mark.asyncio
async def test_token_endpoint_refuses_oversized_bodies(client):
    res = await client.post(
        "/oauth/token",
        content=b"x",
        headers={"content-length": str(1024 * 1024)},
    )
    assert res.status_code == 413


@pytest.mark.asyncio
async def test_register_field_and_capacity_caps(app, client):
    # Per-field cap: an oversized client_name is invalid metadata.
    res = await _register_client(client)
    assert res.status_code == 201
    oversized = await client.post(
        "/register",
        json={"client_name": "x" * 500, "redirect_uris": [ALICE_REDIRECT]},
    )
    assert oversized.status_code == 400
    assert oversized.json() == {"error": "invalid_client_metadata"}

    # Capacity cap: once the store is full, further registrations are refused
    # (unauthenticated growth must be bounded).
    store = app.state.anki_oauth_store
    from src.oauth import MAX_CLIENTS

    for i in range(MAX_CLIENTS):
        store._clients[f"filler-{i}"] = {
            "client_id": f"filler-{i}",
            "client_name": "",
            "redirect_uris": [],
            "token_endpoint_auth_method": "none",
            "created_at": 0.0,
        }
    full = await _register_client(client)
    assert full.status_code == 400
    assert full.json() == {"error": "invalid_client_metadata"}


@pytest.mark.asyncio
async def test_requested_scope_is_coerced_not_echoed(app, client):
    """Whatever scope the client requests, the server issues exactly "mcp"."""
    await login_via_form(client, "alice", ALICE_PASSWORD)
    registered = (await _register_client(client)).json()["client_id"]
    verifier, challenge = _pkce_pair()
    page = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered,
            "redirect_uri": ALICE_REDIRECT,
            "response_type": "code",
            "scope": "admin openid offline_access",
            "state": STATE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert page.status_code == 200
    hidden = dict(HIDDEN_INPUT_RE.findall(page.text))
    res = await client.post("/oauth/authorize", data={**hidden, "decision": "allow"})
    code = parse_qs(urlparse(res.headers["location"]).query)["code"][0]
    token_res = await _exchange_code(client, registered, code, verifier)
    assert token_res.status_code == 200
    assert token_res.json()["scope"] == "mcp"


@pytest.mark.asyncio
async def test_regenerate_token_evicts_refresh_grants(app, client):
    """Incident response: regenerating the MCP token kills outstanding
    connector grants instead of letting them mint the fresh token."""
    token_body, _client_id = await _full_flow_tokens(app, client)
    refresh = token_body["refresh_token"]

    app.state.anki_store.regenerate_token("alice")

    res = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": _client_id, "refresh_token": refresh},
    )
    assert res.status_code == 400
    assert res.json() == {"error": "invalid_grant"}


@pytest.mark.asyncio
async def test_password_reset_evicts_refresh_grants(app, client):
    token_body, _client_id = await _full_flow_tokens(app, client)
    refresh = token_body["refresh_token"]

    app.state.anki_store.set_password("alice", "rotated-password")

    res = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": _client_id, "refresh_token": refresh},
    )
    assert res.status_code == 400
    assert res.json() == {"error": "invalid_grant"}


@pytest.mark.asyncio
async def test_wrong_client_refresh_burns_the_grant(app, client):
    """A refresh token replayed under a different client_id is refused AND
    burned: the rightful client cannot redeem it afterwards either."""
    token_body, registered = await _full_flow_tokens(app, client)
    refresh = token_body["refresh_token"]

    wrong = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": "impostor", "refresh_token": refresh},
    )
    assert wrong.status_code == 400
    assert wrong.json() == {"error": "invalid_grant"}
    assert "access_token" not in wrong.json()

    rightful = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": registered, "refresh_token": refresh},
    )
    assert rightful.status_code == 400
