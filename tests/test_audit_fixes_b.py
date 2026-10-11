"""Regression tests for audit run-1 Wave B (main.py + config.py fixes).

Covers six audit records:
- webauth-login-post-no-csrf: pre-login CSRF binding on POST /login
- webauth-session-no-revocation (wiring): an admin password reset kills the
  pre-reset session, so the stale-cookie-to-token-mint chain fails
- delete-user-stale-registry-state-recreated-username: handler drops the
  registry state and archives the ledger dir; a recreated username starts
  from an empty collection and an empty ledger, even after a restart
- portal-sync-bypasses-maintenance-gate: /portal/sync honors maintenance mode
- mcp-streamable-http-default-session-table: the /mcp session table is sized
  from settings (not SDK wheel defaults) and explicitly stateful
- login-form-parse-before-rate-limiter: oversized form bodies are refused by
  declared Content-Length BEFORE the form parse; uvicorn gains
  limit_concurrency

This module also hosts the shared form-login helper used by the other route
test modules: POST /login now requires the prelogin CSRF pair issued by
GET /login, so every test that logs in through the real form must go through
:func:`login_via_form`.
"""

import json
import os
import re
import time

import httpx
import pytest
from starlette.requests import Request

from src.collections import CollectionRegistry
from src.config import Settings
from src.main import (
    MAX_FORM_BODY_BYTES,
    PRELOGIN_COOKIE_NAME,
    PRELOGIN_FIELD_NAME,
    PRELOGIN_TTL_SECONDS,
    UVICORN_LIMIT_CONCURRENCY,
    _archive_ledger_dir,
    _body_too_large,
    _issue_prelogin,
    _uvicorn_run_kwargs,
    _verify_prelogin,
    create_app,
)
from src.users import UserStore
from src.webauth import (
    FIELD_SEP,
    NONCE_BYTES,
    SESSION_COOKIE_NAME,
    _b64encode,
    _sign,
    derive_session_key,
    issue_csrf,
    issue_session,
    verify_session,
)

SECRET = "test-session-secret"
ALICE_PASSWORD = "correct-horse"

PRELOGIN_TOKEN_RE = re.compile(
    rf'name="{PRELOGIN_FIELD_NAME}" value="([A-Za-z0-9_\-]+\|[0-9a-f]{{64}})"'
)
CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]{64})"')


# ---------------------------------------------------------------------------
# Shared form-login helper (used by test_webauth / test_admin / test_config)
# ---------------------------------------------------------------------------


async def login_via_form(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    """Log in through the real form: GET /login first, then POST with the pair.

    Clears the cookie jar first so GET /login renders the form even when a
    session (or a stale prelogin cookie) is already present, fetches the
    prelogin CSRF pair from the rendered page (the cookie lands in the jar),
    and posts credentials plus the hidden field. Returns the POST response.
    """
    client.cookies.clear()
    page = await client.get("/login")
    assert page.status_code == 200, "a fresh visitor must get the login form"
    match = PRELOGIN_TOKEN_RE.search(page.text)
    assert match, "login page must embed a prelogin CSRF token"
    return await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            PRELOGIN_FIELD_NAME: match.group(1),
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, **overrides) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    store.create_user("root", "root-pw", role="admin")
    values = {
        "session_secret": SECRET,
        "data_dir": str(tmp_path),
        "users_file": str(users_file),
    }
    values.update(overrides)
    return Settings(**values)


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


def _session_value_issued_at(secret: str, username: str, issued_at: float) -> str:
    """A valid session cookie backdated to ``issued_at`` (epoch seconds).

    issue_session always stamps time.time(), which makes same-second reset
    races untestable; the epoch comparison is deterministic against a
    backdated cookie while exercising the exact same signed format.
    """
    username_b64 = _b64encode(username.encode("utf-8"))
    issued = str(int(issued_at))
    expiry = str(int(time.time()) + 7200)
    nonce = _b64encode(os.urandom(NONCE_BYTES))
    mac = _sign(derive_session_key(secret), username_b64, issued, expiry, nonce)
    return FIELD_SEP.join((username_b64, issued, expiry, nonce, _b64encode(mac)))


# ---------------------------------------------------------------------------
# Prelogin token primitives (pure logic)
# ---------------------------------------------------------------------------


def test_prelogin_round_trip_and_rejections():
    token = _issue_prelogin(SECRET)
    nonce, sep, mac = token.partition("|")
    assert sep and nonce and len(mac) == 64
    assert _verify_prelogin(SECRET, token) is True
    # Wrong domain secret, tampered MAC, tampered nonce, and malformed shapes
    # all fail closed.
    assert _verify_prelogin("other-secret", token) is False
    assert _verify_prelogin(SECRET, nonce + "|" + "0" * 64) is False
    flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert _verify_prelogin(SECRET, flipped) is False
    for bad in ("", "abc", "|abcdef", nonce + "|", nonce):
        assert _verify_prelogin(SECRET, bad) is False
    # Non-ASCII segments can never match the ASCII-only format: no raise.
    assert _verify_prelogin(SECRET, "néon|" + "a" * 64) is False
    assert _verify_prelogin("", token) is False
    # Every mint is unique (random nonce).
    assert _issue_prelogin(SECRET) != _issue_prelogin(SECRET)


def test_prelogin_key_is_domain_separated_from_session_signing():
    # A prelogin token must not verify when the session-secret key material is
    # confused: the HMAC is taken under the dedicated anki-mcp/prelogin/v1 key.
    token = _issue_prelogin(SECRET)
    nonce, _, mac = token.partition("|")
    session_mac = _sign(derive_session_key(SECRET), nonce, "0", "1", "x")
    assert mac != _b64encode(session_mac)


def test_body_too_large_boundary():
    def request_with(content_length: bytes) -> Request:
        headers = [(b"content-length", content_length)] if content_length else []
        return Request(
            {"type": "http", "method": "POST", "path": "/login", "headers": headers}
        )

    assert _body_too_large(request_with(str(MAX_FORM_BODY_BYTES + 1).encode())) is True
    assert _body_too_large(request_with(b"999999999")) is True
    # At the cap or below: allowed.
    assert _body_too_large(request_with(str(MAX_FORM_BODY_BYTES).encode())) is False
    assert _body_too_large(request_with(b"0")) is False
    # Absent header without transfer-encoding: not rejected (nothing sane
    # to read); garbage-but-present and chunked transfer-encoding are both
    # refused fail-closed (review fix F1: per-field parser caps do not bound
    # multipart file parts, so chunked must never reach the parser).
    assert _body_too_large(request_with(b"")) is False
    assert _body_too_large(request_with(b"abc")) is True
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"transfer-encoding", b"chunked")],
        }
    )
    assert _body_too_large(req) is True


def test_uvicorn_run_kwargs_carry_concurrency_limit(tmp_path):
    settings = Settings(
        session_secret="s",
        data_dir=str(tmp_path),
        users_file=str(tmp_path / "users.json"),
    )
    kwargs = _uvicorn_run_kwargs(settings)
    assert kwargs["limit_concurrency"] == UVICORN_LIMIT_CONCURRENCY == 32
    assert kwargs["host"] == settings.mcp_host
    assert kwargs["port"] == settings.mcp_port
    assert kwargs["log_level"] == settings.log_level.lower()
    assert kwargs["access_log"] is False


# ---------------------------------------------------------------------------
# Login CSRF (webauth-login-post-no-csrf)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_page_sets_prelogin_cookie_and_matching_field(client):
    res = await client.get("/login")
    assert res.status_code == 200
    set_cookie = res.headers["set-cookie"].lower()
    assert f"{PRELOGIN_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/login" in set_cookie
    assert f"max-age={PRELOGIN_TTL_SECONDS}" in set_cookie
    assert "secure" not in set_cookie  # FORCE_SECURE_COOKIES unset
    # The hidden field echoes exactly the cookie value.
    match = PRELOGIN_TOKEN_RE.search(res.text)
    assert match, "form must carry the hidden prelogin token"
    assert match.group(1) == client.cookies.get(PRELOGIN_COOKIE_NAME)


@pytest.mark.asyncio
async def test_prelogin_cookie_secure_flag_follows_env(client, monkeypatch):
    monkeypatch.setenv("FORCE_SECURE_COOKIES", "true")
    res = await client.get("/login")
    assert "secure" in res.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_login_with_valid_prelogin_pair_succeeds(client):
    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    assert verify_session(SECRET, client.cookies.get(SESSION_COOKIE_NAME)) == "alice"


@pytest.mark.asyncio
async def test_login_without_prelogin_rejected_403(client):
    res = await client.post(
        "/login", data={"username": "alice", "password": ALICE_PASSWORD}
    )
    assert res.status_code == 403
    assert "Invalid or missing CSRF token." in res.text
    # No session may be issued by a refused request.
    assert SESSION_COOKIE_NAME not in res.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_login_with_mismatched_prelogin_rejected_403(client):
    await client.get("/login")  # jar now holds pair A
    forged = _issue_prelogin(SECRET)  # a valid-format token the cookie lacks
    res = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": ALICE_PASSWORD,
            PRELOGIN_FIELD_NAME: forged,
        },
    )
    assert res.status_code == 403
    assert "Invalid or missing CSRF token." in res.text


@pytest.mark.asyncio
async def test_login_with_bad_mac_prelogin_rejected_403(client):
    await client.get("/login")
    forged = "forged-nonce|" + "0" * 64  # well-formed, wrong HMAC
    client.cookies.clear()
    client.cookies.set(PRELOGIN_COOKIE_NAME, forged)
    res = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": ALICE_PASSWORD,
            PRELOGIN_FIELD_NAME: forged,
        },
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_prelogin_cookie_missing_but_field_present_rejected(client):
    res = await client.get("/login")
    token = PRELOGIN_TOKEN_RE.search(res.text).group(1)
    client.cookies.clear()  # cookie lost (e.g. expired max_age)
    res = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": ALICE_PASSWORD,
            PRELOGIN_FIELD_NAME: token,
        },
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_login_csrf_failure_costs_no_scrypt_and_no_limiter_key(app, client, monkeypatch):
    store = app.state.anki_store
    calls = []
    real_verify = store.verify_password

    def counted_verify(username, password):
        calls.append(username)
        return real_verify(username, password)

    monkeypatch.setattr(store, "verify_password", counted_verify)

    for _ in range(3):
        res = await client.post(
            "/login", data={"username": "alice", "password": ALICE_PASSWORD}
        )
        assert res.status_code == 403
    assert calls == [], "the CSRF gate must run before any password verification"

    # No limiter key was created either: a subsequent real login still works
    # (five 403s would not lock out, but zero-touch is the stronger property).
    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert res.status_code == 303


# ---------------------------------------------------------------------------
# Body cap + parse ordering (login-form-parse-before-rate-limiter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_oversized_body_413_before_parse(app, client, monkeypatch):
    store = app.state.anki_store
    calls = []
    real_verify = store.verify_password

    def counted_verify(username, password):
        calls.append(username)
        return real_verify(username, password)

    monkeypatch.setattr(store, "verify_password", counted_verify)

    oversized = (
        f"username=alice&password={ALICE_PASSWORD}&".encode()
        + b"a" * (MAX_FORM_BODY_BYTES + 1)
    )
    res = await client.post(
        "/login",
        content=oversized,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 413
    assert calls == [], "the body must be refused before any parse or scrypt work"

    # No limiter key: a normal login right after still succeeds.
    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert res.status_code == 303


@pytest.mark.asyncio
async def test_other_form_routes_share_the_body_cap(client):
    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert res.status_code == 303
    oversized = b"email=a%40b.c&password=" + b"x" * (MAX_FORM_BODY_BYTES + 1)
    res = await client.post(
        "/portal/credentials",
        content=oversized,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 413


# ---------------------------------------------------------------------------
# Session epoch wiring (webauth-session-no-revocation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_reset_via_admin_route_kills_pre_reset_session(app, client, tmp_path):
    # A cookie issued well before the reset: the portal accepts it...
    old_alice = _session_value_issued_at(SECRET, "alice", time.time() - 100)
    client.cookies.set(SESSION_COOKIE_NAME, old_alice)
    assert (await client.get("/")).status_code == 200

    # ...then root resets alice's password through the admin route.
    client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "root"))
    admin_page = await client.get("/admin")
    csrf = CSRF_RE.search(admin_page.text).group(1)
    res = await client.post(
        "/admin/reset-password",
        data={
            "csrf_token": csrf,
            "username": "alice",
            "new_password": "brand-new-pw",
        },
    )
    assert res.status_code == 303
    assert UserStore(str(tmp_path / "users.json")).verify_password("alice", "brand-new-pw")

    # The pre-reset cookie is dead: the portal bounces to login...
    client.cookies.set(SESSION_COOKIE_NAME, old_alice)
    res = await client.get("/")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"

    # ...and the audit's demonstrated chain — stale cookie mints a persistent
    # MCP token via the portal's regenerate endpoint — now fails too (the
    # session gate runs before CSRF/token work).
    res = await client.post(
        "/portal/regenerate-token", data={"csrf_token": issue_csrf(old_alice)}
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    assert "set-cookie" not in res.headers or SESSION_COOKIE_NAME not in res.headers.get(
        "set-cookie", ""
    )

    # The new password logs in through the real form and gets a working session.
    # (Cross the second boundary first: the fresh cookie's issued-at stamp has
    # second resolution, so a login completing in the SAME second as the reset
    # floors below the float epoch and fails closed — correct behavior, but not
    # what this assertion is about.)
    time.sleep(1.05)
    res = await login_via_form(client, "alice", "brand-new-pw")
    assert res.status_code == 303
    assert (await client.get("/")).status_code == 200


@pytest.mark.asyncio
async def test_backdated_session_rejected_after_store_epoch_bump(app, client):
    # Store-level variant of the same wiring: any cookie issued before the
    # epoch set by set_password fails the portal gate, and a cookie issued
    # AFTER the bump (fresh login) still works.
    old_cookie = _session_value_issued_at(SECRET, "alice", time.time() - 100)
    client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
    assert (await client.get("/")).status_code == 200

    app.state.anki_store.set_password("alice", "rotated-pw")

    client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
    assert (await client.get("/")).status_code == 303

    # A fresh login AFTER the epoch works (cross the second boundary: the
    # cookie's second-resolution issued-at stamp must exceed the float epoch).
    time.sleep(1.05)
    res = await login_via_form(client, "alice", "rotated-pw")
    assert res.status_code == 303
    assert (await client.get("/")).status_code == 200


# ---------------------------------------------------------------------------
# Maintenance gate on /portal/sync (portal-sync-bypasses-maintenance-gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_sync_refused_in_maintenance_mode(app, client, tmp_path, monkeypatch):
    from src import main as main_module

    app.state.anki_store.set_ankiweb_credentials("alice", "user@example.com", "anki-pw")
    registry = app.state.anki_registry

    async def _must_not_sync(*args, **kwargs):
        raise AssertionError("sync_user must not be invoked while in maintenance mode")

    monkeypatch.setattr(registry, "sync_user", _must_not_sync)
    # The app's own sync manager (create_app rebinds the module global).
    monkeypatch.setattr(main_module.sync_manager, "_maintenance_mode", True)

    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert res.status_code == 303
    session_value = client.cookies.get(SESSION_COOKIE_NAME)
    res = await client.post(
        "/portal/sync", data={"csrf_token": issue_csrf(session_value)}
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    notice = await client.get("/")
    assert "Worker is currently in maintenance mode; writes are paused." in notice.text
    # No sync I/O happened: the sync_user spy above never fired, and no backup
    # file was written (opening a collection creates the directory skeleton,
    # so the notice GET's deck stats may create users/alice/backups itself).
    backups = tmp_path / "users" / "alice" / "backups"
    assert not backups.exists() or not any(backups.iterdir())


@pytest.mark.asyncio
async def test_portal_sync_runs_again_when_maintenance_mode_ends(app, client, monkeypatch):
    from src import main as main_module

    app.state.anki_store.set_ankiweb_credentials("alice", "user@example.com", "anki-pw")
    calls = []

    async def _fake_sync(username, email, password):
        calls.append(username)
        return {"success": True, "output": "ok", "outcome_unknown": False}

    monkeypatch.setattr(app.state.anki_registry, "sync_user", _fake_sync)
    manager = main_module.sync_manager

    manager.set_maintenance_mode(True)
    res = await login_via_form(client, "alice", ALICE_PASSWORD)
    res = await client.post(
        "/portal/sync", data={"csrf_token": issue_csrf(client.cookies.get(SESSION_COOKIE_NAME))}
    )
    assert res.status_code == 303
    assert calls == []

    manager.set_maintenance_mode(False)
    res = await client.post(
        "/portal/sync", data={"csrf_token": issue_csrf(client.cookies.get(SESSION_COOKIE_NAME))}
    )
    assert res.status_code == 200  # sync result page
    assert calls == ["alice"]


# ---------------------------------------------------------------------------
# /mcp session-table knobs (mcp-streamable-http-default-session-table)
# ---------------------------------------------------------------------------


def _mcp_session_manager(app):
    for route in app.routes:
        if getattr(route, "path", None) == "/mcp":
            return route.endpoint.session_manager
    raise AssertionError("/mcp route not found")


def test_mcp_session_manager_reflects_env_driven_settings(tmp_path, monkeypatch):
    # Full chain under test: env -> Settings -> create_app -> mounted route's
    # session_manager (the audit inspected the same attribute).
    monkeypatch.setenv("MCP_MAX_SESSIONS", "7")
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "42")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings()
    store = UserStore(settings.users_file)
    store.create_user("alice", ALICE_PASSWORD)
    store.create_user("root", "root-pw", role="admin")
    app = create_app(settings)
    manager = _mcp_session_manager(app)
    assert manager.max_sessions == 7
    assert manager.session_idle_timeout == 42
    assert manager.stateless is False  # stateful mode is explicit, not default drift


def test_mcp_session_manager_defaults_are_deployment_sized(tmp_path):
    app = create_app(_make_settings(tmp_path))
    manager = _mcp_session_manager(app)
    assert manager.max_sessions == 16
    assert manager.session_idle_timeout == 300
    assert manager.stateless is False


# ---------------------------------------------------------------------------
# Delete-user full detach (delete-user-stale-registry-state-recreated-username)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_user_with_open_collection_recreated_username_starts_fresh(tmp_path):
    from test_mcp_protocol import _call_tool, _init_session

    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    store.create_user("root", "root-pw", role="admin")
    store.create_user("bob", "bob-pw")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    app = create_app(settings)
    registry = app.state.anki_registry

    def add_old_note(col):
        from anki.notes import Note

        note = Note(col, col.models.by_name("Basic"))
        note["Front"] = "old-bob-secret"
        note["Back"] = "old"
        col.add_note(note, col.decks.id("Default"))

    # Give the doomed account an OPEN collection and a committed ledger entry.
    await registry.run("bob", add_old_note)
    assert registry.is_open("bob")
    await registry.ledger("bob").record_success("old-key", {"note_ids": [123]})

    # Delete bob through the real admin route.
    web_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )
    web_client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "root"))
    page = await web_client.get("/admin")
    csrf = CSRF_RE.search(page.text).group(1)
    res = await web_client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "bob", "confirm_username": "bob"},
    )
    assert res.status_code == 303

    # Serving state detached and BOTH directories archived (users/ + ledgers/).
    assert not registry.is_open("bob")
    assert not (tmp_path / "users" / "bob").exists()
    assert not (tmp_path / "ledgers" / "bob").exists()
    archived = list((tmp_path / "deleted").iterdir())
    assert len(archived) == 1 and archived[0].name.startswith("bob-")
    assert (archived[0] / "collection.anki2").exists()
    assert (archived[0] / "ledgers" / "ledger.json").exists()

    # The recovery flow: recreate the SAME username. It must start empty.
    _, new_bob_token = app.state.anki_store.create_user("bob", "bob2-pw")

    # Portal deck stats for the recreated account show an empty collection.
    web_client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "bob"))
    portal = await web_client.get("/")
    assert portal.status_code == 200
    assert "old-bob-secret" not in portal.text
    assert "<tr><td>Default</td><td>0</td><td>0</td><td>0</td></tr>" in portal.text

    # MCP add_notes for the recreated account writes under users/bob (a FRESH
    # collection), never into the archived deleted/ file.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as mcp_client:
            headers = await _init_session(mcp_client, new_bob_token)
            payload = await _call_tool(mcp_client, headers, "add_notes", {
                "notes": [{
                    "deck_name": "Default",
                    "model_name": "Basic",
                    "fields": {"Front": "new-bob-note", "Back": "new"},
                }],
            }, request_id=11)
            added = json.loads(payload["result"]["content"][0]["text"])
            assert added["success"] is True
            assert os.path.isfile(str(tmp_path / "users" / "bob" / "collection.anki2"))

            # The new account sees exactly its own note — never the old one.
            search = json.loads((await _call_tool(
                mcp_client, headers, "search_notes", {"query": "old-bob-secret"}, request_id=12
            ))["result"]["content"][0]["text"])
            assert search["total_matches"] == 0

    # Portal now reflects the new account's own single note.
    portal = await web_client.get("/")
    assert "<tr><td>Default</td><td>1</td><td>1</td><td>0</td></tr>" in portal.text
    assert "old-bob-secret" not in portal.text

    # Old ledger keys are gone for the recreated account.
    assert await registry.ledger("bob").get("old-key") is None

    # Restart simulation: the "old process" goes away — release its open
    # collection handles first (the anki backend refuses a second open of the
    # same file within one process, so the fresh registry below could not
    # open users/bob while the app still holds it).
    await registry.close_all()

    # A FRESH CollectionRegistry over the same data dir must not reload the
    # deleted account's collection or ledger entries.
    fresh_registry = CollectionRegistry(
        data_dir=str(tmp_path),
        idle_timeout_minutes=15,
        max_open_collections=5,
        max_concurrent_syncs=2,
        backup_retention=5,
    )
    try:
        def count_old(col):
            return len(col.find_notes("old-bob-secret"))

        assert await fresh_registry.run("bob", count_old) == 0
        assert await fresh_registry.ledger("bob").get("old-key") is None
    finally:
        await fresh_registry.close_all()
    await web_client.aclose()


@pytest.mark.asyncio
async def test_delete_user_without_ledger_dir_skips_ledger_archive(tmp_path):
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    store.create_user("root", "root-pw", role="admin")
    store.create_user("alice", "alice-pw")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    app = create_app(settings)
    web_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )
    web_client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "root"))
    page = await web_client.get("/admin")
    csrf = CSRF_RE.search(page.text).group(1)
    res = await web_client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "alice", "confirm_username": "alice"},
    )
    assert res.status_code == 303
    # Read from disk via a fresh store: create_app rebinds the module-global
    # store instance, so the pre-app `store` object above is stale.
    assert UserStore(users_file).get("alice") is None
    # Neither users/alice nor ledgers/alice existed: deleted/ is not created.
    assert not (tmp_path / "deleted").exists()
    await web_client.aclose()


def test_archive_ledger_dir_moves_into_the_account_archive(tmp_path):
    ledger_dir = tmp_path / "ledgers" / "bob"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "ledger.json").write_text("{}", encoding="utf-8")

    archive = tmp_path / "deleted" / "bob-1700000000"
    archive.mkdir(parents=True)
    dest = _archive_ledger_dir(str(tmp_path), "bob", str(archive))
    assert dest == str(archive / "ledgers")
    assert (archive / "ledgers" / "ledger.json").exists()
    assert not ledger_dir.exists()

    # Absent ledger dir: skip silently, create nothing.
    assert _archive_ledger_dir(str(tmp_path), "ghost", None) is None

    # No archive dir given (user had no data dir): one is created for it.
    ledger_dir2 = tmp_path / "ledgers" / "carol"
    ledger_dir2.mkdir(parents=True)
    (ledger_dir2 / "ledger.json").write_text("{}", encoding="utf-8")
    dest2 = _archive_ledger_dir(str(tmp_path), "carol", None)
    assert dest2 is not None and (tmp_path / "deleted").is_dir()
    assert os.path.basename(os.path.dirname(dest2)).startswith("carol-")
    assert os.path.isfile(os.path.join(dest2, "ledger.json"))
    assert not ledger_dir2.exists()
