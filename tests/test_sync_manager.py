"""Tests for the registry-backed SyncManager (Task 5): per-user idempotent
mutations, explicit sync with ledger semantics, and the ambiguous-outcome rule.

``registry.sync_user`` is monkeypatched for the sync tests (the real network
sync is covered in test_collections.py); mutation tests run against REAL
per-user collections in a tmp data dir.
"""

import asyncio
import os

import pytest
import pytest_asyncio

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported.
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.sync_manager import SyncManager
from src.users import UserStore


def add_basic_note(col, front, back="back"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id("Default"))
    return note.id


async def note_count(reg, username: str) -> int:
    """Number of notes in the user's collection (via the registry's thread)."""

    def _count(col):
        return len(col.find_notes("*"))

    return await reg.run(username, _count)


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + store + manager against a fresh tmp data dir.

    ``alice`` has AnkiWeb credentials; ``no creds`` user has none.
    """
    reg = CollectionRegistry(data_dir=str(tmp_path))
    store = UserStore(str(tmp_path / "users.json"))
    store.create_user("alice", "pw")
    store.set_ankiweb_credentials("alice", "alice@example.com", "ankiweb-pw")
    store.create_user("no_creds", "pw")
    manager = SyncManager(reg, store)
    yield reg, store, manager
    await reg.close_all()


async def _patch_sync_user(monkeypatch, reg, result):
    """Replace registry.sync_user with a recording fake returning ``result``."""
    calls = []

    async def fake_sync_user(username, email, password, endpoint=None, on_step=None):
        calls.append({"username": username, "email": email, "password": password})
        if on_step is not None:
            try:
                on_step("backup")
                on_step("sync")
            except Exception:  # the registry's on_step contract: never fatal
                pass
        return dict(result)

    monkeypatch.setattr(reg, "sync_user", fake_sync_user)
    return calls


# -- explicit sync ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_success_records_ledger(env, monkeypatch, tmp_path):
    reg, store, manager = env
    calls = await _patch_sync_user(
        monkeypatch, reg, {"success": True, "output": "SyncOutput(...)"}
    )

    res = await manager.sync_now("alice", idempotency_key="sync-key-1")

    assert res["success"] is True
    assert res["sync_status"]["post_sync"] == "ok"
    assert res["sync_status"]["pre_sync"] == "ok"
    assert res["message"] == "Synchronization completed successfully."
    assert res["warnings"] == []
    # Credentials came from the account record
    assert calls == [{"username": "alice", "email": "alice@example.com", "password": "ankiweb-pw"}]
    # The per-user ledger recorded the success with the ledger's field shapes
    ledger = reg.ledger("alice")
    record = await ledger.get("sync-key-1")
    assert record["status"] == "committed"
    assert record["data"] == {"output": "SyncOutput(...)"}
    assert "timestamp" in record


@pytest.mark.asyncio
async def test_sync_failure_records_ledger(env, monkeypatch):
    reg, store, manager = env
    await _patch_sync_user(
        monkeypatch,
        reg,
        {"success": False, "error": "SyncError: auth failed", "error_type": "SyncError"},
    )

    res = await manager.sync_now("alice", idempotency_key="sync-key-2")

    assert res["success"] is False
    assert res["sync_status"]["post_sync"] == "failed"
    assert res["sync_status"]["post_sync_error"] == "SyncError: auth failed"
    assert "Synchronization error: SyncError: auth failed" == res["message"]
    assert "outcome_unknown" not in res
    ledger = reg.ledger("alice")
    record = await ledger.get("sync-key-2")
    assert record["status"] == "failed"
    assert record["error"] == "SyncError: auth failed"
    assert "timestamp" in record


@pytest.mark.asyncio
async def test_sync_ambiguous_outcome_records_nothing(env, monkeypatch):
    """Network-layer failure mid-transfer: NO ledger record + outcome_unknown."""
    reg, store, manager = env
    await _patch_sync_user(
        monkeypatch,
        reg,
        {
            "success": False,
            "error": "SyncError: connection reset",
            "error_type": "SyncError",
            "outcome_unknown": True,
        },
    )

    res = await manager.sync_now("alice", idempotency_key="sync-ambiguous")

    assert res["success"] is False
    assert res["outcome_unknown"] is True
    joined = " ".join(res["warnings"])
    assert "UNKNOWN" in joined
    assert "NOT recorded" in joined
    # Crucially: no ledger record for the ambiguous outcome
    ledger = reg.ledger("alice")
    assert await ledger.get("sync-ambiguous") is None


@pytest.mark.asyncio
async def test_sync_missing_credentials_raise_before_ledger_write(env, tmp_path):
    reg, store, manager = env

    with pytest.raises(ValueError, match="AnkiWeb credentials not set"):
        await manager.sync_now("no_creds", idempotency_key="never-written")

    # No ledger file (or record) may exist for that user/key
    ledger_path = os.path.join(str(tmp_path), "ledgers", "no_creds", "ledger.json")
    assert not os.path.exists(ledger_path)
    assert await reg.ledger("no_creds").get("never-written") is None


@pytest.mark.asyncio
async def test_sync_unknown_user_raises(env):
    reg, store, manager = env
    with pytest.raises(ValueError, match="Unknown user"):
        await manager.sync_now("ghost")


@pytest.mark.asyncio
async def test_sync_idempotent_replay(env, monkeypatch):
    reg, store, manager = env
    calls = await _patch_sync_user(monkeypatch, reg, {"success": True, "output": "ok"})

    first = await manager.sync_now("alice", idempotency_key="sync-replay")
    assert first["success"] is True
    assert len(calls) == 1

    second = await manager.sync_now("alice", idempotency_key="sync-replay")
    assert second["success"] is True
    assert len(calls) == 1  # no second sync call
    assert any("Idempotent operation replay" in w for w in second["warnings"])
    assert second["data"] == {"output": "ok"}


@pytest.mark.asyncio
async def test_maintenance_mode_rejects_sync_now(env, monkeypatch):
    """Same gate as the mutation path: skipped-shaped response, no sync, no ledger write."""
    reg, store, manager = env
    manager.set_maintenance_mode(True)
    calls = await _patch_sync_user(monkeypatch, reg, {"success": True, "output": "ok"})

    res = await manager.sync_now("alice", idempotency_key="sync-maint")

    assert res["success"] is False
    assert res["sync_status"]["pre_sync"] == "skipped"
    assert res["sync_status"]["post_sync"] == "skipped"
    assert res["message"] == "Worker is currently in maintenance mode; writes are paused."
    assert res["warnings"] == []
    assert calls == []  # no sync attempted
    # The idempotency key was not consumed by the rejected request
    assert await reg.ledger("alice").get("sync-maint") is None


# -- mutation write-safety ----------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_success_commits_and_records_ledger(env):
    reg, store, manager = env

    async def mutation():
        return await reg.run("alice", add_basic_note, "front-1", "back")

    res = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=mutation, idempotency_key="mut-1"
    )

    assert res.success is True
    assert res.local_status == "committed"
    # No per-mutation sync exists anymore: sync_status stays fully skipped
    assert res.sync_status.pre_sync == "skipped"
    assert res.sync_status.post_sync == "skipped"
    assert isinstance(res.data, int)  # the created note id
    record = await reg.ledger("alice").get("mut-1")
    assert record["status"] == "committed"
    assert record["data"] == res.data


@pytest.mark.asyncio
async def test_mutation_failure_records_failure_ledger(env):
    reg, store, manager = env

    async def failing():
        raise ValueError("Invalid field Front")

    res = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=failing, idempotency_key="mut-fail"
    )

    assert res.success is False
    assert res.local_status == "failed"
    assert "Invalid field Front" in res.error
    record = await reg.ledger("alice").get("mut-fail")
    assert record["status"] == "failed"
    assert record["error"] == "Invalid field Front"


@pytest.mark.asyncio
async def test_mutation_idempotent_replay_runs_once(env):
    reg, store, manager = env
    counter = {"n": 0}

    async def counting():
        counter["n"] += 1
        return await reg.run("alice", add_basic_note, f"front-{counter['n']}")

    first = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=counting, idempotency_key="mut-replay"
    )
    assert first.success is True
    second = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=counting, idempotency_key="mut-replay"
    )
    assert counter["n"] == 1  # mutation ran once
    assert second.success is True
    assert second.data == first.data
    assert any("Idempotent operation replay" in w for w in second.warnings)


@pytest.mark.asyncio
async def test_concurrent_same_key_mutations_apply_once(env):
    """Two concurrent calls sharing an idempotency key: exactly one applies.

    The ledger check → mutate → record sequence must be atomic per user, so
    the second caller — which starts its ledger check while the first's slow
    mutation is still in flight — sees the committed record and replays it
    instead of double-applying.
    """
    reg, store, manager = env
    counter = {"n": 0}

    async def slow_mutation():
        counter["n"] += 1
        # Yield mid-mutation so the second caller's ledger check runs NOW —
        # without the per-user lock it would miss (nothing recorded yet).
        await asyncio.sleep(0.05)
        return await reg.run("alice", add_basic_note, f"front-{counter['n']}")

    results = await asyncio.gather(
        manager.execute_mutation(
            "alice", operation_name="add_notes", mutation_fn=slow_mutation, idempotency_key="race-key"
        ),
        manager.execute_mutation(
            "alice", operation_name="add_notes", mutation_fn=slow_mutation, idempotency_key="race-key"
        ),
    )

    # Exactly one caller applied the mutation; the other replayed the cached
    # committed result (same assertions as the sequential replay test).
    assert counter["n"] == 1
    # The local-only nudge warning rides on both responses now, so discriminate
    # by the replay marker rather than by emptiness of the warnings list.
    committed = [r for r in results if not any("Idempotent operation replay" in w for w in r.warnings)]
    replayed = [r for r in results if any("Idempotent operation replay" in w for w in r.warnings)]
    assert len(committed) == 1
    assert len(replayed) == 1
    assert committed[0].success is True
    assert committed[0].local_status == "committed"
    assert replayed[0].success is True
    assert replayed[0].data == committed[0].data
    # And only ONE note exists in the collection
    found = await note_count(reg, "alice")
    assert found == 1


@pytest.mark.asyncio
async def test_concurrent_mutations_different_users_not_serialized(env):
    """The same-key lock is per user: different users' mutations overlap."""
    reg, store, manager = env
    in_flight = {"count": 0, "max": 0}

    def make_mutation(username):
        async def mutation():
            in_flight["count"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["count"])
            await asyncio.sleep(0.05)
            result = await reg.run(username, add_basic_note, f"front-{username}")
            in_flight["count"] -= 1
            return result

        return mutation

    await asyncio.gather(
        manager.execute_mutation("alice", operation_name="op", mutation_fn=make_mutation("alice")),
        manager.execute_mutation("bob", operation_name="op", mutation_fn=make_mutation("bob")),
    )

    # Both mutations were in flight at the same time — a global lock would
    # report max == 1.
    assert in_flight["max"] == 2
    assert await note_count(reg, "alice") == 1
    assert await note_count(reg, "bob") == 1


@pytest.mark.asyncio
async def test_maintenance_mode_rejects_mutation(env):
    reg, store, manager = env
    manager.set_maintenance_mode(True)

    called = {"v": False}

    async def dummy():
        called["v"] = True
        return {}

    res = await manager.execute_mutation("alice", operation_name="add_notes", mutation_fn=dummy)

    assert res.success is False
    assert res.local_status == "skipped"
    assert "maintenance mode" in res.error
    assert called["v"] is False
    assert res.sync_status.pre_sync == "skipped"


# -- local-only nudge on committed mutations -----------------------------------------


def _has_sync_nudge(res) -> bool:
    return any("sync_collection" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_committed_mutation_warns_changes_stay_local(env):
    """Alice has AnkiWeb credentials: a committed mutation must tell the model
    that the change is local only and sync_collection is needed to push it."""
    reg, store, manager = env

    async def mutation():
        return await reg.run("alice", add_basic_note, "nudge-front", "back")

    res = await manager.execute_mutation("alice", operation_name="add_notes", mutation_fn=mutation)

    assert res.success is True
    assert _has_sync_nudge(res)


@pytest.mark.asyncio
async def test_committed_mutation_without_credentials_gives_no_sync_nudge(env):
    """No AnkiWeb credentials saved: sync_collection would only fail, so the
    nudge must not direct the model into a dead end."""
    reg, store, manager = env

    async def mutation():
        return await reg.run("no_creds", add_basic_note, "nudge-front", "back")

    res = await manager.execute_mutation("no_creds", operation_name="add_notes", mutation_fn=mutation)

    assert res.success is True
    assert not _has_sync_nudge(res)
    assert res.warnings == []


@pytest.mark.asyncio
async def test_failed_mutation_gives_no_sync_nudge(env):
    reg, store, manager = env

    async def failing():
        raise ValueError("Invalid field Front")

    res = await manager.execute_mutation("alice", operation_name="add_notes", mutation_fn=failing)

    assert res.success is False
    assert not _has_sync_nudge(res)


@pytest.mark.asyncio
async def test_replayed_mutation_also_carries_sync_nudge(env):
    """A retry replaying the cached committed result still needs the nudge —
    the caller of the retry may never have seen the original response."""
    reg, store, manager = env

    async def mutation():
        return await reg.run("alice", add_basic_note, "replay-nudge", "back")

    first = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=mutation, idempotency_key="nudge-replay"
    )
    second = await manager.execute_mutation(
        "alice", operation_name="add_notes", mutation_fn=mutation, idempotency_key="nudge-replay"
    )

    assert first.success and second.success
    assert any("Idempotent operation replay" in w for w in second.warnings)
    assert _has_sync_nudge(second)
