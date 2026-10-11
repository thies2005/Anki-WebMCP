"""Tests for the admin console (Task 4): user list, create user, per-user
actions (reset password / regenerate token / set role / delete with typed
confirmation + data-dir archiving).

Route tests use the httpx ASGITransport pattern (in-process app, no network),
with the session cookie placed directly in the client jar so responses can
set/clear cookies (flash) naturally.
"""

import re
import threading

import httpx
import pytest

# src.main pulls anki.collection first — the wheel's anki.notes cannot be the
# FIRST anki module imported (circular import through anki.cards).
from src.config import Settings
from src.main import create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_session

SECRET = "test-session-secret"
ROOT_PASSWORD = "root-pw"


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("root", ROOT_PASSWORD, role="admin")
    store.create_user("alice", "alice-pw")
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


def _login(client, username):
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
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_redirects_unauthenticated(client):
    res = await client.get("/admin")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_admin_forbids_non_admin_with_403_page(client):
    _login(client, "alice")
    res = await client.get("/admin")
    assert res.status_code == 403
    assert "Admin access required." in res.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/admin/create-user",
        "/admin/reset-password",
        "/admin/regenerate-token",
        "/admin/set-role",
        "/admin/delete-user",
    ],
)
async def test_admin_mutations_forbid_non_admin_even_with_valid_csrf(client, path):
    # Close the loop from the review: role gating must hold on the POST
    # mutations themselves, not only on the GET page.
    _login(client, "alice")
    session = client.cookies.get(SESSION_COOKIE_NAME, "")
    from src.webauth import issue_csrf

    res = await client.post(path, data={"csrf_token": issue_csrf(session)})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_page_lists_users_and_links_to_portal(client):
    _login(client, "root")
    res = await client.get("/admin")
    assert res.status_code == 200
    # User list: username, role, created date, credentials + token flags (no secrets).
    assert "<td>alice</td>" in res.text and "<td>root</td>" in res.text
    assert "<td>admin</td>" in res.text and "<td>user</td>" in res.text
    assert "not set" in res.text  # AnkiWeb credentials column
    assert "<td>yes</td>" in res.text  # token column
    assert 'action="/admin/create-user"' in res.text
    # Admins are also users: a link back to their own portal.
    assert '<a href="/">Portal</a>' in res.text


@pytest.mark.asyncio
async def test_admin_link_hidden_from_regular_user(client):
    _login(client, "alice")
    res = await client.get("/")
    assert res.status_code == 200
    assert 'href="/admin"' not in res.text


# ---------------------------------------------------------------------------
# Create user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_user_flash_raw_token_once_and_new_login_works(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)

    res = await client.post(
        "/admin/create-user",
        data={"csrf_token": csrf, "username": "bob", "password": "bob-pw", "role": "user"},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/admin"

    # The raw token is flashed once for out-of-band handover.
    shown = await client.get("/admin")
    match = re.search(r'<pre id="flash-token">([^<]+)</pre>', shown.text)
    assert match, "admin must see the new user's raw token once"
    raw_token = match.group(1)
    assert (await client.get("/admin")).text.count(raw_token) == 0  # one-shot

    disk_store = await _fresh_store(tmp_path)
    assert disk_store.authenticate_token(raw_token).username == "bob"

    # The new account can actually log in through the web form.
    login = await client.post("/login", data={"username": "bob", "password": "bob-pw"})
    assert login.status_code == 303
    assert login.headers["location"] == "/"

    # bob now appears in the admin list (viewed as root again: bob is a plain user).
    _login(client, "root")
    listing = await client.get("/admin")
    assert "<td>bob</td>" in listing.text


@pytest.mark.asyncio
async def test_create_duplicate_username_rejected(client):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/create-user",
        data={"csrf_token": csrf, "username": "alice", "password": "x", "role": "user"},
    )
    assert res.status_code == 400
    assert "Could not create user" in res.text


@pytest.mark.asyncio
async def test_create_user_with_empty_password_rejected(client):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/create-user",
        data={"csrf_token": csrf, "username": "bob", "password": "", "role": "user"},
    )
    assert res.status_code == 400
    assert "Password must not be empty." in res.text


# ---------------------------------------------------------------------------
# Delete user (typed confirmation + archive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_without_typed_username_rejected(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)

    for confirm_value in ("", "bob", "Bob"):  # empty, wrong, wrong case
        res = await client.post(
            "/admin/delete-user",
            data={
                "csrf_token": csrf,
                "username": "alice",
                "confirm_username": confirm_value,
            },
        )
        assert res.status_code == 400
        assert "Deletion not confirmed" in res.text

    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("alice") is not None


@pytest.mark.asyncio
async def test_delete_with_typed_username_archives_data_dir(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)

    # bob must exist in the APP's store snapshot: create him through the app.
    res = await client.post(
        "/admin/create-user",
        data={"csrf_token": csrf, "username": "bob", "password": "bob-pw", "role": "user"},
    )
    assert res.status_code == 303
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)

    # Simulate a user who already has a data directory.
    user_dir = tmp_path / "users" / "bob"
    user_dir.mkdir(parents=True)
    (user_dir / "collection.anki2").write_bytes(b"pretend-collection")

    res = await client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "bob", "confirm_username": "bob"},
    )
    assert res.status_code == 303

    # Record gone from the persisted store...
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("bob") is None

    # ...and the data dir renamed under deleted/, never removed.
    users_dir = tmp_path / "users"
    assert not (users_dir / "bob").exists()
    deleted_root = tmp_path / "deleted"
    assert deleted_root.is_dir()
    archived = list(deleted_root.iterdir())
    assert len(archived) == 1 and archived[0].name.startswith("bob-")
    assert (archived[0] / "collection.anki2").read_bytes() == b"pretend-collection"

    notice = await client.get("/admin")
    assert "Deleted bob." in notice.text
    assert "deleted/" in notice.text


@pytest.mark.asyncio
async def test_delete_user_without_data_dir_still_deletes_record(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "alice", "confirm_username": "alice"},
    )
    assert res.status_code == 303
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("alice") is None
    assert not (tmp_path / "deleted").exists()  # nothing to archive, nothing created


@pytest.mark.asyncio
async def test_delete_unknown_user_rejected(client):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "ghost", "confirm_username": "ghost"},
    )
    assert res.status_code == 400
    assert "Unknown user" in res.text


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_invalidates_old_and_web_login_works(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/reset-password",
        data={"csrf_token": csrf, "username": "alice", "new_password": "brand-new-pw"},
    )
    assert res.status_code == 303

    disk_store = await _fresh_store(tmp_path)
    assert disk_store.verify_password("alice", "alice-pw") is False
    assert disk_store.verify_password("alice", "brand-new-pw") is True

    # Old password fails the web login; the new one works.
    # (root's session still sits in the jar; logout not needed for /login POST.)
    old_login = await client.post("/login", data={"username": "alice", "password": "alice-pw"})
    assert old_login.status_code == 401
    new_login = await client.post("/login", data={"username": "alice", "password": "brand-new-pw"})
    assert new_login.status_code == 303


@pytest.mark.asyncio
async def test_reset_password_with_empty_value_rejected(client):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/reset-password",
        data={"csrf_token": csrf, "username": "alice", "new_password": ""},
    )
    assert res.status_code == 400
    assert "New password must not be empty." in res.text


# ---------------------------------------------------------------------------
# Regenerate token (admin-initiated) and set role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_regenerate_token_invalidates_old(client, tmp_path):
    disk_store = await _fresh_store(tmp_path)
    old_token = disk_store.peek_token("alice")

    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/regenerate-token",
        data={"csrf_token": csrf, "username": "alice"},
    )
    assert res.status_code == 303

    shown = await client.get("/admin")
    new_token = re.search(r'<pre id="flash-token">([^<]+)</pre>', shown.text).group(1)

    disk_store = await _fresh_store(tmp_path)
    assert disk_store.authenticate_token(old_token) is None
    assert disk_store.authenticate_token(new_token).username == "alice"


@pytest.mark.asyncio
async def test_set_role_toggles_admin_access(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)

    # Promote alice to admin: she gains /admin access.
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "alice", "role": "admin"}
    )
    assert res.status_code == 303
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("alice").role == "admin"
    _login(client, "alice")
    assert (await client.get("/admin")).status_code == 200

    # Demote back: /admin access is revoked immediately.
    page = await client.get("/")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "alice", "role": "user"}
    )
    assert res.status_code == 303
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("alice").role == "user"
    assert (await client.get("/admin")).status_code == 403


@pytest.mark.asyncio
async def test_set_role_with_invalid_role_rejected(client):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "alice", "role": "superuser"}
    )
    assert res.status_code == 400
    assert "Could not set role" in res.text


# ---------------------------------------------------------------------------
# Last-admin guard (final review): demoting or deleting the sole admin is a
# one-misclick total lockout of the admin surface — the routes must refuse.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_demote_last_admin(client):
    _login(client, "root")  # root is the only admin in the fixture store
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "root", "role": "user"}
    )
    assert res.status_code == 400
    assert "last admin" in res.text


@pytest.mark.asyncio
async def test_cannot_delete_last_admin(client, tmp_path):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/delete-user",
        data={
            "csrf_token": csrf,
            "username": "root",
            "confirm_username": "root",
        },
    )
    assert res.status_code == 400
    assert "last admin" in res.text
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("root") is not None


@pytest.mark.asyncio
async def test_demoting_deleting_non_last_admin_still_works(client, tmp_path):
    _login(client, "root")
    # Promote alice so root is no longer the last admin.
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "alice", "role": "admin"}
    )
    assert res.status_code == 303

    # Now demoting root must succeed.
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "root", "role": "user"}
    )
    assert res.status_code == 303
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("root").role == "user"
    assert disk_store.get("alice").role == "admin"

    # Deleting a non-last admin must succeed too. Re-login as alice (the only
    # admin now — root was just demoted), promote root back, then delete alice.
    _login(client, "alice")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/set-role", data={"csrf_token": csrf, "username": "root", "role": "admin"}
    )
    assert res.status_code == 303
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/delete-user",
        data={"csrf_token": csrf, "username": "alice", "confirm_username": "alice"},
    )
    assert res.status_code == 303
    disk_store = await _fresh_store(tmp_path)
    assert disk_store.get("alice") is None
    assert disk_store.get("root").role == "admin"


# ---------------------------------------------------------------------------
# Store-lock last-admin guard (audit regression): the guard must be ATOMIC
# with the mutation. Two concurrent set_role demotions targeting two admins
# must leave exactly one admin — previously the guard ran on unlocked store
# copies outside the commit lock, so both demotions could pass and commit,
# leaving zero admins (unadministerable web surface).
# ---------------------------------------------------------------------------


def test_concurrent_admin_demotions_leave_exactly_one_admin(tmp_path):
    for round_no in range(5):
        store = UserStore(str(tmp_path / f"users-race-{round_no}.json"))
        store.create_user("root", "pw", role="admin")
        store.create_user("alice", "pw", role="admin")

        barrier = threading.Barrier(2)  # maximize scheduling overlap
        refused = []

        def demote(name: str) -> None:
            barrier.wait()
            try:
                store.set_role(name, "user")
            except ValueError as e:
                refused.append(str(e))

        threads = [threading.Thread(target=demote, args=("root",)),
                   threading.Thread(target=demote, args=("alice",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one demotion committed; the other was refused by the guard
        # that re-checked the admin count under the store lock.
        admins = [u.username for u in store.list_users() if u.role == "admin"]
        assert len(admins) == 1, f"round {round_no}: admins left: {admins}"
        assert len(refused) == 1, f"round {round_no}: refusals: {refused}"
        assert "last admin" in refused[0]
        assert admins[0] in ("root", "alice")
