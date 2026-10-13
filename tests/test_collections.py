"""Tests for the per-user collection registry (Task 3).

These tests use REAL anki collections in tmp directories (per the task brief:
no mocks). Each test builds its own CollectionRegistry against a fresh
tmp_path data_dir and closes everything on teardown.
"""

import asyncio
import json
import os
import time

import pytest
import pytest_asyncio

# src.collections imports anki.collection first; the wheel's anki.notes cannot
# be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry, _plan_sync_followup  # isort: skip
from anki.errors import InvalidInput
from anki.notes import Note


# -- helpers ------------------------------------------------------------------


def add_basic_note(col, front, back="back"):
    """Add a real note to the Default deck using the full notetype dict."""
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id("Default"))
    return note.id


def count_notes(col):
    return len(col.find_notes(""))


@pytest_asyncio.fixture
async def make_registry(tmp_path):
    """Factory for registries sharing one tmp data_dir; tears down all opens."""
    made = []

    def _make(**kwargs):
        reg = CollectionRegistry(data_dir=str(tmp_path), **kwargs)
        made.append(reg)
        return reg

    yield _make
    for reg in made:
        await reg.close_all()


# -- open-or-create -------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_or_create_builds_layout_and_fresh_collection_works(make_registry):
    reg = make_registry()

    assert await reg.run("alice", lambda col: col.find_notes("")) == []

    user_dir = os.path.join(reg.data_dir, "users", "alice")
    assert os.path.isfile(os.path.join(user_dir, "collection.anki2"))
    assert os.path.isdir(os.path.join(user_dir, "collection.media"))
    assert os.path.isdir(os.path.join(user_dir, "backups"))

    # A fresh collection ships the Default deck and the standard notetypes.
    decks = await reg.run("alice", lambda col: [d.name for d in col.decks.all_names_and_ids()])
    assert "Default" in decks
    models = await reg.run("alice", lambda col: col.models.all_names())
    assert "Basic" in models


# -- isolation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_collections_are_isolated(make_registry):
    reg = make_registry()

    note_id = await reg.run("alice", add_basic_note, "secret-question", "secret-answer")
    assert isinstance(note_id, int)

    assert await reg.run("bob", count_notes) == 0
    assert await reg.run("alice", count_notes) == 1


# -- serialization --------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_user_runs_serialize_without_lost_updates(make_registry):
    reg = make_registry()
    counter = {"n": 0}

    def bump(col):
        prev = counter["n"]
        time.sleep(0.001)  # widen the read-modify-write window
        counter["n"] = prev + 1
        return counter["n"]

    async def worker():
        for _ in range(15):
            await reg.run("carol", bump)

    # Without the per-user lock these 45 read-modify-writes would interleave
    # across worker threads and lose updates.
    await asyncio.gather(worker(), worker(), worker())
    assert counter["n"] == 45


@pytest.mark.asyncio
async def test_different_users_complete_concurrently(make_registry):
    reg = make_registry()

    async def add_for(user):
        return await reg.run(user, add_basic_note, f"front-{user}", "back")

    first, second = await asyncio.gather(add_for("dave"), add_for("eve"))
    assert isinstance(first, int)
    assert isinstance(second, int)
    assert await reg.run("dave", count_notes) == 1
    assert await reg.run("eve", count_notes) == 1


# -- LRU cap ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lru_cap_evicts_least_recently_used(make_registry):
    reg = make_registry(max_open_collections=2)

    await reg.run("a", count_notes)
    await reg.run("b", count_notes)
    assert reg.open_usernames() == ["a", "b"]

    await reg.run("c", count_notes)  # cap reached -> evict LRU user "a"
    assert reg.open_usernames() == ["b", "c"]
    assert not reg.is_open("a")

    await reg.run("a", count_notes)  # re-opening "a" evicts the new LRU, "b"
    assert reg.open_usernames() == ["c", "a"]

    # Eviction never loses data: "b" reopens from disk with its content intact.
    await reg.run("b", add_basic_note, "survivor", "back")
    await reg.run("c", count_notes)  # evict someone ("a" is LRU here)
    assert await reg.run("b", count_notes) == 1


# -- idle close -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_idle_closes_only_stale_users(make_registry):
    reg = make_registry(idle_timeout_minutes=1)
    await reg.run("stale", count_notes)
    await reg.run("fresh", count_notes)

    # Backdate one user's clock (tiny-timeout equivalent for a 1-minute floor).
    reg._users["stale"].last_used = time.time() - 2 * 3600

    assert await reg.close_idle() == ["stale"]
    assert not reg.is_open("stale")
    assert reg.is_open("fresh")
    assert await reg.close_idle() == []  # nothing left to close


@pytest.mark.asyncio
async def test_close_idle_skips_busy_users(make_registry):
    reg = make_registry(idle_timeout_minutes=1)
    await reg.run("busy", count_notes)
    reg._users["busy"].last_used = time.time() - 2 * 3600

    async with reg._users["busy"].lock:  # simulate an in-flight operation
        assert await reg.close_idle() == []
        assert reg.is_open("busy")
    assert await reg.close_idle() == ["busy"]


# -- stale-handle recovery ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_recovers_when_collection_closed_behind_registry_back(make_registry):
    reg = make_registry()
    await reg.run("frank", count_notes)

    reg._users["frank"].col.close()  # yank the handle behind the registry's back
    assert not reg.is_open("frank")

    assert await reg.run("frank", count_notes) == 0
    assert reg.is_open("frank")


@pytest.mark.asyncio
async def test_run_retries_once_after_stale_handle_failure(make_registry):
    reg = make_registry()
    calls = {"n": 0}

    def flaky(col):
        calls["n"] += 1
        if calls["n"] == 1:
            col.close()  # handle dies mid-operation
            return col.find_notes("")  # raises InvalidInput("CollectionNotOpen")
        return col.find_notes("")  # the retry succeeds on the fresh handle

    assert await reg.run("gina", flaky) == []
    assert calls["n"] == 2
    assert reg.is_open("gina")


@pytest.mark.asyncio
async def test_user_text_exception_on_healthy_handle_is_not_stale(make_registry):
    """An application exception whose TEXT mentions 'CollectionNotOpen' on a
    healthy handle must propagate unchanged: fn runs exactly once, the handle
    is NOT recycled, no retry fires (regression: the old substring fallback
    re-executed non-idempotent mutations and reported them as failed)."""
    reg = make_registry()
    await reg.run("hank", count_notes)
    col_before = reg._users["hank"].col
    calls = {"n": 0}

    def fn(col):
        calls["n"] += 1
        raise KeyError("CollectionNotOpen")

    with pytest.raises(KeyError):
        await reg.run("hank", fn)
    assert calls["n"] == 1  # no retry on a healthy handle
    assert reg._users["hank"].col is col_before  # handle not recycled


@pytest.mark.asyncio
async def test_genuinely_stale_handle_retries_once_then_serves_reopened_collection(make_registry):
    reg = make_registry()
    await reg.run("iris", count_notes)
    calls = {"n": 0}

    def read(col):
        calls["n"] += 1
        if calls["n"] == 1:
            col.close()  # underlying collection closed directly mid-operation
            return col.find_notes("")  # raises InvalidInput (EMPTY args, col.db is None)
        return col.find_notes("")  # the retry succeeds on the reopened handle

    assert await reg.run("iris", read) == []
    assert calls["n"] == 2  # exactly one retry on the authoritative db-is-None signal
    assert reg._users["iris"].col is not None
    assert reg._users["iris"].col.db is not None  # a genuinely reopened handle
    assert reg.is_open("iris")

    # The reopened collection serves subsequent calls normally.
    assert await reg.run("iris", count_notes) == 0


@pytest.mark.asyncio
async def test_handle_closed_directly_between_calls_is_replaced_before_fn_runs(make_registry):
    """A handle closed behind the registry's back between calls never reaches
    fn: _ensure_open sees col.db is None and swaps in a fresh handle, so the
    read fn runs exactly once on the reopened collection."""
    reg = make_registry()
    await reg.run("iris2", count_notes)
    closed_handle = reg._users["iris2"].col
    closed_handle.close()
    calls = {"n": 0}

    def read(col):
        calls["n"] += 1
        return col.find_notes("")

    assert await reg.run("iris2", read) == []
    assert calls["n"] == 1
    assert reg._users["iris2"].col is not closed_handle
    assert reg.is_open("iris2")


@pytest.mark.asyncio
async def test_is_stale_matches_only_backend_type_never_exception_text(make_registry):
    """The only healthy-handle fallback is the closed handle's EXACT backend
    signature: InvalidInput with EMPTY args AND str == "CollectionNotOpen".

    The pinned wheel raises InvalidInput with EMPTY args for a closed handle,
    but InvalidInput is also what a backend INPUT error on a healthy handle
    looks like (a bad find_and_replace regex arrives as InvalidInput whose
    message is a "<pre>regex parse error:..." diagnostic) — so bare type
    identity classified healthy handles as stale, closing/reopening them and
    re-executing the operation. And exception TEXT must otherwise never be
    matched: tool-layer messages can carry user-controlled model/field names,
    and text matching classified benign application errors (e.g.
    KeyError('CollectionNotOpen')) as stale."""
    reg = make_registry()
    await reg.run("hana", count_notes)
    healthy = reg._users["hana"].col
    assert healthy.db is not None

    # The closed handle's exact signature triggers the fallback.
    backend_error = InvalidInput("CollectionNotOpen", None, None, None)
    assert backend_error.args == ()  # wheel shape: EMPTY args, no text to match
    assert str(backend_error) == "CollectionNotOpen"
    assert CollectionRegistry._is_stale(healthy, backend_error) is True
    # A backend InvalidInput on a HEALTHY handle whose message is an input
    # diagnostic (bad regex), not the closed-handle marker: NOT stale — no
    # close/reopen, no re-execution of fn.
    input_error = InvalidInput("<pre>regex parse error:\n    (fo\nerror: unclosed group", None, None, None)
    assert input_error.args == ()  # same EMPTY-args shape as the closed handle
    assert str(input_error).startswith("<pre>regex parse error")
    assert CollectionRegistry._is_stale(healthy, input_error) is False
    # Application exceptions on a healthy handle: NEVER stale, whatever text.
    assert CollectionRegistry._is_stale(healthy, KeyError("CollectionNotOpen")) is False
    assert CollectionRegistry._is_stale(healthy, ValueError("backend says CollectionNotOpen")) is False

    # The handle check stays authoritative even for unrelated exception types.
    healthy.close()
    assert CollectionRegistry._is_stale(healthy, ValueError("unrelated")) is True


# -- per-user idempotency ledgers ---------------------------------------------------


@pytest.mark.asyncio
async def test_per_user_ledgers_are_separate_files(make_registry):
    reg = make_registry()

    led = reg.ledger("henry")
    assert reg.ledger("henry") is led  # cached per user

    await led.record_success("op-1", {"x": 1})
    henry_path = os.path.join(reg.data_dir, "ledgers", "henry", "ledger.json")
    assert os.path.isfile(henry_path)
    with open(henry_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["op-1"]["status"] == "committed"
    assert data["op-1"]["data"] == {"x": 1}

    await reg.ledger("ivy").record_success("op-2", {"y": 2})
    ivy_path = os.path.join(reg.data_dir, "ledgers", "ivy", "ledger.json")
    assert os.path.isfile(ivy_path)
    with open(henry_path, "r", encoding="utf-8") as f:
        assert "op-2" not in json.load(f)


# -- sync ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_user_without_credentials_fails_before_any_io(make_registry):
    reg = make_registry()

    for email, password in (("", ""), ("kate@example.com", "")):
        result = await reg.sync_user("kate", email, password)
        assert result["success"] is False
        assert result["error_type"] == "ValueError"
        assert "credentials not set" in result["error"]

    # Rejected before any network call, collection open, or directory creation.
    assert not os.path.exists(os.path.join(reg.data_dir, "users", "kate"))
    assert not reg.is_open("kate")


@pytest.mark.asyncio
async def test_sync_user_unreachable_endpoint_returns_structured_failure(make_registry):
    reg = make_registry()
    steps = []

    result = await reg.sync_user(
        "leo",
        "leo@example.com",
        "password",
        endpoint="http://127.0.0.1:9",  # discard port: nothing listens, fails fast
        on_step=steps.append,
    )

    assert result["success"] is False
    assert result["error"]  # non-empty message
    assert result["error_type"]  # exception type name present
    # Login-stage failure: nothing was synced, so the outcome is KNOWN and the
    # ambiguous-outcome flag must NOT be set.
    assert "outcome_unknown" not in result
    assert steps == ["backup", "login"]  # the sync step was never reached


@pytest.mark.asyncio
async def test_sync_user_acquire_timeout_skips_without_lock_backup_or_sync(make_registry):
    """acquire_timeout bounds ONLY the wait for a global sync slot: with the
    slot pool saturated, sync_user returns the TimeoutError failure quickly
    and takes no user lock, no backup, opens no collection — the pre-tool
    sync degrades to a skip instead of stalling a client's mutation behind
    someone else's transfer. The slot itself is neither leaked nor consumed."""
    reg = make_registry(max_concurrent_syncs=1)
    await reg._sync_semaphore.acquire()  # saturate the only slot
    try:
        started = time.monotonic()
        result = await reg.sync_user("tova", "tova@example.com", "pw", acquire_timeout=0.05)
        elapsed = time.monotonic() - started

        assert result["success"] is False
        assert result["error_type"] == "TimeoutError"
        assert result["error"] == "skipped: no sync slot free within 0.05s"
        assert elapsed < 5.0  # bounded wait, not an indefinite stall
        # Nothing happened: no collection opened, no user directory created,
        # hence no pre-sync backup either.
        assert not reg.is_open("tova")
        assert not os.path.exists(os.path.join(reg.data_dir, "users", "tova"))
    finally:
        reg._sync_semaphore.release()

    # The slot was released: a plain sync_user proceeds through the gate now
    # (login fails fast against the discard port — but AFTER the gate, with
    # the pre-sync backup taken, proving the earlier timeout consumed nothing).
    result = await reg.sync_user("tova", "tova@example.com", "pw", endpoint="http://127.0.0.1:9")
    assert result["success"] is False
    assert result["error_type"] != "TimeoutError"
    assert [entry["reason"] for entry in reg.list_backups("tova")] == ["pre-sync"]


# -- backup pruning --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_backups_keeps_only_newest_n(make_registry):
    reg = make_registry(backup_retention=3)
    backups_dir = os.path.join(reg.data_dir, "users", "mia", "backups")
    os.makedirs(backups_dir)
    for i in range(5):
        with open(os.path.join(backups_dir, f"backup-{i}.zst"), "w", encoding="utf-8") as f:
            f.write("x")

    kept = reg.prune_backups("mia")
    assert kept == ["backup-2.zst", "backup-3.zst", "backup-4.zst"]
    assert sorted(os.listdir(backups_dir)) == kept
    assert reg.prune_backups("mia") == kept  # idempotent


@pytest.mark.asyncio
async def test_prune_backups_tolerates_missing_directory(make_registry):
    reg = make_registry()
    assert reg.prune_backups("nobody") == []  # user never opened: no backups dir


@pytest.mark.asyncio
async def test_prune_backups_keeps_safety_snapshots_over_routine_churn(make_registry):
    """Retention is per CLASS: a destructive-op (SAFETY) snapshot survives the
    pre-sync churn that age-only pruning used to evict. With
    backup_retention=3, one pre-delete-notes file and five pre-sync files:
    the SAFETY file keeps its retention slot even as the OLDEST file on disk,
    and only the newest max(_ROUTINE_BACKUP_FLOOR, 3 - 1) = 2 ROUTINE files
    stay alongside it. Returned list == exact sorted on-disk survivors."""
    reg = make_registry(backup_retention=3)
    backups_dir = os.path.join(reg.data_dir, "users", "paz", "backups")
    os.makedirs(backups_dir)
    safety = "backup-2020-01-01-00.00.00-pre-delete-notes.colpkg"
    for name in [safety] + [f"backup-2020-01-01-00.00.{second:02d}-pre-sync.colpkg" for second in range(1, 6)]:
        with open(os.path.join(backups_dir, name), "w", encoding="utf-8") as f:
            f.write("x")

    kept = reg.prune_backups("paz")

    assert safety in kept  # the destructive-op snapshot survives the churn
    assert kept == sorted(
        [
            "backup-2020-01-01-00.00.00-pre-delete-notes.colpkg",
            "backup-2020-01-01-00.00.04-pre-sync.colpkg",
            "backup-2020-01-01-00.00.05-pre-sync.colpkg",
        ]
    )
    assert sorted(os.listdir(backups_dir)) == kept  # returned list IS the disk state
    assert reg.prune_backups("paz") == kept  # idempotent


# -- username validation (path-traversal guard) -------------------------------------------


@pytest.mark.asyncio
async def test_invalid_usernames_are_rejected_before_path_use(make_registry):
    reg = make_registry()
    for bad in ("../evil", "Alice", "a/b", "", "x" * 33, "a b"):
        with pytest.raises(ValueError):
            await reg.run(bad, count_notes)
        with pytest.raises(ValueError):
            reg.ledger(bad)
        with pytest.raises(ValueError):
            reg.prune_backups(bad)
        with pytest.raises(ValueError):
            await reg.sync_user(bad, "e@example.com", "p")
        with pytest.raises(ValueError):
            await reg.drop_user(bad)

    # Nothing was created for the rejected names.
    assert not os.path.exists(os.path.join(reg.data_dir, "users"))
    assert not os.path.exists(os.path.join(reg.data_dir, "ledgers"))


# -- shutdown ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_closes_everything_but_keeps_data_on_disk(make_registry):
    reg = make_registry()
    await reg.run("pat", add_basic_note, "front", "back")
    await reg.run("quinn", count_notes)

    await reg.close_all()
    assert reg.open_usernames() == []

    # Collections reopen transparently after shutdown with data intact.
    assert await reg.run("pat", count_notes) == 1


# -- drop_user (user-deletion hook) ------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_user_detaches_open_collection_and_ledger(make_registry):
    """Deleted user loses state AND ledger, so a recreated username starts
    fresh (regression: recreated usernames inherited the old open collection
    handle and the old idempotency ledger)."""
    reg = make_registry()
    await reg.run("zara", add_basic_note, "old-zara-note", "back")
    old_ledger = reg.ledger("zara")
    await old_ledger.record_success("old-key", {"created_note_ids": [1]})
    old_state = reg._users["zara"]

    await reg.drop_user("zara")

    assert "zara" not in reg._users  # cached state popped
    assert not reg.is_open("zara")
    assert "zara" not in reg.open_usernames()
    assert "zara" not in reg._ledgers  # in-memory ledger popped

    # Simulate the delete handler's archive step (production moves users/ and
    # ledgers/ into deleted/<name>-<ts> before/around drop_user): the registry
    # reopens whatever is on disk, so the old files must be out of the way for
    # the fresh-collection guarantee.
    os.rename(os.path.join(reg.data_dir, "users", "zara"), os.path.join(reg.data_dir, "archived-zara"))
    os.rename(os.path.join(reg.data_dir, "ledgers", "zara"), os.path.join(reg.data_dir, "archived-ledgers-zara"))

    # Recreated username: fresh registry state and a FRESH empty collection.
    assert await reg.run("zara", count_notes) == 0
    assert reg._users["zara"] is not old_state
    new_ledger = reg.ledger("zara")
    assert new_ledger is not old_ledger
    assert await new_ledger.get("old-key") is None  # old entries not reloaded


@pytest.mark.asyncio
async def test_drop_user_unknown_username_is_a_noop(make_registry):
    reg = make_registry()

    await reg.drop_user("ghost")  # must not raise

    assert not os.path.exists(os.path.join(reg.data_dir, "users", "ghost"))
    assert not os.path.exists(os.path.join(reg.data_dir, "ledgers", "ghost"))


@pytest.mark.asyncio
async def test_drop_user_leaves_other_users_unaffected(make_registry):
    reg = make_registry()
    await reg.run("alice", add_basic_note, "alice-note", "back")
    await reg.run("bob", add_basic_note, "bob-note", "back")

    await reg.drop_user("bob")

    assert reg.is_open("alice")  # never touched
    fronts = await reg.run("alice", lambda col: [col.get_note(nid).fields[0] for nid in col.find_notes("")])
    assert fronts == ["alice-note"]  # data intact
    assert await reg.run("alice", count_notes) == 1


# -- AnkiWeb sync follow-up phases ---------------------------------------------


def _sync_response(required, new_endpoint=None, server_media_usn=0, host_number=0):
    from anki.sync_pb2 import SyncCollectionResponse

    resp = SyncCollectionResponse()
    resp.required = required
    resp.host_number = host_number
    resp.server_media_usn = server_media_usn
    if new_endpoint:
        resp.new_endpoint = new_endpoint
    return resp


def test_followup_plan_no_changes_and_normal_sync_settle_in_handshake():
    from anki.sync_pb2 import SyncCollectionResponse

    plan = _plan_sync_followup
    assert plan(_sync_response(SyncCollectionResponse.ChangesRequired.NO_CHANGES)) == ("none", None)
    assert plan(_sync_response(SyncCollectionResponse.ChangesRequired.NORMAL_SYNC)) == ("none", None)


def test_followup_plan_full_download_carries_media_usn():
    from anki.sync_pb2 import SyncCollectionResponse

    out = _sync_response(
        SyncCollectionResponse.ChangesRequired.FULL_DOWNLOAD,
        new_endpoint="https://sync30.ankiweb.net/",
        server_media_usn=231400,
        host_number=30,
    )
    # Exactly the shape a fresh empty collection gets from a real account.
    assert _plan_sync_followup(out) == ("download", 231400)


def test_followup_plan_full_upload_and_full_sync():
    from anki.sync_pb2 import SyncCollectionResponse

    assert _plan_sync_followup(_sync_response(SyncCollectionResponse.ChangesRequired.FULL_UPLOAD)) == ("upload", None)
    assert _plan_sync_followup(_sync_response(SyncCollectionResponse.ChangesRequired.FULL_SYNC)) == ("error", None)


class _FakeSyncCollection:
    """Stands in for anki.collection.Collection for sync_user (no network)."""

    def __init__(self, handshake):
        self.handshake = handshake
        self.calls = []

    def create_backup(self, backup_folder, force, wait_for_completion):
        # Emulate the real backend: a backup writes backup-<ts>.colpkg into
        # backup_folder and returns True. The registry's _backup_locked diffs
        # the directory and renames the new file with the sync reason, so the
        # fake must produce the file just like the wheel does.
        os.makedirs(backup_folder, exist_ok=True)
        name = time.strftime("backup-%Y-%m-%d-%H.%M.%S.colpkg")
        with open(os.path.join(backup_folder, name), "w", encoding="utf-8") as f:
            f.write("fake")
        return True

    def sync_login(self, email, password, endpoint):
        self.calls.append(("login", email, endpoint))
        from anki.sync import SyncAuth

        auth = SyncAuth()
        auth.hkey = "hkey"
        return auth

    def sync_collection(self, auth, sync_media):
        self.calls.append(("sync_collection", auth.endpoint))
        return self.handshake

    def full_upload_or_download(self, *, auth, server_usn, upload):
        self.calls.append(("full", auth.endpoint, server_usn, upload))


@pytest.mark.asyncio
async def test_sync_user_performs_full_download_followup_with_redirect(make_registry, monkeypatch):
    """Regression (real-account first sync): the handshake alone reported
    success while the collection stayed empty — sync_user must perform the
    FULL_DOWNLOAD transfer, against the redirected endpoint."""
    from anki.sync_pb2 import SyncCollectionResponse

    reg = make_registry()
    fake = _FakeSyncCollection(
        _sync_response(
            SyncCollectionResponse.ChangesRequired.FULL_DOWNLOAD,
            new_endpoint="https://sync30.ankiweb.net/",
            server_media_usn=231400,
            host_number=30,
        )
    )

    async def fake_ensure_open(state):
        return fake

    monkeypatch.setattr(reg, "_ensure_open", fake_ensure_open)

    result = await reg.sync_user("alice", "user@example.com", "pw")

    assert result["success"] is True, result
    assert "Full download" in result["output"]
    # The follow-up transfer ran, against the redirected endpoint, with the
    # server's media USN, in the download direction.
    assert fake.calls[1] == ("sync_collection", "")
    assert fake.calls[2] == ("full", "https://sync30.ankiweb.net/", 231400, False)


@pytest.mark.asyncio
async def test_sync_user_full_upload_followup(make_registry, monkeypatch):
    from anki.sync_pb2 import SyncCollectionResponse

    reg = make_registry()
    fake = _FakeSyncCollection(
        _sync_response(SyncCollectionResponse.ChangesRequired.FULL_UPLOAD, host_number=7)
    )

    async def fake_ensure_open(state):
        return fake

    monkeypatch.setattr(reg, "_ensure_open", fake_ensure_open)

    result = await reg.sync_user("alice", "user@example.com", "pw")

    assert result["success"] is True, result
    assert "Full upload" in result["output"]
    assert fake.calls[2] == ("full", "", None, True)


@pytest.mark.asyncio
async def test_sync_user_full_sync_direction_is_a_definitive_failure(make_registry, monkeypatch):
    from anki.sync_pb2 import SyncCollectionResponse

    reg = make_registry()
    fake = _FakeSyncCollection(_sync_response(SyncCollectionResponse.ChangesRequired.FULL_SYNC))

    async def fake_ensure_open(state):
        return fake

    monkeypatch.setattr(reg, "_ensure_open", fake_ensure_open)

    result = await reg.sync_user("alice", "user@example.com", "pw")

    assert result["success"] is False
    assert "full sync" in result["error"].lower()
    assert result["error_type"] == "ValueError"
    assert result["full_sync_required"] is True  # portal/MCP render a direction choice off this
    assert "outcome_unknown" not in result  # definitive refusal, not ambiguous
    assert fake.calls == [("login", "user@example.com", None), ("sync_collection", "")]  # no transfer


@pytest.mark.asyncio
async def test_sync_user_full_sync_with_chosen_upload_direction(make_registry, monkeypatch):
    """FULL_SYNC + full_sync_direction='upload' runs the bulk upload instead
    of failing — mirroring the desktop client's user-picked direction."""
    from anki.sync_pb2 import SyncCollectionResponse

    reg = make_registry()
    fake = _FakeSyncCollection(_sync_response(SyncCollectionResponse.ChangesRequired.FULL_SYNC))

    async def fake_ensure_open(state):
        return fake

    monkeypatch.setattr(reg, "_ensure_open", fake_ensure_open)

    result = await reg.sync_user("alice", "user@example.com", "pw", full_sync_direction="upload")

    assert result["success"] is True, result
    assert "Full upload" in result["output"]
    assert "user-chosen direction" in result["output"]
    # Upload carries no media USN, pushes local data to the server.
    assert fake.calls[2] == ("full", "", None, True)


@pytest.mark.asyncio
async def test_sync_user_full_sync_with_chosen_download_direction_carries_media_usn(
    make_registry, monkeypatch
):
    """FULL_SYNC + full_sync_direction='download' pulls the server copy
    wholesale, with the handshake's server_media_usn and against the
    redirected endpoint — exactly like the FULL_DOWNLOAD follow-up."""
    from anki.sync_pb2 import SyncCollectionResponse

    reg = make_registry()
    fake = _FakeSyncCollection(
        _sync_response(
            SyncCollectionResponse.ChangesRequired.FULL_SYNC,
            new_endpoint="https://sync30.ankiweb.net/",
            server_media_usn=231400,
        )
    )

    async def fake_ensure_open(state):
        return fake

    monkeypatch.setattr(reg, "_ensure_open", fake_ensure_open)

    result = await reg.sync_user("alice", "user@example.com", "pw", full_sync_direction="download")

    assert result["success"] is True, result
    assert "Full download" in result["output"]
    assert "user-chosen direction" in result["output"]
    assert fake.calls[2] == ("full", "https://sync30.ankiweb.net/", 231400, False)


@pytest.mark.asyncio
async def test_sync_user_rejects_invalid_full_sync_direction_before_any_network(make_registry, monkeypatch):
    reg = make_registry()

    async def explode(state):
        raise AssertionError("_ensure_open must not be reached for an invalid direction")

    monkeypatch.setattr(reg, "_ensure_open", explode)

    result = await reg.sync_user("alice", "user@example.com", "pw", full_sync_direction="sideways")

    assert result["success"] is False
    assert result["error_type"] == "ValueError"
    assert "full_sync_direction" in result["error"]
    assert "full_sync_required" not in result  # not the server's FULL_SYNC demand


# -- backup engine ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_locked_tags_reason_and_prunes_to_retention(make_registry):
    """_backup_locked renames the backend's plain backup-<ts>.colpkg into
    backup-<ts>-<reason>.colpkg (timestamp prefix untouched, so lexicographic
    order stays chronological) and prunes to backup_retention afterwards."""
    reg = make_registry(backup_retention=2)
    backups_dir = os.path.join(reg.data_dir, "users", "ursula", "backups")
    os.makedirs(backups_dir)
    # Older backups predate the new one lexicographically (real 2026 timestamps).
    for ts in ("2020-01-01-00.00.01", "2020-01-01-00.00.02", "2020-01-01-00.00.03"):
        with open(os.path.join(backups_dir, f"backup-{ts}.colpkg"), "w", encoding="utf-8") as f:
            f.write("old")

    await reg.run("ursula", add_basic_note, "changed", "back")  # a change, so the backend writes a file

    def backup(col):
        return reg._backup_locked(col, "ursula", "manual")

    name = await reg.run("ursula", backup)

    assert name.startswith("backup-") and name.endswith("-manual.colpkg")
    assert name in os.listdir(backups_dir)  # renamed file is on disk

    # Prune kept exactly the 2 newest: the newest seed plus the new backup.
    kept = sorted(os.listdir(backups_dir))
    assert kept == sorted(["backup-2020-01-01-00.00.03.colpkg", name])


@pytest.mark.asyncio
async def test_backup_locked_no_new_file_returns_newest_or_fails_loud(make_registry, monkeypatch):
    """With no change since the last backup the backend writes no file
    (verified against the wheel: returns False, or overwrites a same-second
    name); _backup_locked must then return the NEWEST existing backup's name,
    and fail loud when no backup exists at all."""
    reg = make_registry()
    await reg.run("walt", count_notes)  # opens collection, creates empty backups dir
    col = reg._users["walt"].col
    monkeypatch.setattr(col, "create_backup", lambda **kwargs: False)

    def backup(col_):
        return reg._backup_locked(col_, "walt", "pre-sync")

    # Empty backups dir + no backup created: there is no name to return.
    with pytest.raises(RuntimeError):
        await reg.run("walt", backup)

    # With an existing backup, "nothing changed" yields that backup's name.
    seeded = "backup-2020-01-01-00.00.00-pre-sync.colpkg"
    with open(os.path.join(reg.data_dir, "users", "walt", "backups", seeded), "w", encoding="utf-8") as f:
        f.write("old")
    assert await reg.run("walt", backup) == seeded


@pytest.mark.asyncio
async def test_backup_locked_rejects_unknown_reason(make_registry):
    reg = make_registry()

    def backup(col):
        return reg._backup_locked(col, "petra", "because")

    with pytest.raises(ValueError):
        await reg.run("petra", backup)


@pytest.mark.asyncio
async def test_backup_user_rejects_bad_reason_and_username_before_any_io(make_registry):
    reg = make_registry()
    with pytest.raises(ValueError):
        await reg.backup_user("yuri", "because")
    with pytest.raises(ValueError):
        await reg.backup_user("../evil", "manual")

    # Rejected before any collection open or directory creation.
    assert not os.path.exists(os.path.join(reg.data_dir, "users", "yuri"))
    assert not reg.is_open("yuri")


@pytest.mark.asyncio
async def test_list_backups_parses_reason_and_sorts_newest_first(make_registry):
    reg = make_registry()
    backups_dir = os.path.join(reg.data_dir, "users", "xena", "backups")
    os.makedirs(backups_dir)
    files = {
        "backup-2026-01-01-10.00.00.colpkg": "plain",
        "backup-2026-01-02-10.00.00-pre-delete-notes.colpkg": "hyphen",
        "backup-2026-01-03-10.00.00-manual.colpkg": "manual",
        "backup-2020-01-01-00.00.00-pre-sync.colpkg": "old",
        # Not a backup filename: must be invisible to the listing.
        "not-a-backup.txt": "foreign",
        "backup-2026-01-04-10.00.00-PRE-SYNC.colpkg": "uppercase reason",
        "backup-2026-01-05-10.00.00-pre_sync.colpkg": "underscore reason",
    }
    for name, content in files.items():
        with open(os.path.join(backups_dir, name), "w", encoding="utf-8") as f:
            f.write(content)

    listing = reg.list_backups("xena")

    assert [entry["filename"] for entry in listing] == [
        "backup-2026-01-03-10.00.00-manual.colpkg",  # newest first
        "backup-2026-01-02-10.00.00-pre-delete-notes.colpkg",
        "backup-2026-01-01-10.00.00.colpkg",
        "backup-2020-01-01-00.00.00-pre-sync.colpkg",
    ]
    newest = listing[0]
    assert newest["timestamp"] == "2026-01-03-10.00.00"
    assert newest["reason"] == "manual"
    assert newest["size_bytes"] == len("manual")
    plain = listing[2]
    assert plain["timestamp"] == "2026-01-01-10.00.00"
    assert plain["reason"] == "unknown"  # no reason group in the filename

    # Missing directory (user never opened): empty listing, not an error.
    assert reg.list_backups("nobody-here") == []


# -- restore -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_round_trip_brings_deleted_notes_back(make_registry):
    """The full engine round-trip: snapshot -> delete -> restore -> notes back,
    with the handle detached (next run() reopens) and a pre-restore snapshot
    left behind so the restore itself is reversible."""
    reg = make_registry()
    await reg.run("nia", add_basic_note, "first", "one")
    await reg.run("nia", add_basic_note, "second", "two")
    snapshot = await reg.backup_user("nia", "manual")
    assert snapshot.endswith("-manual.colpkg")

    await reg.run("nia", lambda col: col.remove_notes(col.find_notes("")))
    assert await reg.run("nia", count_notes) == 0

    result = await reg.restore_backup("nia", snapshot)

    assert result["restored_from"] == snapshot
    # The open handle was detached; the next run() reopens the restored file.
    assert not reg.is_open("nia")
    fronts = sorted(
        await reg.run("nia", lambda col: [col.get_note(nid).fields[0] for nid in col.find_notes("")])
    )
    assert fronts == ["first", "second"]

    # A pre-restore snapshot was taken and is listed with its reason.
    listing = reg.list_backups("nia")
    assert result["pre_restore_snapshot"].endswith("-pre-restore.colpkg")
    assert result["pre_restore_snapshot"] in [entry["filename"] for entry in listing]
    assert "pre-restore" in [entry["reason"] for entry in listing]


@pytest.mark.asyncio
async def test_restore_of_oldest_snapshot_at_retention_cap_reads_target_before_prune(make_registry):
    """core-1 repro: at the retention cap, the pre-restore snapshot written
    INSIDE restore_backup pushes the directory over retention and the prune
    it runs deletes the OLDEST file — exactly the one a "restore the oldest
    snapshot" call is reading (its reason, pre-restore, is a SAFETY class,
    but the aging target is routine). The payload is therefore read BEFORE
    the snapshot: the restore must succeed with the restored state live,
    never FileNotFoundError."""
    reg = make_registry(backup_retention=3)
    await reg.run("petra", add_basic_note, "snapshot state", "back")
    real = await reg.backup_user("petra", "manual")  # a REAL wheel colpkg
    backups_dir = os.path.join(reg.data_dir, "users", "petra", "backups")
    with open(os.path.join(backups_dir, real), "rb") as f:
        data = f.read()
    # Three identical valid snapshots at the cap under fixed chronological
    # names (successive real backups land in the same second and would tie).
    names = [f"backup-2020-01-01-00.00.0{i}-manual.colpkg" for i in (1, 2, 3)]
    for name in names:
        with open(os.path.join(backups_dir, name), "wb") as f:
            f.write(data)
    os.remove(os.path.join(backups_dir, real))
    assert sorted(os.listdir(backups_dir)) == sorted(names)

    # One unbacked change: the live state moves past every snapshot.
    await reg.run("petra", add_basic_note, "unbacked change", "back")
    assert await reg.run("petra", count_notes) == 2

    result = await reg.restore_backup("petra", names[0])  # the OLDEST

    assert result["restored_from"] == names[0]
    assert result["pre_restore_snapshot"].endswith("-pre-restore.colpkg")
    # The restored state is LIVE: the open handle was detached and the next
    # run() reopened the restored file — the unbacked change is gone again.
    assert not reg.is_open("petra")
    assert await reg.run("petra", count_notes) == 1


@pytest.mark.asyncio
async def test_restore_backup_rejects_traversal_and_unknown_names(make_registry):
    reg = make_registry()
    snapshot = await reg.backup_user("omar", "manual")

    for bad in ("../../etc/passwd", "..", "backup-2030-01-01-00.00.00-manual.colpkg", f"{snapshot}-extra"):
        with pytest.raises(ValueError):
            await reg.restore_backup("omar", bad)
    with pytest.raises(ValueError):
        await reg.restore_backup("../evil", snapshot)  # username path guard

    # Rejections changed nothing: the valid backup still restores.
    result = await reg.restore_backup("omar", snapshot)
    assert result["restored_from"] == snapshot


@pytest.mark.asyncio
async def test_sync_user_still_takes_a_pre_sync_backup(make_registry, monkeypatch):
    """Regression guard for the sync_user refactor: the pre-sync snapshot must
    route through the shared _backup_locked path and leave a reason-tagged
    file on disk (endpoint unreachable, so the login failure happens AFTER
    the backup)."""
    reg = make_registry()
    seen = []
    real_backup_locked = reg._backup_locked

    def spy(col, username, reason):
        seen.append((username, reason))
        return real_backup_locked(col, username, reason)

    monkeypatch.setattr(reg, "_backup_locked", spy)

    result = await reg.sync_user("sage", "sage@example.com", "pw", endpoint="http://127.0.0.1:9")

    assert result["success"] is False  # login failed, but the backup ran first
    assert seen == [("sage", "pre-sync")]
    entries = reg.list_backups("sage")
    assert [entry["reason"] for entry in entries] == ["pre-sync"]


def test_runtime_can_decompress_the_wheels_zstd_backup_member():
    """Restore reads the wheel's zstd-compressed collection.anki21b backup
    member. This pins the invariant the 2026 Docker incident taught (the image
    regressed to a Python where restore died at runtime while backups kept
    succeeding — unreadable safety snapshots): every runtime running this
    suite must offer a zstd implementation the service's own import chain can
    use — stdlib compression.zstd (Python 3.14+, preferred) or the
    'zstandard' backport. And it must actually decompress a frame WITHOUT an
    embedded content size (exactly what the wheel writes; the backport's
    one-shot decompress refuses those, hence the service's streaming adapter).
    """
    from src.collections import _zstd_decompress

    assert _zstd_decompress is not None, (
        "no zstd available for backup restore: use Python 3.14+ or "
        "pip install zstandard"
    )
    # Frame built the way the wheel builds its member: no content-size field.
    import zstandard

    payload = b"restore-me" * 1000
    frame = zstandard.ZstdCompressor(write_content_size=False).compress(payload)
    assert _zstd_decompress(frame) == payload


def test_backport_decompress_roundtrips_and_fails_closed_on_corrupt_frames():
    """Direct adapter contract: the zstandard streaming adapter must
    round-trip a content-size-less frame (exactly what the wheel writes) AND
    fail closed on empty, truncated, and trailing-data input — the three
    shapes the raw decompressobj silently accepts (empty -> b'', truncated ->
    partial prefix with eof False, trailing bytes -> unused_data; probed on
    zstandard 0.25.0). This is tested DIRECTLY because on this Python
    (3.14+) _zstd_decompress resolves to the stdlib one-shot, which already
    raises on truncated input — only the module-level function pins the
    backport path's fail-closed behavior for older runtimes."""
    pytest.importorskip("zstandard")
    import zstandard

    from src.collections import _backport_decompress

    payload = b"restore-me" * 1000
    frame = zstandard.ZstdCompressor(write_content_size=False).compress(payload)
    assert _backport_decompress(frame) == payload

    truncated = frame[: len(frame) - max(1, len(frame) // 10)]
    assert truncated != frame  # genuinely cut mid-frame, not a no-op slice
    with pytest.raises(ValueError):
        _backport_decompress(truncated)
    with pytest.raises(ValueError):
        _backport_decompress(b"")
    with pytest.raises(ValueError):
        _backport_decompress(frame + b"trailing garbage")
