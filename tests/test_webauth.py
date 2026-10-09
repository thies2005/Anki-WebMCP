"""Tests for the web-auth layer: signed sessions, CSRF, rate limiting, and the login/logout routes."""

import re

import httpx
import pytest

from src.config import Settings
from src.main import create_app
from src.users import UserStore
from src.webauth import (
    SESSION_COOKIE_NAME,
    LoginRateLimiter,
    issue_csrf,
    issue_session,
    secure_cookies_enabled,
    verify_csrf,
    verify_session,
)

SECRET = "test-session-secret"
SECRET_OTHER = "some-other-secret"


# ---------------------------------------------------------------------------
# Pure session-cookie logic
# ---------------------------------------------------------------------------


def test_session_round_trip():
    cookie = issue_session(SECRET, "alice")
    assert verify_session(SECRET, cookie) == "alice"


def test_session_cookie_varies_between_issues():
    # The random nonce must make two logins for the same account produce
    # different cookie values.
    assert issue_session(SECRET, "alice") != issue_session(SECRET, "alice")


def test_tampered_username_segment_rejected():
    cookie = issue_session(SECRET, "alice")
    tampered = "ZXZl" + cookie.split("|", 1)[1]  # b64("eve") in place of b64("alice")
    assert verify_session(SECRET, tampered) is None


def test_tampered_mac_segment_rejected():
    cookie = issue_session(SECRET, "alice")
    u, expiry, nonce, _mac = cookie.split("|")
    tampered = "|".join([u, expiry, nonce, "AAAA" * 11])
    assert verify_session(SECRET, tampered) is None


def test_tampered_expiry_rejected():
    cookie = issue_session(SECRET, "alice")
    u, expiry, nonce, mac = cookie.split("|")
    tampered = "|".join([u, str(int(expiry) + 9999), nonce, mac])
    assert verify_session(SECRET, tampered) is None


def test_expired_session_rejected():
    cookie = issue_session(SECRET, "alice", ttl_seconds=-1)
    assert verify_session(SECRET, cookie) is None


def test_different_session_secret_rejected():
    cookie = issue_session(SECRET, "alice")
    assert verify_session(SECRET_OTHER, cookie) is None


@pytest.mark.parametrize(
    "value", ["", "garbage", "a|b|c", "a|b|c|d|e", "YWxpY2U|notanumber|nonce|AAAA"]
)
def test_malformed_session_values_rejected(value):
    assert verify_session(SECRET, value) is None


def test_non_ascii_session_segments_rejected_not_raised():
    # Crafted cookie with a non-ASCII segment: the contract is None, never a
    # UnicodeEncodeError escaping the auth layer (was a 500 pre-fix).
    assert verify_session(SECRET, "AAAA|ö\xff|n|AAAA") is None
    assert verify_session(SECRET, "AAAA|123|nön|AAAA") is None


# ---------------------------------------------------------------------------
# CSRF primitives
# ---------------------------------------------------------------------------


def test_csrf_round_trip():
    cookie = issue_session(SECRET, "alice")
    token = issue_csrf(cookie)
    assert verify_csrf(cookie, token) is True


def test_csrf_wrong_token_rejected():
    cookie = issue_session(SECRET, "alice")
    assert verify_csrf(cookie, "0" * 64) is False


def test_csrf_token_bound_to_its_session():
    cookie_a = issue_session(SECRET, "alice")
    cookie_b = issue_session(SECRET, "alice")  # same user, different nonce
    assert verify_csrf(cookie_b, issue_csrf(cookie_a)) is False


def test_csrf_against_malformed_session_value_is_false():
    assert verify_csrf("garbage", issue_csrf(issue_session(SECRET, "alice"))) is False
    assert verify_csrf(issue_session(SECRET, "alice"), "") is False


def test_csrf_non_ascii_token_rejected_not_raised():
    cookie = issue_session(SECRET, "alice")
    # Non-ASCII form value must compare False, not raise TypeError (was a 500 pre-fix).
    assert verify_csrf(cookie, "tökén") is False
    assert verify_csrf(cookie, "0" * 63 + "ö") is False


# ---------------------------------------------------------------------------
# LoginRateLimiter
# ---------------------------------------------------------------------------


def _limiter(now, **kwargs):
    return LoginRateLimiter(now_fn=lambda: now[0], **kwargs)


def test_rate_limiter_locks_after_five_consecutive_failures():
    now = [1000.0]
    rl = _limiter(now)
    for _ in range(4):
        rl.record_failure("alice", "10.0.0.1")
    assert rl.check("alice", "10.0.0.1") is True
    rl.record_failure("alice", "10.0.0.1")  # 5th consecutive failure
    assert rl.check("alice", "10.0.0.1") is False


def test_rate_limiter_success_clears_history():
    now = [1000.0]
    rl = _limiter(now)
    for _ in range(5):
        rl.record_failure("alice", "10.0.0.1")
    assert rl.check("alice", "10.0.0.1") is False
    rl.record_success("alice", "10.0.0.1")
    assert rl.check("alice", "10.0.0.1") is True


def test_rate_limiter_independent_per_username_and_ip():
    now = [1000.0]
    rl = _limiter(now)
    for _ in range(5):
        rl.record_failure("alice", "10.0.0.1")
    # Different IP, different username: both unaffected.
    assert rl.check("alice", "10.0.0.2") is True
    assert rl.check("bob", "10.0.0.1") is True


def test_rate_limiter_lockout_expires_and_counter_resets():
    now = [1000.0]
    rl = _limiter(now)
    for _ in range(5):
        rl.record_failure("alice", "10.0.0.1")
    assert rl.check("alice", "10.0.0.1") is False
    now[0] += 15 * 60 + 1  # past the lockout window
    assert rl.check("alice", "10.0.0.1") is True
    # A lapsed lockout requires a fresh run of consecutive failures.
    rl.record_failure("alice", "10.0.0.1")
    assert rl.check("alice", "10.0.0.1") is True


def test_rate_limiter_bounds_entries_and_evicts_oldest():
    now = [1000.0]
    rl = _limiter(now, max_entries=3)
    for name in ["a", "b", "c", "d"]:
        rl.record_failure(name, "10.0.0.1")
        now[0] += 1
    assert len(rl._entries) == 3
    # "a" was the oldest activity and was evicted.
    assert ("a", "10.0.0.1") not in rl._entries
    assert ("d", "10.0.0.1") in rl._entries


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _make_settings(temp_ledger_file, tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", "correct-horse")
    return Settings(
        anki_connect_url="http://127.0.0.1:8765",
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
        session_secret=SECRET,
        users_file=str(users_file),
    )


@pytest.fixture
def web_app(temp_ledger_file, tmp_path):
    return create_app(_make_settings(temp_ledger_file, tmp_path))


@pytest.fixture
def client(web_app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app),
        base_url="http://testserver",
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_login_page_returns_form(client):
    res = await client.get("/login")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert '<form method="post" action="/login">' in res.text
    assert 'name="username"' in res.text and 'name="password"' in res.text
    assert 'name="viewport"' in res.text


@pytest.mark.asyncio
async def test_login_page_redirects_when_already_logged_in(client):
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    assert res.status_code == 303
    assert client.cookies.get(SESSION_COOKIE_NAME)
    res = await client.get("/login")
    assert res.status_code == 303
    assert res.headers["location"] == "/"


@pytest.mark.asyncio
async def test_login_success_sets_signed_cookie_and_redirects(client):
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    set_cookie = res.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    # The issued cookie must verify under the same session secret.
    cookie_value = client.cookies.get(SESSION_COOKIE_NAME)
    assert verify_session(SECRET, cookie_value) == "alice"


@pytest.mark.asyncio
async def test_login_secure_cookie_flag_follows_env(client, monkeypatch):
    monkeypatch.delenv("FORCE_SECURE_COOKIES", raising=False)
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    assert "secure" not in res.headers["set-cookie"].lower()

    monkeypatch.setenv("FORCE_SECURE_COOKIES", "true")
    assert secure_cookies_enabled() is True
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    assert "secure" in res.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_login_wrong_password_is_401_generic(client):
    res = await client.post(
        "/login", data={"username": "alice", "password": "wrong-password"}
    )
    assert res.status_code == 401
    assert "Invalid username or password" in res.text
    assert "Set-Cookie" not in res.headers


@pytest.mark.asyncio
async def test_login_unknown_user_gets_identical_generic_message(client):
    known = await client.post(
        "/login", data={"username": "alice", "password": "wrong-password"}
    )
    unknown = await client.post(
        "/login", data={"username": "nobody", "password": "wrong-password"}
    )
    assert known.status_code == unknown.status_code == 401
    # Same generic message: no hint whether the username exists.
    assert "Invalid username or password" in unknown.text
    assert "alice" not in unknown.text and "nobody" not in unknown.text


@pytest.mark.asyncio
async def test_login_locked_out_returns_429(client):
    for _ in range(5):
        res = await client.post(
            "/login", data={"username": "alice", "password": "wrong-password"}
        )
        assert res.status_code == 401
    # Even the CORRECT password is rejected while locked out.
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    assert res.status_code == 429
    assert "Too many failed attempts" in res.text


@pytest.mark.asyncio
async def test_root_redirects_unauthenticated_to_login(client):
    res = await client.get("/")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_root_shows_username_and_logout_form_when_authenticated(client):
    cookie = issue_session(SECRET, "alice")
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    res = await client.get("/")
    assert res.status_code == 200
    assert "Logged in as" in res.text
    assert "alice" in res.text
    assert '<form method="post" action="/logout">' in res.text
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', res.text)
    assert match, "logout form must carry a CSRF token"
    assert verify_csrf(cookie, match.group(1)) is True


@pytest.mark.asyncio
async def test_root_rejects_cookie_for_account_not_in_store(client):
    # A structurally valid cookie naming an account the store does not have
    # must not grant access (fail-closed lookup, not just signature check).
    client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "ghost"))
    res = await client.get("/")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_logout_with_csrf_clears_cookie_and_redirects(client):
    res = await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    res = await client.post("/logout", data={"csrf_token": issue_csrf(cookie)})
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    set_cookie = res.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    lowered = set_cookie.lower()
    assert "max-age=0" in lowered or "expires=thu, 01 jan 1970" in lowered


@pytest.mark.asyncio
async def test_logout_without_csrf_token_rejected(client):
    await client.post(
        "/login", data={"username": "alice", "password": "correct-horse"}
    )
    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    res = await client.post("/logout", data={})
    assert res.status_code == 403
    res = await client.post("/logout", data={"csrf_token": "0" * 64})
    assert res.status_code == 403
    # The session itself is untouched by rejected logout attempts.
    assert verify_session(SECRET, cookie) == "alice"


@pytest.mark.asyncio
async def test_logout_without_session_redirects_to_login(client):
    res = await client.post("/logout", data={"csrf_token": "0" * 64})
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_mcp_endpoint_still_requires_legacy_token(web_app):
    # The scoped middleware must keep guarding the MCP surface with the
    # legacy shared token even though the web routes bypass it.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app),
        base_url="http://testserver",
    ) as bare_client:
        res = await bare_client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"})
        assert res.status_code == 401
