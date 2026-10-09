"""Unit tests for the registry-backed MCP tool implementations (Task 5).

These run the real tool functions against REAL per-user anki collections in a
tmp data dir (no AnkiConnect, no HTTP mocks). The asserted response shapes are
byte-for-byte the ones the AnkiConnect-era suite asserted — that schema is the
compatibility contract.
"""

import os

import pytest
import pytest_asyncio

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.models import NoteInput
from src.sync_manager import SyncManager
from src.tools import decks as deck_tools
from src.tools import notes as note_tools
from src.tools import stats as stats_tools


def add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return note.id


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data_dir, two users."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, None)
    yield reg, manager
    await reg.close_all()


# -- list_decks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_decks(env):
    reg, _ = env
    await reg.run("alice", add_basic_note, "q", "a", deck="Japanese")

    data = await deck_tools.list_decks(reg, "alice")
    assert data["count"] == 2
    names = {d["name"]: d["id"] for d in data["decks"]}
    assert set(names) == {"Default", "Japanese"}
    assert all(isinstance(did, int) and did > 0 for did in names.values())
    assert {"name": "Default", "id": names["Default"]} in data["decks"]


# -- search_notes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_notes_bounded_pagination(env):
    reg, _ = env
    for i in range(3):
        await reg.run("alice", add_basic_note, f"question-{i}", f"answer-{i}")

    # Request limit=200 should be clamped to 100
    res = await note_tools.search_notes(reg, "alice", query="deck:Default", limit=200, offset=1)
    assert res["total_matches"] == 3
    assert res["limit"] == 100  # Clamped to 100
    assert res["offset"] == 1
    assert res["returned_count"] == 2
    first = res["notes"][0]
    assert set(first) == {"note_id", "model_name", "tags", "fields"}
    assert first["model_name"] == "Basic"
    assert first["tags"] == []
    assert set(first["fields"]) == {"Front", "Back"}
    assert first["fields"]["Front"].startswith("question-")
    assert isinstance(first["note_id"], int)


@pytest.mark.asyncio
async def test_search_notes_query_syntax_and_tags(env):
    reg, _ = env
    nid = await reg.run("alice", add_basic_note, "vocab word", "答え")

    def _tag(col):
        col.tags.bulk_add([nid], "vocab japanese")

    await reg.run("alice", _tag)

    res = await note_tools.search_notes(reg, "alice", query="tag:vocab")
    assert res["total_matches"] == 1
    assert res["notes"][0]["note_id"] == nid
    # The wheel normalizes/sorts stored tags on write
    assert sorted(res["notes"][0]["tags"]) == ["japanese", "vocab"]
    assert res["notes"][0]["fields"]["Front"] == "vocab word"

    # Anki query syntax narrows: a non-matching query finds nothing
    empty = await note_tools.search_notes(reg, "alice", query="tag:nonexistent")
    assert empty == {
        "total_matches": 0,
        "returned_count": 0,
        "offset": 0,
        "limit": 50,
        "notes": [],
    }


@pytest.mark.asyncio
async def test_search_notes_offset_past_end_returns_empty_shape(env):
    reg, _ = env
    await reg.run("alice", add_basic_note, "only", "note")
    res = await note_tools.search_notes(reg, "alice", query="deck:Default", offset=5)
    assert res["total_matches"] == 1
    assert res["returned_count"] == 0
    assert res["offset"] == 5
    assert res["notes"] == []


# -- get_note -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_note(env):
    reg, _ = env

    def _make(col):
        note = Note(col, col.models.by_name("Basic"))
        note["Front"] = "Hello"
        note["Back"] = "World"
        note.tags = ["tag1", "tag2"]
        col.add_note(note, col.decks.id("Default"))
        return note.id

    nid = await reg.run("alice", _make)

    note = await note_tools.get_note(reg, "alice", nid)
    assert note["note_id"] == nid
    assert note["model_name"] == "Basic"
    assert note["tags"] == ["tag1", "tag2"]
    assert note["fields"] == {"Front": "Hello", "Back": "World"}


@pytest.mark.asyncio
async def test_get_note_not_found(env):
    reg, _ = env
    with pytest.raises(CollectionToolError, match="not found"):
        await note_tools.get_note(reg, "alice", 99999)


# -- get_collection_stats -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_collection_stats(env):
    reg, _ = env
    await reg.run("alice", add_basic_note, "q1", "a1")
    await reg.run("alice", add_basic_note, "q2", "a2", deck="Deep")

    stats = await stats_tools.get_collection_stats(reg, "alice")
    assert stats["cards_reviewed_today"] == 0  # fresh collection, nothing reviewed
    assert stats["deck_count"] == 2
    assert set(stats["summary"]) == {"total_new", "total_learn", "total_review", "total_cards"}
    assert stats["summary"]["total_new"] == 2  # fresh cards are queue 0
    assert stats["summary"]["total_learn"] == 0
    assert stats["summary"]["total_review"] == 0
    assert stats["summary"]["total_cards"] == 2
    deck_names = {d["name"] for d in stats["decks"]}
    assert deck_names == {"Default", "Deep"}
    for deck in stats["decks"]:
        assert set(deck) == {"deck_id", "name", "new_count", "learn_count", "review_count", "total_in_deck"}
        assert deck["total_in_deck"] == 1
        assert deck["new_count"] == 1


# -- add_notes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_notes_success(env):
    reg, manager = env

    notes = [
        NoteInput(
            deck_name="Default",
            model_name="Basic",
            fields={"Front": "Unique Q", "Back": "Ans"},
            tags=["sample"],
        )
    ]

    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is True
    assert res.local_status == "committed"
    assert len(res.data["created_note_ids"]) == 1
    assert res.data["count"] == 1
    nid = res.data["created_note_ids"][0]

    # The note really landed, with its tag
    def _check(col):
        note = col.get_note(nid)
        return dict(note.items()), list(note.tags)

    fields, tags = await reg.run("alice", _check)
    assert fields == {"Front": "Unique Q", "Back": "Ans"}
    assert tags == ["sample"]


@pytest.mark.asyncio
async def test_add_notes_creates_missing_deck(env):
    reg, manager = env
    notes = [NoteInput(deck_name="Japanese", model_name="Basic", fields={"Front": "q", "Back": "a"})]
    res = await note_tools.add_notes(manager, reg, "alice", notes)
    assert res.success is True
    decks = await deck_tools.list_decks(reg, "alice")
    assert "Japanese" in [d["name"] for d in decks["decks"]]


@pytest.mark.asyncio
async def test_add_notes_duplicate_prevention(env):
    reg, manager = env
    await reg.run("alice", add_basic_note, "Duplicate Question", "Answer")

    notes = [
        NoteInput(
            deck_name="Default",
            model_name="Basic",
            fields={"Front": "Duplicate Question", "Back": "Answer 2"},
        )
    ]

    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is False
    assert res.local_status == "failed"
    assert "Duplicate check failed" in res.error

    # allow_duplicates=True goes through
    res2 = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=True)
    assert res2.success is True


@pytest.mark.asyncio
async def test_add_notes_duplicates_scoped_to_deck(env):
    reg, manager = env
    await reg.run("alice", add_basic_note, "Same Front", "a", deck="Default")
    # Same first field, different deck: not a duplicate under deck scope
    notes = [NoteInput(deck_name="Other", model_name="Basic", fields={"Front": "Same Front", "Back": "b"})]
    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is True


@pytest.mark.asyncio
async def test_add_notes_unknown_model_lists_available(env):
    reg, manager = env
    notes = [NoteInput(deck_name="Default", model_name="No Such Model", fields={"Front": "q"})]
    # In-mutation validation comes back as a failed MutationResponse (same
    # semantics as the legacy duplicate/field checks inside execute_mutation).
    res = await note_tools.add_notes(manager, reg, "alice", notes)
    assert res.success is False
    assert res.local_status == "failed"
    assert "No Such Model" in res.error
    assert "Basic" in res.error  # available models listed


@pytest.mark.asyncio
async def test_add_notes_empty_batch_rejected(env, tmp_path):
    reg, manager = env
    with pytest.raises(CollectionToolError, match="At least one note"):
        await note_tools.add_notes(manager, reg, "alice", [])
    # Nothing may have been opened/created for the user by a rejected batch
    assert not os.path.exists(os.path.join(str(tmp_path), "users", "alice"))


# -- update_note_fields ----------------------------------------------------------


@pytest.mark.asyncio
async def test_update_note_fields(env):
    reg, manager = env
    nid = await reg.run("alice", add_basic_note, "Old", "Old")

    res = await note_tools.update_note_fields(manager, reg, "alice", nid, {"Front": "New Question"})
    assert res.success is True
    assert res.local_status == "committed"
    assert res.data["note_id"] == nid
    assert res.data["updated_fields"] == ["Front"]

    updated = await note_tools.get_note(reg, "alice", nid)
    assert updated["fields"]["Front"] == "New Question"
    assert updated["fields"]["Back"] == "Old"  # untouched


@pytest.mark.asyncio
async def test_update_note_fields_unknown_field_rejected(env):
    reg, manager = env
    nid = await reg.run("alice", add_basic_note, "Old", "Old")

    res = await note_tools.update_note_fields(manager, reg, "alice", nid, {"Frant": "typo"})
    assert res.success is False
    assert res.local_status == "failed"
    assert "unknown field" in res.error
    assert "Front" in res.error  # valid fields listed


@pytest.mark.asyncio
async def test_update_note_fields_missing_note_rejected(env):
    reg, manager = env
    res = await note_tools.update_note_fields(manager, reg, "alice", 99999, {"Front": "x"})
    assert res.success is False
    assert "not found" in res.error


# -- add_tags ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_tags(env):
    reg, manager = env
    nid1 = await reg.run("alice", add_basic_note, "q1", "a1")
    nid2 = await reg.run("alice", add_basic_note, "q2", "a2")

    res = await note_tools.add_tags(manager, reg, "alice", [nid1, nid2], ["tag1", "tag2"])
    assert res.success is True
    assert res.local_status == "committed"
    assert res.data["tags_added"] == ["tag1", "tag2"]

    def _tags(col, ids):
        return [list(col.get_note(i).tags) for i in ids]

    tags = await reg.run("alice", _tags, [nid1, nid2])
    assert tags == [["tag1", "tag2"], ["tag1", "tag2"]]


@pytest.mark.asyncio
async def test_add_tags_missing_note_ids_rejected(env):
    reg, manager = env
    nid = await reg.run("alice", add_basic_note, "q", "a")

    res = await note_tools.add_tags(manager, reg, "alice", [99999, nid], ["tag1"])
    assert res.success is False
    assert "not found" in res.error
    assert "99999" in res.error


# -- cross-user isolation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_notes(env):
    reg, _ = env
    await reg.run("alice", add_basic_note, "alice secret", "x")

    alice_view = await note_tools.search_notes(reg, "alice", query="secret")
    bob_view = await note_tools.search_notes(reg, "bob", query="secret")
    assert alice_view["total_matches"] == 1
    assert bob_view["total_matches"] == 0

    # bob cannot read alice's note by id either
    alice_note_id = alice_view["notes"][0]["note_id"]
    with pytest.raises(CollectionToolError, match="not found"):
        await note_tools.get_note(reg, "bob", alice_note_id)
