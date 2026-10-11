"""Tests for the portal's Version history routes (version-history stage):
POST /portal/backup/create, GET /portal/backup/confirm, POST
/portal/backup/restore, and GET /portal/backup/download.

Route tests use the httpx ASGITransport pattern (in-process app, no network),
with the session cookie placed directly in the client jar so responses can
set/clear cookies (flash) naturally — the same pattern as test_portal.py.
Filename access control is exact string membership against the acting user's
own snapshot listing, so the tests pin: CSRF on every POST, traversal and
unknown names refused with 400, and another account's snapshot refused.
"""

import re

import httpx
import pytest

# src.main pulls anki.collection first — the wheel's anki.notes cannot be the
# FIRST anki module imported (circular import through anki.cards).
from src.config import Settings
from src.main import create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_session
# The shared form-login helper lives with the Wave B regression tests: POST
# /login requires the prelogin CSRF pair issued by GET /login.
from test_audit_fixes_b import login_via_form

SECRET = "test-session-secret"
ALICE_PASSWORD = "correct-horse"
BOB_PASSWORD = "bob-pw"


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    store.create_user("bob", BOB_PASSWORD)
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


def _add_note(col, front, back="back"):
    from anki.notes import Note

    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id("Default"))
    return note.id


def _count_notes(col):
    return len(col.find_notes(""))


# ---------------------------------------------------------------------------
# Version history section on the portal page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_history_empty_state_and_backup_form(client):
    _login(client)
    res = await client.get("/")
    assert res.status_code == 200
    assert "Version history" in res.text
    assert "No snapshots yet" in res.text
    assert "one is created before every sync and destructive change" in res.text
    # One "Back up now" POST form with a CSRF token at the section top.
    assert 'action="/portal/backup/create"' in res.text
    assert "Back up now" in res.text
    # The standing note: snapshots exclude media files.
    assert "exclude media files" in res.text


@pytest.mark.asyncio
async def test_backup_create_flash_and_row_listed(client):
    from src.main import collection_registry

    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post("/portal/backup/create", data={"csrf_token": csrf})
    assert res.status_code == 303
    assert res.headers["location"] == "/"

    entries = collection_registry.list_backups("alice")
    assert [entry["reason"] for entry in entries] == ["manual"]
    filename = entries[0]["filename"]
    assert filename.endswith("-manual.colpkg")

    shown = await client.get("/")
    assert f"Snapshot created: {filename}." in shown.text
    # The snapshot renders as a Version history row with both row actions.
    assert f'href="/portal/backup/confirm?file={filename}"' in shown.text
    assert f'href="/portal/backup/download?file={filename}"' in shown.text
    assert "<code>manual</code>" in shown.text


@pytest.mark.asyncio
async def test_version_history_caps_at_20_rows_with_older_line(client, tmp_path):
    backups_dir = tmp_path / "users" / "alice" / "backups"
    backups_dir.mkdir(parents=True)
    for second in range(22):  # 22 crafted snapshots; only 20 newest may render
        name = f"backup-2020-01-01-00.00.{second:02d}-manual.colpkg"
        (backups_dir / name).write_bytes(b"colpkg-bytes")

    _login(client)
    res = await client.get("/")
    assert res.status_code == 200
    assert res.text.count("<tr><td>2020-01-01-") == 20
    assert "+2 older" in res.text
    # Newest first: second 21 is visible, 00 and 01 collapsed into "+2 older".
    assert "backup-2020-01-01-00.00.21-manual.colpkg" in res.text
    assert "backup-2020-01-01-00.00.01-manual.colpkg" not in res.text


# ---------------------------------------------------------------------------
# Confirm page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_page_shows_snapshot_and_restore_form(client):
    from src.main import collection_registry

    snapshot = await collection_registry.backup_user("alice", "manual")
    entry = collection_registry.list_backups("alice")[0]
    assert entry["filename"] == snapshot

    _login(client)
    res = await client.get("/portal/backup/confirm", params={"file": snapshot})
    assert res.status_code == 200
    assert snapshot in res.text
    assert "<code>manual</code>" in res.text  # reason badge
    assert entry["timestamp"] in res.text  # raw timestamp group
    assert re.search(r">\d+(\.\d+)? (B|KB|MB)<", res.text)  # human-readable size
    assert "replaces the CURRENT collection" in res.text
    assert "pre-restore snapshot" in res.text
    assert "next AnkiWeb sync reconciles" in res.text
    # CSRF'd POST form with the filename hidden field.
    assert 'action="/portal/backup/restore"' in res.text
    assert f'name="file" value="{snapshot}"' in res.text
    restore_csrf = _csrf_from(res.text)
    assert re.fullmatch(r"[0-9a-f]{64}", restore_csrf)


# ---------------------------------------------------------------------------
# Restore round trip through the portal (logged in via the real form)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_round_trip_bring_notes_back(client):
    from src.main import collection_registry

    await collection_registry.run("alice", _add_note, "first")
    await collection_registry.run("alice", _add_note, "second")
    assert await collection_registry.run("alice", _count_notes) == 2

    login = await login_via_form(client, "alice", ALICE_PASSWORD)
    assert login.status_code == 303
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post("/portal/backup/create", data={"csrf_token": csrf})
    assert res.status_code == 303

    manual = [
        entry["filename"]
        for entry in collection_registry.list_backups("alice")
        if entry["reason"] == "manual"
    ]
    assert len(manual) == 1
    snapshot = manual[0]

    # Destroy the collection content AFTER the snapshot exists.
    await collection_registry.run("alice", lambda col: col.remove_notes(col.find_notes("")))
    assert await collection_registry.run("alice", _count_notes) == 0

    confirm = await client.get("/portal/backup/confirm", params={"file": snapshot})
    assert confirm.status_code == 200
    res = await client.post(
        "/portal/backup/restore",
        data={"csrf_token": _csrf_from(confirm.text), "file": snapshot},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/"

    shown = await client.get("/")
    assert f"Collection restored from {snapshot}" in shown.text
    # The pre-restore snapshot (the replaced state) is named in the flash.
    assert "kept as snapshot backup-" in shown.text
    pre_restore = re.search(r"kept as snapshot (backup-[0-9a-z.\-]+\.colpkg)", shown.text)
    assert pre_restore, "flash must name the automatic pre-restore snapshot"
    assert pre_restore.group(1) in [
        entry["filename"] for entry in collection_registry.list_backups("alice")
    ]

    # The deleted notes are back after the restore.
    assert await collection_registry.run("alice", _count_notes) == 2


# ---------------------------------------------------------------------------
# Refusals: CSRF, traversal/unknown names, other users' snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_posts_without_csrf_refused(client):
    _login(client)
    res = await client.post("/portal/backup/create", data={})
    assert res.status_code == 403
    res = await client.post("/portal/backup/restore", data={"file": "x"})
    assert res.status_code == 403
    # A wrong token is refused too.
    page = await client.get("/")
    res = await client.post(
        "/portal/backup/restore",
        data={"csrf_token": "0" * 64, "file": "x"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "..",
        "backup-2030-01-01-00.00.00-manual.colpkg",  # well-formed but unknown
    ],
)
async def test_backup_unknown_or_traversal_names_refused_400(client, bad):
    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    confirm = await client.get("/portal/backup/confirm", params={"file": bad})
    assert confirm.status_code == 400
    download = await client.get("/portal/backup/download", params={"file": bad})
    assert download.status_code == 400
    restore = await client.post(
        "/portal/backup/restore", data={"csrf_token": csrf, "file": bad}
    )
    assert restore.status_code == 400


@pytest.mark.asyncio
async def test_backup_routes_require_session(client):
    for request in (
        client.post("/portal/backup/create", data={}),
        client.get("/portal/backup/confirm", params={"file": "x"}),
        client.post("/portal/backup/restore", data={}),
        client.get("/portal/backup/download", params={"file": "x"}),
    ):
        res = await request
        assert res.status_code == 303
        assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_download_serves_snapshot_as_zip(client):
    from src.main import collection_registry

    snapshot = await collection_registry.backup_user("alice", "manual")
    _login(client)
    res = await client.get("/portal/backup/download", params={"file": snapshot})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert f'filename="{snapshot}"' in res.headers["content-disposition"]
    assert res.content[:2] == b"PK"  # a .colpkg IS a zip archive


@pytest.mark.asyncio
async def test_other_users_snapshot_refused_for_alice(client):
    from src.main import collection_registry

    bob_snapshot = await collection_registry.backup_user("bob", "manual")
    assert collection_registry.list_backups("alice") == []

    _login(client, "alice")
    page = await client.get("/")
    # bob's snapshot is not in alice's listing...
    assert bob_snapshot not in page.text
    csrf = _csrf_from(page.text)
    # ...so every route refuses it with 400 — never a download or a restore.
    confirm = await client.get("/portal/backup/confirm", params={"file": bob_snapshot})
    assert confirm.status_code == 400
    download = await client.get("/portal/backup/download", params={"file": bob_snapshot})
    assert download.status_code == 400
    restore = await client.post(
        "/portal/backup/restore",
        data={"csrf_token": csrf, "file": bob_snapshot},
    )
    assert restore.status_code == 400
    # bob's collection is untouched: he still has exactly his snapshot.
    assert [entry["filename"] for entry in collection_registry.list_backups("bob")] == [
        bob_snapshot
    ]


# ---------------------------------------------------------------------------
# Maintenance-mode gate (same contract as the sync button)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_create_and_restore_refused_in_maintenance_mode(client, monkeypatch):
    from src.main import collection_registry, sync_manager

    _login(client)
    page = await client.get("/")
    csrf = _csrf_from(page.text)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("registry must not be touched while in maintenance mode")

    monkeypatch.setattr(sync_manager, "_maintenance_mode", True)
    monkeypatch.setattr(collection_registry, "backup_user", _must_not_run)
    monkeypatch.setattr(collection_registry, "restore_backup", _must_not_run)

    created = await client.post("/portal/backup/create", data={"csrf_token": csrf})
    assert created.status_code == 303
    shown = await client.get("/")
    assert "Worker is currently in maintenance mode; writes are paused." in shown.text

    restored = await client.post(
        "/portal/backup/restore", data={"csrf_token": csrf, "file": "whatever.colpkg"}
    )
    assert restored.status_code == 303
    shown2 = await client.get("/")
    assert "Worker is currently in maintenance mode; writes are paused." in shown2.text
