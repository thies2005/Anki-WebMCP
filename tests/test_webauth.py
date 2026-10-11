"""Tests for the web-auth layer: signed sessions, CSRF, rate limiting, and the login/logout routes."""

import re
import time

import httpx
import pytest

from src.config import Settings
from src.main import PRELOGIN_FIELD_NAME, create_app
from src.users import UserStore
from src.webauth import (
    SESSION_COOKIE_NAME,
    LoginRateLimiter,
    issue_csrf,
    issue_session,
    secure_cookies_enabled,
    session_issued_before,
    verify_csrf,
    verify_session,
)
# The shared form-login helper lives with the Wave B regression tests: POST
# /login requires the prelogin CSRF pair issued by GET /login.
from test_audit_fixes_b import PRELOGIN_TOKEN_RE, login_via_form

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
    u, issued, expiry, nonce, _mac = cookie.split("|")
    tampered = "|".join([u, issued, expiry, nonce, "AAAA" * 11])
    assert verify_session(SECRET, tampered) is None


def test_tampered_expiry_rejected():
    cookie = issue_session(SECRET, "alice")
    u, issued, expiry, nonce, mac = cookie.split("|")
    tampered = "|".join([u, issued, str(int(expiry) + 9999), nonce, mac])
    assert verify_session(SECRET, tampered) is None


def test_tampered_issued_at_rejected():
    # The issued-at stamp is part of the MACed payload: shifting it (to dodge
    # a revocation epoch) without re-signing must fail the MAC check.
    cookie = issue_session(SECRET, "alice")
    u, issued, expiry, nonce, mac = cookie.split("|")
    tampered = "|".join([u, str(int(issued) - 10_000), expiry, nonce, mac])
    assert verify_session(SECRET, tampered) is None


def test_expired_session_rejected():
    cookie = issue_session(SECRET, "alice", ttl_seconds=-1)
    assert verify_session(SECRET, cookie) is None


def test_different_session_secret_rejected():
    cookie = issue_session(SECRET, "alice")
    assert verify_session(SECRET_OTHER, cookie) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "a|b|c",
        "a|b|c|d",  # legacy four-segment format: fails closed
        "a|b|c|d|e",  # right segment count, bogus MAC
        "a|b|c|d|e|f",
        "YWxpY2U|notanumber|nonce|AAAA",
    ],
)
def test_malformed_session_values_rejected(value):
    assert verify_session(SECRET, value) is None


def test_non_ascii_session_segments_rejected_not_raised():
    # Crafted cookie with a non-ASCII segment: the contract is None, never a
    # UnicodeEncodeError escaping the auth layer (was a 500 pre-fix).
    assert verify_session(SECRET, "AAAA|1|123|n|AAAA|ö\xff") is None
    assert verify_session(SECRET, "AAAA|1|123|nön|AAAA") is None
    assert verify_session(SECRET, "AAAA|ö\xff|123|n|AAAA") is None


# ---------------------------------------------------------------------------
# Issued-at segment (session revocation epoch primitive)
# ---------------------------------------------------------------------------


def test_session_payload_has_five_segments_with_issued_at():
    cookie = issue_session(SECRET, "alice")
    parts = cookie.split("|")
    assert len(parts) == 5
    assert parts[0] != "alice"  # username is base64url-encoded
    assert int(parts[1]) <= time.time()  # issued at (or before) now
    assert int(parts[2]) > time.time()  # expires in the future
    assert parts[3] and parts[4]  # nonce + MAC present


def test_session_issued_before_parses_issue_time():
    before = float(int(time.time()))
    cookie = issue_session(SECRET, "alice")
    after = time.time() + 1
    issued = session_issued_before(cookie)
    assert issued is not None
    assert before <= issued < after


def test_session_issued_before_rejects_malformed_and_legacy_values():
    assert session_issued_before("") is None
    assert session_issued_before("garbage") is None
    # Legacy four-segment cookie (pre-epoch format): no issued-at -> None.
    assert session_issued_before("YWxpY2U|1234567890|nonce|AAAA") is None
    # Five segments but a non-integer issued-at.
    assert session_issued_before("YWxpY2U|soon|123|nonce|AAAA") is None


def test_session_issued_before_none_even_for_verifying_cookie_without_segment():
    # The primitive and verify_session agree on fail-closed: a value without
    # the issued-at segment neither verifies nor yields an issue time, so a
    # legacy cookie can never sneak past an epoch check in get_session_user.
    legacy = "YWxpY2U|1234567890|nonce|AAAA"
    assert verify_session(SECRET, legacy) is None
    assert session_issued_before(legacy) is None


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
# LoginRateLimiter per-IP aggregate budget (username-rotation defense)
# ---------------------------------------------------------------------------


def test_rate_limiter_default_ip_budget_is_30():
    rl = LoginRateLimiter()
    assert rl.max_ip_failures == 30
    assert rl.max_failures == 5


def test_rate_limiter_rotating_usernames_eventually_locks_the_ip_bucket():
    now = [1000.0]
    rl = _limiter(now, max_ip_failures=3)
    for i in range(3):
        # Every request presents a fresh username: each per-account key stays
        # unlocked, but the per-source aggregate counts them all.
        assert rl.check(f"attacker-{i}", "10.0.0.1") is True
        rl.record_failure(f"victim-{i}", "10.0.0.1")
        now[0] += 1
    # The 4th request from this IP is rejected no matter which username it
    # presents — the aggregate budget, not the per-account key, gates it.
    assert rl.check("brand-new-username", "10.0.0.1") is False
    assert rl.check("victim-0", "10.0.0.1") is False
    # Other source IPs are unaffected.
    assert rl.check("brand-new-username", "10.0.0.2") is True


def test_rate_limiter_ip_bucket_lockout_expires_after_window():
    now = [1000.0]
    rl = _limiter(now, max_ip_failures=3)
    for i in range(3):
        rl.record_failure(f"u{i}", "10.0.0.9")
        now[0] += 1
    assert rl.check("x", "10.0.0.9") is False
    now[0] += 15 * 60 + 1  # past the lockout window
    assert rl.check("x", "10.0.0.9") is True


def test_rate_limiter_pinned_username_lockout_still_works_alongside_ip_budget():
    now = [1000.0]
    rl = _limiter(now)  # defaults: 5 per account, 30 per source
    for _ in range(5):
        rl.record_failure("pinned", "10.0.0.1")
    assert rl.check("pinned", "10.0.0.1") is False  # account key locked at 5
    # The source is not yet locked (5 < 30): a DIFFERENT username from the
    # same IP still passes the aggregate gate — per-account lockout is intact.
    assert rl.check("someone-else", "10.0.0.1") is True


def test_rate_limiter_success_clears_username_key_but_not_ip_budget():
    now = [1000.0]
    rl = _limiter(now, max_ip_failures=2)
    rl.record_failure("a", "10.0.0.1")
    rl.record_failure("b", "10.0.0.1")
    assert rl.check("c", "10.0.0.1") is False  # aggregate exhausted
    rl.record_success("a", "10.0.0.1")
    # The success clears only ("a", ip); the source budget must NOT reset, or
    # an attacker could revive it by interleaving an occasional success into
    # a username rotation.
    assert rl.check("a", "10.0.0.1") is False
    assert rl.check("b", "10.0.0.1") is False
    assert rl.check("a", "10.0.0.2") is True  # other IPs unaffected


def test_rate_limiter_empty_username_counts_once_toward_ip_budget():
    # Empty-username failures must not double-bump the aggregate key (the
    # ("", ip) shapes coincide); they also don't burn scrypt upstream.
    now = [1000.0]
    rl = _limiter(now, max_ip_failures=3)
    for _ in range(3):
        rl.record_failure("", "10.0.0.5")
    assert rl.check("", "10.0.0.5") is False
    assert rl._entries[("", "10.0.0.5")][0] == 3  # bumped once per failure


def test_rate_limiter_ip_bucket_shares_entry_cap():
    now = [1000.0]
    rl = _limiter(now, max_entries=4)
    for name in ["a", "b", "c", "d", "e"]:
        rl.record_failure(name, "10.0.0.1")
        now[0] += 1
    assert len(rl._entries) <= 4  # both key shapes share the one cap


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", "correct-horse")
    return Settings(
        session_secret=SECRET,
        # Task 4's portal opens real per-user collections under data_dir on
        # GET /, so it must not fall back to the ambient default (/data).
        data_dir=str(tmp_path),
        users_file=str(users_file),
    )


@pytest.fixture
def web_app(tmp_path):
    return create_app(_make_settings(tmp_path))


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
    # The prelogin CSRF pair (hidden field + cookie) must be present.
    assert PRELOGIN_TOKEN_RE.search(res.text), "login form must embed a prelogin token"
    assert client.cookies.get("anki_prelogin")


@pytest.mark.asyncio
async def test_login_page_redirects_when_already_logged_in(client):
    res = await login_via_form(client, "alice", "correct-horse")
    assert res.status_code == 303
    assert client.cookies.get(SESSION_COOKIE_NAME)
    res = await client.get("/login")
    assert res.status_code == 303
    assert res.headers["location"] == "/"


@pytest.mark.asyncio
async def test_login_success_sets_signed_cookie_and_redirects(client):
    res = await login_via_form(client, "alice", "correct-horse")
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    set_cookie = res.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    # Lax (not strict): the OAuth authorize flow arrives as a cross-site
    # top-level GET from the connector host; strict would force a re-login on
    # every connector link. Cross-site POSTs still never carry the cookie.
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    # The issued cookie must verify under the same session secret.
    cookie_value = client.cookies.get(SESSION_COOKIE_NAME)
    assert verify_session(SECRET, cookie_value) == "alice"


@pytest.mark.asyncio
async def test_login_secure_cookie_flag_follows_env(client, monkeypatch):
    monkeypatch.delenv("FORCE_SECURE_COOKIES", raising=False)
    res = await login_via_form(client, "alice", "correct-horse")
    assert "secure" not in res.headers["set-cookie"].lower()

    # Under FORCE_SECURE_COOKIES the prelogin cookie is Secure too, and httpx
    # (correctly) refuses to replay Secure cookies over the plain-http test
    # transport — so the pair is carried explicitly on the POST instead of via
    # the jar, exactly as an https browser would send it.
    monkeypatch.setenv("FORCE_SECURE_COOKIES", "true")
    assert secure_cookies_enabled() is True
    client.cookies.clear()  # drop alice's session so GET /login renders the form
    page = await client.get("/login")
    token = PRELOGIN_TOKEN_RE.search(page.text).group(1)
    client.cookies.clear()
    # The real prelogin cookie is Secure and httpx (correctly) refuses to
    # replay Secure cookies over the plain-http test transport, so place the
    # value in the jar without the flag — exactly what an https browser sends.
    client.cookies.set("anki_prelogin", token)
    res = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": "correct-horse",
            PRELOGIN_FIELD_NAME: token,
        },
    )
    assert res.status_code == 303
    assert "secure" in res.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_login_wrong_password_is_401_generic(client):
    res = await login_via_form(client, "alice", "wrong-password")
    assert res.status_code == 401
    assert "Invalid username or password" in res.text
    set_cookie = res.headers.get("set-cookie", "")
    # The rejected attempt must not establish a session; the retry form ships
    # with a fresh prelogin pair cookie instead.
    assert SESSION_COOKIE_NAME not in set_cookie
    assert "anki_prelogin=" in set_cookie.lower()
    assert PRELOGIN_TOKEN_RE.search(res.text)


@pytest.mark.asyncio
async def test_login_unknown_user_gets_identical_generic_message(client):
    known = await login_via_form(client, "alice", "wrong-password")
    unknown = await login_via_form(client, "nobody", "wrong-password")
    assert known.status_code == unknown.status_code == 401
    # Same generic message: no hint whether the username exists.
    assert "Invalid username or password" in unknown.text
    assert "alice" not in unknown.text and "nobody" not in unknown.text


@pytest.mark.asyncio
async def test_login_locked_out_returns_429(client):
    for _ in range(5):
        res = await login_via_form(client, "alice", "wrong-password")
        assert res.status_code == 401
    # Even the CORRECT password is rejected while locked out.
    res = await login_via_form(client, "alice", "correct-horse")
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
    res = await login_via_form(client, "alice", "correct-horse")
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
    await login_via_form(client, "alice", "correct-horse")
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
