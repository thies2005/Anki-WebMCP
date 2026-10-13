"""Unit tests for the registry-backed MCP tool implementations (Task 5).

These run the real tool functions against REAL per-user anki collections in a
tmp data dir (no AnkiConnect, no HTTP mocks). The asserted response shapes are
byte-for-byte the ones the AnkiConnect-era suite asserted — that schema is the
compatibility contract.
"""

import logging
import os

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.logging_util import RedactingFormatter
from src.models import AddTagsInput, BuryCardsInput, GetNotesInput, NoteInput, SearchNotesInput
from src.sync_manager import SyncManager
from src.users import UserStore
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
    # Real (empty) user store: the mutation path reads it for the local-only nudge
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
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


@pytest.mark.asyncio
async def test_search_notes_query_log_is_single_line(env, caplog):
    """A newline in the client query cannot split the log record (CWE-117).

    The call site logs the query with %r, so the repr escapes the newline and
    the production-formatted output stays exactly one line.
    """
    reg, _ = env
    caplog.set_level(logging.INFO)
    res = await note_tools.search_notes(reg, "alice", query="deck:Default\nforged line")
    assert res["total_matches"] == 0  # the search still runs, just finds nothing

    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.tools.notes" and "Executing search_notes" in r.getMessage()
    ]
    assert len(recs) == 1  # exactly ONE record for this call
    line = RedactingFormatter("%(message)s").format(recs[0])
    assert "\n" not in line  # no forged second line
    assert "\\n" in line  # newline repr-escaped
    assert "forged line" in line  # content preserved inline, escaped


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


# -- search_notes field projection -------------------------------------------------


@pytest.mark.asyncio
async def test_search_notes_fields_projection_limits_payload(env):
    """fields=["Front"] projects every result down to just that field."""
    reg, _ = env
    await reg.run("alice", add_basic_note, "q1", "a1")
    await reg.run("alice", add_basic_note, "q2", "a2")

    res = await note_tools.search_notes(reg, "alice", query="deck:Default", fields=["Front"])
    assert res["total_matches"] == 2
    for note in res["notes"]:
        assert set(note["fields"]) == {"Front"}
        assert note["fields"]["Front"].startswith("q")
    # The response meta shape is the pinned compatibility contract — the
    # projection must not add keys to it.
    assert set(res) == {"total_matches", "returned_count", "offset", "limit", "notes"}


@pytest.mark.asyncio
async def test_search_notes_fields_missing_names_are_omitted_not_errors(env):
    """A requested field the note's type lacks is silently absent for that note
    (searches span note types, so an unknown-to-this-note name is not an error)."""
    reg, _ = env
    await reg.run("alice", add_basic_note, "q", "a")

    res = await note_tools.search_notes(reg, "alice", query="deck:Default", fields=["Front", "Nope"])
    assert res["notes"][0]["fields"] == {"Front": "q"}


@pytest.mark.asyncio
async def test_search_notes_fields_empty_list_behaves_like_none(env):
    """Direct calls bypass pydantic; an empty projection list must read as
    'no projection' rather than emptying every note's fields."""
    reg, _ = env
    await reg.run("alice", add_basic_note, "q", "a")

    res = await note_tools.search_notes(reg, "alice", query="deck:Default", fields=[])
    assert res["notes"][0]["fields"] == {"Front": "q", "Back": "a"}


# -- get_notes (batch) -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_notes_returns_full_summaries_in_request_order(env):
    reg, _ = env
    n1 = await reg.run("alice", add_basic_note, "one", "1")
    n2 = await reg.run("alice", add_basic_note, "two", "2")

    res = await note_tools.get_notes(reg, "alice", [n2, n1])
    assert res["requested"] == 2
    assert res["returned"] == 2
    assert res["missing"] == []
    assert [n["note_id"] for n in res["notes"]] == [n2, n1]
    assert res["notes"][0]["fields"] == {"Front": "two", "Back": "2"}
    assert res["notes"][0]["model_name"] == "Basic"
    assert set(res["notes"][0]) == {"note_id", "model_name", "tags", "fields"}


@pytest.mark.asyncio
async def test_get_notes_reports_missing_ids(env):
    reg, _ = env
    real = await reg.run("alice", add_basic_note, "keep", "me")

    res = await note_tools.get_notes(reg, "alice", [real, 999999999, real])
    assert res["requested"] == 3
    assert res["returned"] == 2  # duplicates are preserved, like notesInfo
    assert res["missing"] == [999999999]
    assert [n["note_id"] for n in res["notes"]] == [real, real]


def test_get_notes_input_bounds():
    with pytest.raises(ValidationError):
        GetNotesInput(note_ids=[])
    with pytest.raises(ValidationError):
        GetNotesInput(note_ids=[0])
    with pytest.raises(ValidationError):
        GetNotesInput(note_ids=list(range(1, 202)))  # 201 IDs > 200 cap
    assert len(GetNotesInput(note_ids=list(range(1, 201))).note_ids) == 200


def test_search_notes_input_fields_validation():
    SearchNotesInput(query="deck:Default", fields=["Front", "Back"])  # ok
    with pytest.raises(ValidationError):
        SearchNotesInput(query="deck:Default", fields=[])  # empty = omit it instead
    with pytest.raises(ValidationError):
        SearchNotesInput(query="deck:Default", fields=[""])
    with pytest.raises(ValidationError):
        SearchNotesInput(query="deck:Default", fields=[f"f{i}" for i in range(21)])  # > 20
    with pytest.raises(ValidationError):
        SearchNotesInput(query="deck:Default", fields=["Front\ninjected"])  # control chars


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
async def test_add_notes_duplicate_value_with_quotes_and_spaces(env):
    """A first-field value containing spaces and double quotes is still found.

    The search term is the combined-quoted ``"field:value"`` form with embedded
    quotes escaped (``\\"``); the duplicate failure message stays byte-identical
    to the plain-value case.
    """
    reg, manager = env
    tricky = 'He said "hello world" loudly'
    await reg.run("alice", add_basic_note, tricky, "a")

    notes = [NoteInput(deck_name="Default", model_name="Basic", fields={"Front": tricky, "Back": "b"})]
    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is False
    assert res.local_status == "failed"
    # Byte-identical to the plain-value duplicate failure
    assert res.error == (
        "Duplicate check failed: note index(es) [0] would create duplicate cards. "
        "Set allow_duplicates=True if duplicates are intended."
    )

    # allow_duplicates=True goes through and really creates the second copy
    res2 = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=True)
    assert res2.success is True
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 2


@pytest.mark.asyncio
async def test_add_notes_duplicate_spaced_field_name_detected(env):
    """A model whose FIRST FIELD NAME contains a space is searched correctly.

    ``Field:"value"`` silently misses when the field name is unquoted (parsed
    as separate terms); the combined-quoted ``"field:value"`` term handles it.
    """
    reg, manager = env

    def _make_model(col):
        mm = col.models
        m = mm.new(name="Spaced")
        mm.add_field(m, mm.new_field(name="Question Text"))
        mm.add_field(m, mm.new_field(name="Answer"))
        t = mm.new_template("Card 1")
        t["qfmt"] = "{{Question Text}}"
        t["afmt"] = "{{Answer}}"
        mm.add_template(m, t)
        mm.add(m)

    await reg.run("alice", _make_model)

    fields = {"Question Text": "what now", "Answer": "this"}
    first = await note_tools.add_notes(
        manager, reg, "alice", [NoteInput(deck_name="Default", model_name="Spaced", fields=fields)]
    )
    assert first.success is True

    dup = await note_tools.add_notes(
        manager, reg, "alice",
        [NoteInput(deck_name="Default", model_name="Spaced", fields=dict(fields))],
        allow_duplicates=False,
    )
    assert dup.success is False
    assert "Duplicate check failed" in dup.error


@pytest.mark.asyncio
async def test_add_notes_intra_batch_duplicates_rejected(env):
    """Two identical notes inside ONE batch fail the pass-1 seen-set gate.

    The pre-existing-collection duplicate check cannot see them (neither note
    exists yet), so without this gate the batch would silently create N
    identical notes. The failure is the same 'Duplicate check failed' error
    the collection check raises, and NOTHING is created.
    """
    reg, manager = env
    notes = [
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "same-q", "Back": "a"}),
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "same-q", "Back": "b"}),
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is False
    assert res.local_status == "failed"
    assert "Duplicate check failed" in res.error
    assert "note index(es) [1]" in res.error  # the SECOND copy is flagged
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 0  # zero notes created

    # allow_duplicates=True still creates both.
    res2 = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=True)
    assert res2.success is True, res2.error
    assert res2.data["count"] == 2
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 2


@pytest.mark.asyncio
async def test_add_notes_intra_batch_gate_normalizes_deck_and_type_variants(env):
    """Case/padding variants of one deck or note type cannot evade the gate.

    The wheel resolves deck and note-type names case- and padding-insensitively
    (decks.id("default") and decks.id(" Default ") both return the Default deck;
    models.by_name("basic") returns Basic), so raw-string keys would let variants
    of one physical deck create duplicates the gate exists to refuse. The key
    normalizes both components the same way.
    """
    reg, manager = env
    notes = [
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "same-q", "Back": "a"}),
        NoteInput(deck_name=" default ", model_name="basic", fields={"Front": "same-q", "Back": "b"}),
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is False
    assert res.local_status == "failed"
    assert "Duplicate check failed" in res.error
    assert "note index(es) [1]" in res.error  # only the SECOND copy is flagged, once
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 0  # zero notes created


@pytest.mark.asyncio
async def test_add_notes_empty_first_fields_exempt_from_intra_batch_gate(env):
    """Notes whose stripped first field is empty are exempt, mirroring the
    collection check's early return: two blank-first-field notes in one batch
    are not intra-batch duplicates."""
    reg, manager = env
    notes = [
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "   ", "Back": "a"}),
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "", "Back": "b"}),
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes, allow_duplicates=False)
    assert res.success is True, res.error
    assert res.data["count"] == 2


def test_note_input_rejects_malformed_deck_names():
    """NoteInput now rejects the same deck-name shapes create_deck/rename_deck
    reject, so add_notes cannot create the mangling shapes (':::' runs, empty
    hierarchy components, control characters, >200 chars)."""
    for bad in ("A:::B", "Bad\x01Name", "x" * 201):
        with pytest.raises(ValidationError):
            NoteInput(deck_name=bad, note_type="Basic", fields={"Front": "q", "Back": "a"})
    # Sanity: a well-formed hierarchical name still passes.
    assert NoteInput(deck_name="Parent::Child", note_type="Basic",
                     fields={"Front": "q", "Back": "a"}).deck_name == "Parent::Child"


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


# -- add_notes pass-1 field validation (all-or-nothing batches) -------------------


@pytest.mark.asyncio
async def test_add_notes_unknown_field_rejected_before_write(env):
    """An unknown field name fails deterministically in pass 1: no partial state.

    Regression for partial-batch-failure-record-poisoning: the field check runs
    BEFORE any col.add_note, so the 'failed' ledger record implies nothing was
    applied (the wheel's KeyError can no longer fire mid-pass-2).
    """
    reg, manager = env
    notes = [
        NoteInput(deck_name="Default", model_name="Basic",
                  fields={"Front": "q1", "Back": "a1", "BogusField": "x"})
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes, idempotency_key="K1")
    assert res.success is False
    assert res.local_status == "failed"
    # Structured error: index, unknown fields, and the valid fields are named.
    assert "Note index 0" in res.error
    assert "BogusField" in res.error
    assert "Front" in res.error and "Back" in res.error

    # ZERO notes created.
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 0

    # The ledger records the deterministic failure for the key...
    entry = await reg.ledger("alice").get("K1")
    assert entry is not None
    assert entry["status"] == "failed"
    assert "BogusField" in entry["error"]
    # ...and a same-key retry deterministically replays it (nothing applied).
    replay = await note_tools.add_notes(manager, reg, "alice", notes, idempotency_key="K1")
    assert replay.success is False
    assert "Idempotent operation replay" in replay.warnings[0]
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 0


@pytest.mark.asyncio
async def test_add_notes_valid_plus_unknown_field_batch_applies_nothing(env):
    """A batch of [valid note, unknown-field note] applies NOTHING (all-or-nothing)."""
    reg, manager = env
    notes = [
        NoteInput(deck_name="Default", model_name="Basic",
                  fields={"Front": "valid-q", "Back": "a"}),
        NoteInput(deck_name="Default", model_name="Basic",
                  fields={"Front": "q2", "Back": "a2", "BogusField": "x"}),
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes)
    assert res.success is False
    assert "Note index 1" in res.error
    assert "BogusField" in res.error
    # The otherwise-valid note 0 was NOT applied either.
    count = await reg.run("alice", lambda col: len(col.find_notes("*")))
    assert count == 0


@pytest.mark.asyncio
async def test_add_notes_field_validation_precedes_duplicate_gate(env):
    """Field validation is deterministic and runs before the duplicate check.

    The duplicate gate itself is unchanged (covered by the duplicate tests
    above); this pins only the precedence within pass 1.
    """
    reg, manager = env
    await reg.run("alice", add_basic_note, "dup-q", "a")
    notes = [
        NoteInput(deck_name="Default", model_name="Basic",
                  fields={"Front": "dup-q", "BogusField": "x"})
    ]
    res = await note_tools.add_notes(manager, reg, "alice", notes)
    assert res.success is False
    assert "BogusField" in res.error
    assert "Duplicate check failed" not in res.error


@pytest.mark.asyncio
async def test_add_notes_all_valid_batch_still_applies_after_field_check(env):
    """Sanity: the new pass-1 check does not reject valid field dicts."""
    reg, manager = env
    notes = [NoteInput(deck_name="Default", model_name="Basic",
                       fields={"Front": "ok-q", "Back": "ok-a"})]
    res = await note_tools.add_notes(manager, reg, "alice", notes, idempotency_key="KOK")
    assert res.success is True
    assert res.data["count"] == 1
    entry = await reg.ledger("alice").get("KOK")
    assert entry is not None and entry["status"] == "committed"


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


def test_add_tags_input_bounds():
    """AddTagsInput shares _IdListInput's 500-ID cap and remove_tags' tag rules.

    Deliberate tightening: whitespace-bearing tags used to be silently
    stripped; they are now rejected, exactly like remove_tags rejects them.
    """
    # 501 note IDs rejected; 500 pass.
    with pytest.raises(ValidationError, match="Batches are limited to 500"):
        AddTagsInput(note_ids=list(range(1, 502)), tags=["t"])
    ok = AddTagsInput(note_ids=list(range(1, 501)), tags=["t"])
    assert ok.note_ids == list(range(1, 501))
    # 51 tags rejected; 50 pass.
    with pytest.raises(ValidationError, match="between 1 and 50 tags"):
        AddTagsInput(note_ids=[1], tags=[f"t{i}" for i in range(51)])
    assert len(AddTagsInput(note_ids=[1], tags=[f"t{i}" for i in range(50)]).tags) == 50
    # Whitespace-bearing and control-character tags are rejected (no stripping).
    with pytest.raises(ValidationError, match="must not contain whitespace"):
        AddTagsInput(note_ids=[1], tags=["two words"])
    with pytest.raises(ValidationError, match="control characters"):
        AddTagsInput(note_ids=[1], tags=["bad\x01tag"])


# -- card selection input shape ------------------------------------------------------


def test_card_selection_error_names_card_ids():
    """A card_ids-vs-query violation reports card_ids, not note_ids: the shared
    XOR rule's error names the field the caller actually sent."""
    with pytest.raises(ValidationError) as exc:
        BuryCardsInput(card_ids=[1, 2], query="deck:Default")
    assert "card_ids" in str(exc.value)
    assert "note_ids" not in str(exc.value)


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
