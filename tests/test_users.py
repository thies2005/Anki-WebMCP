"""Unit tests for the multi-user account store (src/users.py)."""

import hashlib
import json
import os
import stat
import time

import pytest

from src.users import (
    ROLES,
    SCRYPT_N,
    USERNAME_RE,
    User,
    UserStore,
    hash_password,
    verify_password_hash,
)


@pytest.fixture
def users_file(tmp_path):
    """Path to a per-test users.json inside a not-yet-created directory.

    Deliberately exercises UserStore's directory creation on first write.
    """
    return str(tmp_path / "data" / "users.json")


# -- username validation -----------------------------------------------------


def test_username_regex_accepts_lowercase_digits_hyphen_underscore():
    assert USERNAME_RE.fullmatch("a-b_2")
    assert USERNAME_RE.fullmatch("alice")
    assert USERNAME_RE.fullmatch("a" * 32)


@pytest.mark.parametrize("bad", ["Alice", "../etc", "..", "a/b", "a b", "a" * 33, "", "álice"])
def test_username_regex_rejects_unsafe_names(bad):
    assert not USERNAME_RE.fullmatch(bad)


@pytest.mark.parametrize("bad", ["Alice", "../etc", "a" * 33, ""])
def test_create_user_rejects_invalid_username(users_file, bad):
    store = UserStore(users_file)
    with pytest.raises(ValueError, match="Invalid username"):
        store.create_user(bad, "pw")


# -- password hashing helpers ------------------------------------------------


def test_hash_password_format():
    encoded = hash_password("s3cret")
    prefix, n_str, r_str, p_str, salt_hex, hash_hex = encoded.split("$")
    assert prefix == "scrypt"
    assert int(n_str) == SCRYPT_N
    assert int(r_str) == 8
    assert int(p_str) == 1
    assert len(bytes.fromhex(salt_hex)) == 16
    assert len(bytes.fromhex(hash_hex)) == 64


def test_hash_password_salts_are_unique():
    assert hash_password("same") != hash_password("same")


def test_verify_password_hash_roundtrip():
    encoded = hash_password("pw")
    assert verify_password_hash("pw", encoded) is True
    assert verify_password_hash("wrong", encoded) is False


def test_verify_password_hash_malformed_encoding_is_false_not_raise():
    assert verify_password_hash("pw", "garbage") is False
    assert verify_password_hash("pw", "argon2$x$y") is False
    assert verify_password_hash("pw", "") is False


# -- create / verify / authenticate ------------------------------------------


def test_create_user_verify_password_and_token_auth(users_file):
    store = UserStore(users_file)
    user, raw = store.create_user("alice", "pw123", "admin")

    assert isinstance(user, User)
    assert user.username == "alice"
    assert user.role == "admin"
    assert store.verify_password("alice", "pw123") is True
    assert store.verify_password("alice", "wrong") is False
    assert store.verify_password("nobody-here", "pw123") is False

    got = store.authenticate_token(raw)
    assert got is not None
    assert got.username == "alice"
    assert store.authenticate_token("wrong-token") is None
    assert store.authenticate_token("") is None


def test_create_user_rejects_duplicate_username(users_file):
    store = UserStore(users_file)
    store.create_user("bob", "pw1")
    with pytest.raises(ValueError, match="already exists"):
        store.create_user("bob", "pw2")


def test_create_user_rejects_invalid_role(users_file):
    store = UserStore(users_file)
    with pytest.raises(ValueError, match="Invalid role"):
        store.create_user("carol", "pw", "superadmin")
    assert ROLES == ("admin", "user")


def test_only_token_hash_is_stored_ever(users_file):
    store = UserStore(users_file)
    user, raw = store.create_user("dave", "plain-pw")

    # The sha256 of the raw token is what's stored, never the raw value...
    assert user.token_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert user.token_hash != raw
    with open(users_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert raw not in content
    assert "plain-pw" not in content  # ...and never the password either
    stored = json.loads(content)
    assert set(stored["dave"]) >= {"username", "password_hash", "token_hash", "role", "created_at"}


# -- mutations ---------------------------------------------------------------


def test_regenerate_token_invalidates_old(users_file):
    store = UserStore(users_file)
    _, old = store.create_user("erin", "pw")
    new = store.regenerate_token("erin")
    assert new != old
    assert store.authenticate_token(old) is None
    assert store.authenticate_token(new) is not None


def test_regenerate_token_unknown_user_raises(users_file):
    store = UserStore(users_file)
    with pytest.raises(ValueError, match="Unknown username"):
        store.regenerate_token("ghost")


def test_set_password_invalidates_old(users_file):
    store = UserStore(users_file)
    store.create_user("frank", "old-pw")
    assert store.verify_password("frank", "old-pw") is True
    store.set_password("frank", "new-pw")
    assert store.verify_password("frank", "old-pw") is False
    assert store.verify_password("frank", "new-pw") is True


def test_set_password_unknown_user_raises(users_file):
    store = UserStore(users_file)
    with pytest.raises(ValueError, match="Unknown username"):
        store.set_password("ghost", "pw")


# -- session revocation epoch ------------------------------------------------


def test_new_user_epochs_start_at_creation(users_file):
    # Audit F-01: a fresh record starts with BOTH revocation epochs AT its
    # creation instant, so cookies/grants minted for a previous account that
    # reused the username are dead on arrival. (Records written before the
    # fields existed still load tolerantly at 0.0 — see the legacy test.)
    store = UserStore(users_file)
    store.create_user("kay", "pw")
    user = store.get("kay")
    assert user.sessions_invalid_before == user.created_at
    assert user.tokens_invalid_before == user.created_at


def test_legacy_record_without_epoch_field_loads_tolerantly(users_file):
    # A users.json written before the field existed must load with epoch 0.0
    # (no revocation), not raise or reset the store.
    os.makedirs(os.path.dirname(users_file), exist_ok=True)
    legacy = {
        "kay": {
            "username": "kay",
            "password_hash": hash_password("pw"),
            "token_hash": "a" * 64,
            "role": "user",
            "created_at": 123.0,
        }
    }
    with open(users_file, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    store = UserStore(users_file)
    user = store.get("kay")
    assert user is not None
    assert user.sessions_invalid_before == 0.0
    assert user.created_at == 123.0
    assert store.verify_password("kay", "pw") is True


def test_set_password_bumps_session_epoch(users_file):
    store = UserStore(users_file)
    store.create_user("lem", "pw")
    # Fresh records start at their creation instant (audit F-01), not 0.0.
    assert store.get("lem").sessions_invalid_before == store.get("lem").created_at
    before = time.time()
    store.set_password("lem", "pw2")
    epoch = store.get("lem").sessions_invalid_before
    # A cookie issued before the reset (any issued_at < epoch) must be
    # rejected; the epoch is stamped at/after the reset moment.
    assert epoch >= before
    assert epoch <= time.time()


def test_session_epoch_persists_across_reload(users_file):
    store = UserStore(users_file)
    store.create_user("moe", "pw")
    store.set_password("moe", "pw2")
    epoch = store.get("moe").sessions_invalid_before
    reloaded = UserStore(users_file).get("moe")
    assert reloaded.sessions_invalid_before == epoch


def test_other_mutations_do_not_bump_session_epoch(users_file):
    # Only a password reset evicts sessions; token regeneration and credential
    # storage must not log the user out of the web portal.
    store = UserStore(users_file)
    store.create_user("nan", "pw")
    store.regenerate_token("nan")
    store.set_ankiweb_credentials("nan", "nan@example.com", "anki-pw")
    store.set_role("nan", "admin")
    # Still at its creation-time initial value: none of these bumps the epoch.
    assert store.get("nan").sessions_invalid_before == store.get("nan").created_at


def test_set_ankiweb_credentials_round_trip(users_file):
    store = UserStore(users_file)
    store.create_user("gina", "pw")
    store.set_ankiweb_credentials("gina", "gina@example.com", "ankiweb-secret")
    user = store.list_users()[0]
    assert user.ankiweb_email == "gina@example.com"
    assert user.ankiweb_password == "ankiweb-secret"
    # Credentials survive a reload from disk.
    reloaded = UserStore(users_file).list_users()[0]
    assert reloaded.ankiweb_email == "gina@example.com"
    assert reloaded.ankiweb_password == "ankiweb-secret"


def test_delete_user_removes_record_and_kills_token(users_file):
    store = UserStore(users_file)
    _, raw = store.create_user("henry", "pw")
    assert store.delete_user("henry") is True
    assert store.delete_user("henry") is False
    assert store.authenticate_token(raw) is None
    assert store.list_users() == []


# -- last-admin guards under the store lock -----------------------------------


def test_set_role_refuses_demoting_last_admin(users_file):
    store = UserStore(users_file)
    store.create_user("root", "pw", "admin")
    with pytest.raises(ValueError, match="last admin"):
        store.set_role("root", "user")
    assert store.get("root").role == "admin"  # mutation did not commit
    # With a second admin the same demotion goes through.
    store.create_user("alice", "pw", "admin")
    store.set_role("root", "user")
    assert store.get("root").role == "user"
    assert store.get("alice").role == "admin"


def test_delete_user_refuses_deleting_last_admin(users_file):
    store = UserStore(users_file)
    store.create_user("root", "pw", "admin")
    with pytest.raises(ValueError, match="last admin"):
        store.delete_user("root")
    assert store.get("root") is not None  # mutation did not commit
    # Deleting a plain user while an admin remains is unaffected.
    store.create_user("bob", "pw")
    assert store.delete_user("bob") is True


def test_delete_last_admin_guard_sees_concurrent_role_changes(users_file):
    # The guard must count admins from the CURRENT in-lock state, not a stale
    # copy: promote a second admin, then deleting the first must succeed even
    # if an old list_users() snapshot showed only one admin.
    store = UserStore(users_file)
    store.create_user("root", "pw", "admin")
    store.create_user("alice", "pw")
    snapshot = store.list_users()  # stale: alice still 'user' here
    store.set_role("alice", "admin")
    assert sum(1 for u in snapshot if u.role == "admin") == 1
    assert store.delete_user("root") is True
    assert store.get("root") is None
    assert store.get("alice").role == "admin"


# -- persistence -------------------------------------------------------------


def test_missing_file_is_empty_store_and_creates_nothing(users_file):
    store = UserStore(users_file)
    assert store.list_users() == []
    assert not os.path.exists(users_file)


def test_persistence_round_trip_file_mode_and_json(users_file):
    store = UserStore(users_file)
    _, raw = store.create_user("iris", "pw", "admin")

    # Atomic write via mkstemp + os.replace leaves a 0o600 file.
    assert stat.S_IMODE(os.stat(users_file).st_mode) == 0o600

    with open(users_file, "r", encoding="utf-8") as f:
        data = json.load(f)  # valid JSON
    assert "iris" in data

    # A fresh store instance on the same path sees the account.
    store2 = UserStore(users_file)
    assert store2.verify_password("iris", "pw") is True
    assert store2.authenticate_token(raw) is not None

    # Mode stays 0o600 after subsequent atomic rewrites.
    store2.set_password("iris", "pw2")
    assert stat.S_IMODE(os.stat(users_file).st_mode) == 0o600


def test_corrupt_store_raises_instead_of_silently_resetting(users_file):
    store = UserStore(users_file)
    store.create_user("jack", "pw")
    with open(users_file, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(ValueError, match="corrupt"):
        UserStore(users_file)
    # The existing accounts were not wiped.
    with open(users_file, "r", encoding="utf-8") as f:
        assert "{not json" in f.read()


# -- bootstrap ---------------------------------------------------------------


def test_bootstrap_admin_creates_then_noops(users_file):
    store = UserStore(users_file)
    assert store.bootstrap_admin("admin", "boot-pw") is True
    # Second call is a no-op even with different credentials.
    assert store.bootstrap_admin("root", "other-pw") is False

    users = store.list_users()
    assert len(users) == 1
    assert users[0].username == "admin"
    assert users[0].role == "admin"
    assert store.verify_password("admin", "boot-pw") is True
    assert store.verify_password("root", "other-pw") is False

    # The no-op decision is persisted, not just in-memory.
    assert len(UserStore(users_file).list_users()) == 1


def test_bootstrap_admin_rejects_invalid_username_on_empty_store(users_file):
    store = UserStore(users_file)
    with pytest.raises(ValueError, match="Invalid username"):
        store.bootstrap_admin("Not Valid", "pw")
    assert store.list_users() == []
