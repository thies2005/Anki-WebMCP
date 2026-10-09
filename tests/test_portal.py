"""Tests for the token portal (Task 4): GET /, token display + regeneration,
AnkiWeb credentials, sync-now, flash messages, and the CSRF sweep.

Route tests use the httpx ASGITransport pattern (in-process app, no network),
with the session cookie placed directly in the client jar so responses can
set/clear cookies (flash) naturally.
"""

import json
import os
import re

import httpx
import pytest

# src.main pulls anki.collection first — the wheel's anki.notes cannot be the
# FIRST anki module imported (circular import through anki.cards).
from src.config import Settings
from src.main import _collect_deck_stats, _mask_email, _mask_token, create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_csrf, issue_session
from src.webflash import FLASH_COOKIE_NAME, issue_flash, verify_flash

SECRET = "test-session-secret"
ALICE_PASSWORD = "correct-horse"


# ---------------------------------------------------------------------------
# webflash primitives
# ---------------------------------------------------------------------------


def test_flash_round_trip():
    value = issue_flash(SECRET, "AnkiWeb credentials saved.", kind="success")
    assert verify_flash(SECRET, value) == ("success", "AnkiWeb credentials saved.")


def test_flash_rejects_tampering_and_wrong_secret():
    value = issue_flash(SECRET, "hello")
    tampered = value.replace("a", "b", 1) if "a" in value else value[:-1] + "A"
    assert verify_flash(SECRET, tampered) is None
    assert verify_flash("other-secret", value) is None
    assert verify_flash(SECRET, "garbage") is None
    assert verify_flash(SECRET, "") is None


def test_flash_expired_is_rejected():
    value = issue_flash(SECRET, "hello", ttl_seconds=-1)
    assert verify_flash(SECRET, value) is None


def test_flash_rejects_unknown_kind_and_overlong_message():
    with pytest.raises(ValueError):
        issue_flash(SECRET, "hello", kind="bogus")
    with pytest.raises(ValueError):
        issue_flash(SECRET, "x" * 801)


def test_mask_token_uses_first_and_last_four():
    assert _mask_token("abcdefghijklmnop") == "abcd…mnop"
    assert _mask_token("short") == "…"
    assert _mask_token(None) == ""


def test_mask_email_reveals_only_first_local_char():
    assert _mask_email("user@example.com") == "u***@example.com"
    assert _mask_email("@example.com") == "***@example.com"
    assert _mask_email("no-at-sign") == ""
    assert _mask_email("") == ""


# ---------------------------------------------------------------------------
# Route fixtures
# ---------------------------------------------------------------------------


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(users_file),
    )


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


def _login(client, username="alice"):
    """Replace the jar's session cookie (clearing any server-set one first)."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, username))


def _csrf_from(html_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', html_text)
    assert match, "page must carry a CSRF token"
    return match.group(1)


async def _fresh_store(tmp_path) -> UserStore:
    """A new store instance reading what the app persisted to disk."""
    return UserStore(str(tmp_path / "users.json"))


# ---------------------------------------------------------------------------
# Portal page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_redirects_unauthenticated(client):
    res = await client.get("/")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_portal_shows_masked_token_deck_table_and_logout(client, tmp_path):
    _login(client)
    raw_token = (await _fresh_store(tmp_path)).peek_token("alice")
    assert raw_token
    res = await client.get("/")
    assert res.status_code == 200
    assert "Logged in as" in res.text and "alice" in res.text
    # Masked token: first 4 + last 4 chars of the RAW token, never the whole value.
    assert _mask_token(raw_token) in res.text
    assert raw_token not in res.text
    # Deck table headers and the sync/credentials sections.
    for header in ("<th>Deck</th>", "<th>Notes</th>", "<th>Cards</th>", "<th>Due</th>"):
        assert header in res.text
    assert "AnkiWeb credentials" in res.text
    assert "Sync now" in res.text
    assert "Regenerate token" in res.text
    assert '<form method="post" action="/logout">' in res.text


@pytest.mark.asyncio
async def test_portal_deck_stats_reflect_real_collection(client, tmp_path):
    from anki.notes import Note

    from src.main import collection_registry

    def add_note(col):
        note = Note(col, col.models.by_name("Basic"))
        note["Front"] = "hello"
        note["Back"] = "world"
        col.add_note(note, col.decks.id("Default"))

    _login(client)
    await collection_registry.run("alice", add_note)
    res = await client.get("/")
    assert res.status_code == 200
    # One Basic note in the Default deck: 1 note, 1 card, nothing time-due.
    assert "<tr><td>Default</td><td>1</td><td>1</td><td>0</td></tr>" in res.text
    assert "Due now: 1 new, 0 learning, 0 review." in res.text


@pytest.mark.asyncio
async def test_collect_deck_stats_on_fresh_collection(tmp_path):
    from src.collections import CollectionRegistry

    reg = CollectionRegistry(data_dir=str(tmp_path))
    try:
        stats = await reg.run("deckless", _collect_deck_stats)
        assert stats["decks"][0]["name"] == "Default"
        assert stats["decks"][0]["notes"] == 0
        assert stats["due"]["new"] == 0
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_portal_hides_token_for_legacy_record_without_plaintext(
    tmp_path
):
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    # Simulate a record created before token_plaintext existed.
    with open(users_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    del data["alice"]["token_plaintext"]
    with open(users_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    settings = Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(users_file),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "alice"))
        res = await client.get("/")
        assert res.status_code == 200
        assert "regenerate to view" in res.text


# ---------------------------------------------------------------------------
# Token regeneration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_rotates_token_and_reveals_it_once(client, tmp_path):
    _login(client)
    old_token = (await _fresh_store(tmp_path)).peek_token("alice")
    page = await client.get("/")
    csrf = _csrf_from(page.text)

    res = await client.post("/portal/regenerate-token", data={"csrf_token": csrf})
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    # The flash cookie is HttpOnly + strict, like the session cookie.
    set_cookie = res.headers["set-cookie"].lower()
    assert f"{FLASH_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie

    shown = await client.get("/")
    match = re.search(r'<pre id="flash-token">([^<]+)</pre>', shown.text)
    assert match, "the new raw token must be revealed once on the next GET"
    new_token = match.group(1)
    assert new_token != old_token

    # The reveal is one-shot: a second GET no longer shows it.
    again = await client.get("/")
    assert f'<pre id="flash-token">{new_token}</pre>' not in again.text

    # Old raw token stops authenticating; the new one authenticates.
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.authenticate_token(old_token) is None
    authenticated = disk_store.authenticate_token(new_token)
    assert authenticated is not None and authenticated.username == "alice"
    # peek_token now serves the new value (masked display stays accurate).
    assert disk_store.peek_token("alice") == new_token


@pytest.mark.asyncio
async def test_regenerated_token_displayed_masked_after_flash_is_gone(client, tmp_path):
    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    await client.post("/portal/regenerate-token", data={"csrf_token": csrf})
    shown = await client.get("/")
    new_token = re.search(r'<pre id="flash-token">([^<]+)</pre>', shown.text).group(1)
    later = await client.get("/")
    assert _mask_token(new_token) in later.text
    assert new_token not in later.text


# ---------------------------------------------------------------------------
# AnkiWeb credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_post_persists_and_masks_email(client, tmp_path):
    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/portal/credentials",
        data={"csrf_token": csrf, "email": "user@example.com", "password": "ankiweb-pw"},
    )
    assert res.status_code == 303

    disk_store = await _fresh_store(tmp_path)
    account = disk_store.get("alice")
    assert account.ankiweb_email == "user@example.com"
    assert account.ankiweb_password == "ankiweb-pw"  # persisted for the sync service

    portal = await client.get("/")
    assert "u***@example.com" in portal.text  # masked email shown
    # Neither the raw password nor the full email may appear.
    assert "ankiweb-pw" not in portal.text
    assert "user@example.com" not in portal.text


@pytest.mark.asyncio
async def test_credentials_post_requires_both_fields(client):
    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/portal/credentials",
        data={"csrf_token": csrf, "email": "user@example.com", "password": ""},
    )
    assert res.status_code == 303
    notice = await client.get("/")
    assert "Both AnkiWeb email and password are required." in notice.text


# ---------------------------------------------------------------------------
# Sync now
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_now_without_credentials_is_inline_notice_and_no_call(
    client, tmp_path, monkeypatch
):
    from src.main import collection_registry

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("sync_user must not be called without credentials")

    monkeypatch.setattr(collection_registry, "sync_user", _must_not_run)
    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post("/portal/sync", data={"csrf_token": csrf})
    assert res.status_code == 303
    notice = await client.get("/")
    assert "AnkiWeb credentials are not set" in notice.text


# ---------------------------------------------------------------------------
# CSRF sweep: every state-changing POST requires a valid token (400/403)
# ---------------------------------------------------------------------------

STATE_CHANGING_PATHS = [
    "/logout",
    "/portal/regenerate-token",
    "/portal/credentials",
    "/portal/sync",
    "/admin/create-user",
    "/admin/reset-password",
    "/admin/regenerate-token",
    "/admin/set-role",
    "/admin/delete-user",
]


@pytest.fixture
def admin_app(tmp_path):
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("root", "root-pw", role="admin")
    settings = Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(users_file),
    )
    return create_app(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", STATE_CHANGING_PATHS)
async def test_state_changing_posts_without_csrf_rejected(admin_app, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "root"))
        # Missing token...
        res = await client.post(path, data={})
        assert res.status_code == 403
        # ...and a wrong token.
        res = await client.post(path, data={"csrf_token": "0" * 64})
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_state_changing_posts_rejected_without_session(admin_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        for path in STATE_CHANGING_PATHS:
            res = await client.post(path, data={})
            assert res.status_code == 303
            assert res.headers["location"] == "/login"
