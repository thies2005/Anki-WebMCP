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
from src.collections import CollectionRegistry  # isort: skip
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
