"""Tests for the 18 new MCP tools (decks/notes/cards/media, tool stage 2).

Same contract as test_mcp_tools.py: the REAL tool functions run against REAL
per-user anki collections in a tmp data dir — no mocks (the one exception is
the fail-closed backup test, where the snapshot helper itself is monkeypatched
to raise). Snapshot assertions go through the registry's list_backups from the
previous stage.
"""

import base64
import os

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.models import (
    AnswerCardInput,
    CreateDeckInput,
    CreateNoteTypeInput,
    NoteInput,
    RemoveTagsInput,
    SetDueDateInput,
    TemplateInput,
)
from src.sync_manager import SyncManager
from src.users import UserStore
from src.tools import cards as card_tools
from src.tools import decks as deck_tools
from src.tools import media as media_tools
from src.tools import notes as note_tools


def add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return note.id


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data_dir."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    # Real (empty) user store: the mutation path reads it for the local-only nudge
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


def _backup_reasons(reg, username):
    return [entry["reason"] for entry in reg.list_backups(username)]


def _first_card_id(reg, username, query="deck:Default"):
    async def _get():
        res = await card_tools.find_cards(reg, username, query)
        return res["cards"][0]["card_id"]

    return _get


# -- create_deck -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deck_and_subdeck_appear_in_listing(env):
    reg, manager = env
    res = await deck_tools.create_deck(manager, reg, "alice", "Languages")
    assert res.success is True and res.local_status == "committed"
    assert res.data["deck_name"] == "Languages"
    deck_id = res.data["deck_id"]

    # "P::C" makes the parent too: the listing then shows both.
    res2 = await deck_tools.create_deck(manager, reg, "alice", "Languages::Vocab")
    assert res2.success is True
    listing = await deck_tools.list_decks(reg, "alice")
    names = {d["name"] for d in listing["decks"]}
    assert {"Languages", "Languages::Vocab", "Default"} <= names
    # Parent and child have distinct ids.
    assert res2.data["deck_id"] != deck_id

    # Re-creating an existing name resolves to the SAME id (probed wheel behavior).
    again = await deck_tools.create_deck(manager, reg, "alice", "Languages")
    assert again.success is True
    assert again.data["deck_id"] == deck_id


@pytest.mark.asyncio
async def test_create_deck_rejects_bad_names_before_any_write(env):
    reg, manager = env
    for bad in ("", "::lead", "trail::", "a:::b"):
        with pytest.raises(ValidationError):
            CreateDeckInput(deck_name=bad)
    # Nothing was created for the rejected names.
    listing = await deck_tools.list_decks(reg, "alice")
    assert {d["name"] for d in listing["decks"]} == {"Default"}


# -- rename_deck -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_deck_requires_confirm_and_snapshots_before_renaming_children(env):
    reg, manager = env
    await deck_tools.create_deck(manager, reg, "rena", "P::C")
    nid = await reg.run("rena", add_basic_note, "child note", "a", deck="P::C")
    decks = {d["name"]: d["id"] for d in (await deck_tools.list_decks(reg, "rena"))["decks"]}
    cid = (await card_tools.find_cards(reg, "rena", 'deck:"P::C"'))["cards"][0]["card_id"]

    # confirm=False: skipped, nothing renamed, no snapshot taken.
    skipped = await deck_tools.rename_deck(manager, reg, "rena", decks["P"], "M", confirm=False)
    assert skipped.success is False
    assert skipped.local_status == "skipped"
    assert "confirm=true" in skipped.error
    assert _backup_reasons(reg, "rena") == []
    listing = {d["name"] for d in (await deck_tools.list_decks(reg, "rena"))["decks"]}
    assert "P::C" in listing and "M::C" not in listing

    # confirm=True: fail-closed snapshot first, then the rename (children follow).
    res = await deck_tools.rename_deck(manager, reg, "rena", decks["P"], "M", confirm=True)
    assert res.success is True
    assert res.data == {"deck_id": decks["P"], "old_name": "P", "new_name": "M"}
    assert "pre-rename-deck" in _backup_reasons(reg, "rena")

    # The child deck AND the note's deck follow the rename.
    listing = {d["name"] for d in (await deck_tools.list_decks(reg, "rena"))["decks"]}
    assert "P" not in listing and "P::C" not in listing
    assert "M::C" in listing
    deck_name = await reg.run("rena", lambda col: col.decks.name(col.get_card(cid).did))
    assert deck_name == "M::C"
    # ...and the note survived under its new deck name.
    assert await reg.run("rena", lambda col: col.get_note(nid)["Front"]) == "child note"


@pytest.mark.asyncio
async def test_rename_deck_unknown_id_fails_without_snapshot(env):
    reg, manager = env
    res = await deck_tools.rename_deck(manager, reg, "rena", 999999, "Whatever", confirm=True)
    assert res.success is False
    assert "not found" in res.error
    assert _backup_reasons(reg, "rena") == []


# -- delete_deck -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_deck_refuses_default_even_with_confirm(env):
    reg, manager = env
    for confirm in (False, True):
        res = await deck_tools.delete_deck(manager, reg, "dana", 1, confirm=confirm)
        assert res.success is False
        assert res.local_status == "skipped"
        assert "Default deck cannot be deleted" in res.error
    listing = await deck_tools.list_decks(reg, "dana")
    assert "Default" in {d["name"] for d in listing["decks"]}


@pytest.mark.asyncio
async def test_delete_deck_requires_confirm(env):
    reg, manager = env
    created = await deck_tools.create_deck(manager, reg, "dana", "Doomed")
    res = await deck_tools.delete_deck(manager, reg, "dana", created.data["deck_id"], confirm=False)
    assert res.success is False and res.local_status == "skipped"
    assert "confirm=true" in res.error
    assert _backup_reasons(reg, "dana") == []
    listing = await deck_tools.list_decks(reg, "dana")
    assert "Doomed" in {d["name"] for d in listing["decks"]}


@pytest.mark.asyncio
async def test_delete_deck_snapshots_and_counts_cards(env):
    reg, manager = env
    await deck_tools.create_deck(manager, reg, "dana", "Doomed::Kid")  # creates parent + child
    await reg.run("dana", add_basic_note, "q1", "a1", deck="Doomed::Kid")
    await reg.run("dana", add_basic_note, "q2", "a2", deck="Doomed::Kid")
    decks = {d["name"]: d["id"] for d in (await deck_tools.list_decks(reg, "dana"))["decks"]}

    # Deleting the PARENT removes the child deck and its cards too (wheel behavior).
    res = await deck_tools.delete_deck(manager, reg, "dana", decks["Doomed"], confirm=True)

    assert res.success is True
    assert res.data["deck_name"] == "Doomed"
    assert res.data["cards_removed"] == 2
    # The pre-delete snapshot exists with the exact reason-tagged filename.
    entries = [e for e in reg.list_backups("dana") if e["reason"] == "pre-delete-deck"]
    assert len(entries) == 1
    assert entries[0]["filename"].endswith("-pre-delete-deck.colpkg")
    listing = {d["name"] for d in (await deck_tools.list_decks(reg, "dana"))["decks"]}
    assert "Doomed" not in listing and "Doomed::Kid" not in listing


@pytest.mark.asyncio
async def test_delete_deck_unknown_id_fails(env):
    reg, manager = env
    res = await deck_tools.delete_deck(manager, reg, "dana", 999999, confirm=True)
    assert res.success is False
    assert "not found" in res.error


# -- delete_notes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_notes_confirm_gate_snapshot_and_missing_ids(env):
    reg, manager = env
    nid1 = await reg.run("evan", add_basic_note, "gone-1", "a")
    nid2 = await reg.run("evan", add_basic_note, "gone-2", "a")
    survivor = await reg.run("evan", add_basic_note, "stays", "a")

    # confirm=False: nothing happens, no snapshot.
    skipped = await note_tools.delete_notes(manager, reg, "evan", [nid1], confirm=False)
    assert skipped.success is False and skipped.local_status == "skipped"
    assert "confirm=true" in skipped.error
    assert _backup_reasons(reg, "evan") == []

    res = await note_tools.delete_notes(manager, reg, "evan", [nid1, nid2, 987654], confirm=True)

    assert res.success is True
    assert res.data == {"requested": 3, "removed": 2, "missing": [987654]}
    entries = [e for e in reg.list_backups("evan") if e["reason"] == "pre-delete-notes"]
    assert len(entries) == 1
    assert entries[0]["filename"].endswith("-pre-delete-notes.colpkg")
    remaining = await note_tools.get_note(reg, "evan", survivor)
    assert remaining["fields"]["Front"] == "stays"
    with pytest.raises(CollectionToolError, match="not found"):
        await note_tools.get_note(reg, "evan", nid1)


@pytest.mark.asyncio
async def test_delete_notes_aborts_fail_closed_when_snapshot_fails(env, monkeypatch):
    """Regression for the fail-closed gate: if the pre-delete snapshot raises,
    the mutation fails and NOTHING is deleted."""
    reg, manager = env
    nid1 = await reg.run("fred", add_basic_note, "keep-1", "a")
    nid2 = await reg.run("fred", add_basic_note, "keep-2", "a")

    def _boom(col, username, reason):
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(reg, "_backup_locked", _boom)

    res = await note_tools.delete_notes(manager, reg, "fred", [nid1, nid2], confirm=True)

    assert res.success is False
    assert res.local_status == "failed"
    assert "simulated snapshot failure" in res.error
    # Both notes are still there.
    count = await reg.run("fred", lambda col: len(col.find_notes("*")))
    assert count == 2


# -- remove_tags ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_tags_removes_only_requested(env):
    reg, manager = env
    nid = await reg.run("gail", add_basic_note, "q", "a")

    def _tag(col):
        col.tags.bulk_add([nid], "keepme dropme")

    await reg.run("gail", _tag)

    res = await note_tools.remove_tags(manager, reg, "gail", [nid], ["dropme"])
    assert res.success is True
    assert res.data == {"note_ids": [nid], "tags_removed": ["dropme"]}
    tags = await reg.run("gail", lambda col: list(col.get_note(nid).tags))
    assert tags == ["keepme"]


@pytest.mark.asyncio
async def test_remove_tags_missing_note_rejected(env):
    reg, manager = env
    res = await note_tools.remove_tags(manager, reg, "gail", [424242], ["x"])
    assert res.success is False
    assert "not found" in res.error


def test_remove_tags_input_rejects_whitespace_tags_and_over_500_ids():
    with pytest.raises(ValidationError):
        RemoveTagsInput(note_ids=[1], tags=["has space"])
    with pytest.raises(ValidationError):
        RemoveTagsInput(note_ids=list(range(501)), tags=["x"])  # cap 500
    with pytest.raises(ValidationError):
        RemoveTagsInput(note_ids=[1], tags=[f"t{i}" for i in range(51)])  # cap 50


# -- list_note_types / create_note_type ------------------------------------------------


@pytest.mark.asyncio
async def test_list_note_types_lists_basic(env):
    reg, _ = env
    data = await note_tools.list_note_types(reg, "hiro")
    names = {m["name"] for m in data["note_types"]}
    assert "Basic" in names
    basic = next(m for m in data["note_types"] if m["name"] == "Basic")
    assert set(basic) == {"name", "id", "fields", "templates"}
    assert set(basic["fields"]) >= {"Front", "Back"}
    assert len(basic["templates"]) >= 1


@pytest.mark.asyncio
async def test_create_note_type_default_and_explicit_templates_readable_back(env):
    reg, manager = env

    # Default template over the first two fields.
    res = await note_tools.create_note_type(manager, reg, "hiro", "VocabCard", ["Question", "Answer", "Hint"])
    assert res.success is True
    assert res.data["name"] == "VocabCard"
    assert res.data["fields"] == ["Question", "Answer", "Hint"]
    assert res.data["template_count"] == 1
    listed = await note_tools.list_note_types(reg, "hiro")
    vocab = next(m for m in listed["note_types"] if m["name"] == "VocabCard")
    assert vocab["fields"] == ["Question", "Answer", "Hint"]
    assert vocab["templates"] == ["Card 1"]

    # Explicit templates are stored verbatim.
    res2 = await note_tools.create_note_type(
        manager, reg, "hiro", "TwoWay",
        ["Front", "Back"],
        templates=[
            TemplateInput(name="Forward", front="{{Front}}", back="{{Back}}"),
            TemplateInput(name="Reverse", front="{{Back}}", back="{{Front}}"),
        ],
    )
    assert res2.success is True
    assert res2.data["template_count"] == 2
    listed = await note_tools.list_note_types(reg, "hiro")
    twoway = next(m for m in listed["note_types"] if m["name"] == "TwoWay")
    assert twoway["templates"] == ["Forward", "Reverse"]

    # The default-template note type can actually serve notes.
    added = await note_tools.add_notes(
        manager, reg, "hiro",
        [NoteInput(deck_name="Default", model_name="VocabCard",
                   fields={"Question": "q", "Answer": "a", "Hint": "h"})],
    )
    assert added.success is True, added.error


@pytest.mark.asyncio
async def test_create_note_type_refuses_duplicate_name(env):
    reg, manager = env
    first = await note_tools.create_note_type(manager, reg, "iris", "Mine", ["F1", "F2"])
    assert first.success is True
    dup = await note_tools.create_note_type(manager, reg, "iris", "Mine", ["F1", "F2"])
    assert dup.success is False
    assert "already exists" in dup.error


@pytest.mark.asyncio
async def test_create_note_type_validations(env):
    reg, manager = env
    # Duplicate field names are rejected by the input model.
    with pytest.raises(ValidationError):
        CreateNoteTypeInput(name="X", fields=["A", "A"])
    # The default template needs two fields (rejected before any I/O, like
    # add_notes' empty-batch gate on direct calls).
    with pytest.raises(CollectionToolError, match="at least 2 fields"):
        await note_tools.create_note_type(manager, reg, "iris", "Single", ["Only"])
    listed = await note_tools.list_note_types(reg, "iris")
    assert "Single" not in {m["name"] for m in listed["note_types"]}


# -- find_cards / get_card --------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_cards_pagination_envelope(env):
    reg, _ = env
    await reg.run("jack", add_basic_note, "c-q1", "c-a1")
    await reg.run("jack", add_basic_note, "c-q2", "c-a2")

    res = await card_tools.find_cards(reg, "jack", "deck:Default")
    assert set(res) == {"total_matches", "returned_count", "offset", "limit", "cards"}
    assert res["total_matches"] == 2
    assert res["returned_count"] == 2
    row = res["cards"][0]
    assert set(row) == {
        "card_id", "note_id", "deck_name", "queue", "type", "due", "ivl", "reps", "lapses",
    }
    assert row["deck_name"] == "Default"
    assert row["queue"] == 0 and row["reps"] == 0

    paged = await card_tools.find_cards(reg, "jack", "deck:Default", limit=1, offset=1)
    assert paged["returned_count"] == 1 and paged["offset"] == 1


@pytest.mark.asyncio
async def test_get_card_fields_question_answer_and_truncation(env):
    reg, _ = env
    nid = await reg.run("kate", add_basic_note, "short question", "me")

    # Untruncated first: rendered question/answer carry the field content.
    cid = (await card_tools.find_cards(reg, "kate", "deck:Default"))["cards"][0]["card_id"]
    card = await card_tools.get_card(reg, "kate", cid)
    assert card["card_id"] == cid and card["note_id"] == nid
    assert card["deck_name"] == "Default"
    assert card["model_name"] == "Basic"
    assert card["fields"]["Back"] == "me"
    assert "short question" in card["question"] and card["truncated"] is False
    assert "me" in card["answer"]
    assert card["queue"] == 0 and card["type"] == 0
    assert card["ivl"] == 0 and card["reps"] == 0 and card["lapses"] == 0
    assert card["flags"] == 0

    # A field bloated past the 10000-char cap comes back capped, flagged.
    def _bloat_front(col):
        note = col.get_note(nid)
        note["Front"] = "Q" * 11000
        col.update_note(note)

    await reg.run("kate", _bloat_front)
    bloated = await card_tools.get_card(reg, "kate", cid)
    assert len(bloated["question"]) == 10000
    assert bloated["truncated"] is True


@pytest.mark.asyncio
async def test_get_card_unknown_id_raises_domain_error(env):
    reg, _ = env
    with pytest.raises(CollectionToolError, match="not found"):
        await card_tools.get_card(reg, "kate", 987654321)


# -- suspend / unsuspend ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspend_unsuspend_round_trip(env):
    reg, manager = env
    await reg.run("liam", add_basic_note, "q", "a")
    cid = (await card_tools.find_cards(reg, "liam", "deck:Default"))["cards"][0]["card_id"]

    res = await card_tools.suspend_cards(manager, reg, "liam", [cid])
    assert res.success is True
    assert res.data["suspended"] == 1 and res.data["missing"] == []
    queue = await reg.run("liam", lambda col: col.get_card(cid).queue)
    assert queue == -1  # suspended (probed wheel value)

    # A suspended card is not in the review queue.
    q = await card_tools.get_review_queue(reg, "liam")
    assert cid not in [c["card_id"] for c in q["cards"]]

    res2 = await card_tools.unsuspend_cards(manager, reg, "liam", [cid])
    assert res2.success is True
    assert res2.data["unsuspended"] == 1
    queue = await reg.run("liam", lambda col: col.get_card(cid).queue)
    assert queue == 0  # back to new


@pytest.mark.asyncio
async def test_suspend_cards_reports_missing_and_fails_when_none_exist(env):
    reg, manager = env
    await reg.run("liam", add_basic_note, "q", "a")
    cid = (await card_tools.find_cards(reg, "liam", "deck:Default"))["cards"][0]["card_id"]

    mixed = await card_tools.suspend_cards(manager, reg, "liam", [cid, 555555])
    assert mixed.success is True
    assert mixed.data["card_ids"] == [cid] and mixed.data["missing"] == [555555]

    none = await card_tools.suspend_cards(manager, reg, "liam", [555555])
    assert none.success is False
    assert "None of the requested cards exist" in none.error


# -- set_due_date / move_cards ------------------------------------------------------------


def test_set_due_date_input_rejects_garbage():
    SetDueDateInput(card_ids=[1], days="3")  # ok
    SetDueDateInput(card_ids=[1], days="0-2")  # ok: random range
    for bad in ("abc", "-3", "3.5", ""):
        with pytest.raises(ValidationError):
            SetDueDateInput(card_ids=[1], days=bad)


@pytest.mark.asyncio
async def test_set_due_date_moves_due_forward(env):
    reg, manager = env
    await reg.run("mona", add_basic_note, "q", "a")
    cid = (await card_tools.find_cards(reg, "mona", "deck:Default"))["cards"][0]["card_id"]

    res = await card_tools.set_due_date(manager, reg, "mona", [cid], "5")
    assert res.success is True
    assert res.data["days"] == "5" and res.data["updated"] == 1
    due = await reg.run("mona", lambda col: col.get_card(cid).due)
    assert due == 5  # new cards carry day offsets as their due value


@pytest.mark.asyncio
async def test_move_cards_moves_to_existing_deck_only(env):
    reg, manager = env
    await deck_tools.create_deck(manager, reg, "nadia", "Target")
    await reg.run("nadia", add_basic_note, "q", "a")
    cid = (await card_tools.find_cards(reg, "nadia", "deck:Default"))["cards"][0]["card_id"]
    target_id = next(
        d["id"] for d in (await deck_tools.list_decks(reg, "nadia"))["decks"] if d["name"] == "Target"
    )

    res = await card_tools.move_cards(manager, reg, "nadia", [cid], target_id)
    assert res.success is True
    assert res.data == {"card_ids": [cid], "missing": [], "moved": 1, "deck_id": target_id}
    deck_name = await reg.run("nadia", lambda col: col.decks.name(col.get_card(cid).did))
    assert deck_name == "Target"

    bad = await card_tools.move_cards(manager, reg, "nadia", [cid], 987654)
    assert bad.success is False
    assert "not found" in bad.error


# -- review queue / answer_card -------------------------------------------------------


@pytest.mark.asyncio
async def test_review_queue_contains_new_card_and_answer_card_answers_it(env):
    reg, manager = env
    await reg.run("omar", add_basic_note, "due-q", "due-a")
    cid = (await card_tools.find_cards(reg, "omar", "deck:Default"))["cards"][0]["card_id"]

    q = await card_tools.get_review_queue(reg, "omar")
    assert set(q) == {"total_queue_count", "cards"}
    assert q["total_queue_count"] >= 1
    entry = next(c for c in q["cards"] if c["card_id"] == cid)
    assert set(entry) == {"card_id", "note_id", "deck_name", "question"}
    assert entry["deck_name"] == "Default"
    assert "due-q" in entry["question"]
    assert len(entry["question"]) <= 2000

    revlog_before = await reg.run("omar", lambda col: col.db.scalar("SELECT COUNT() FROM revlog"))
    res = await card_tools.answer_card(manager, reg, "omar", cid, 3)  # 3 = Good

    assert res.success is True, res.error
    assert res.data["card_id"] == cid
    assert res.data["rating"] == 3
    assert isinstance(res.data["next_due"], int)
    assert res.data["remaining_in_queue"] == 0
    # The answer really landed in the review log and moved the card into the
    # learning pipeline (type 1; a GOOD-answered NEW card returns to today's
    # queue later, so queue absence is NOT asserted).
    revlog_after = await reg.run("omar", lambda col: col.db.scalar("SELECT COUNT() FROM revlog"))
    assert revlog_after == revlog_before + 1
    ctype, reps = await reg.run("omar", lambda col: (col.get_card(cid).type, col.get_card(cid).reps))
    assert ctype == 1 and reps == 1


@pytest.mark.asyncio
async def test_answer_card_refuses_card_not_in_queue(env):
    reg, manager = env
    await reg.run("omar", add_basic_note, "q", "a")

    res = await card_tools.answer_card(manager, reg, "omar", 987654321, 3)

    assert res.success is False
    assert "not currently in the review queue" in res.error


def test_answer_card_rating_input_bounded():
    AnswerCardInput(card_id=1, rating=1)
    AnswerCardInput(card_id=1, rating=4)
    for bad in (0, 5, -1):
        with pytest.raises(ValidationError):
            AnswerCardInput(card_id=1, rating=bad)


# -- media ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_store_get_round_trip(env):
    reg, manager = env
    payload = "hello media 🌍".encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")

    res = await media_tools.store_media_file(manager, reg, "pete", "notes.txt", encoded)
    assert res.success is True, res.error
    assert res.data["filename"] == "notes.txt"

    back = await media_tools.get_media_file(reg, "pete", "notes.txt")
    assert back["filename"] == "notes.txt"
    assert back["size_bytes"] == len(payload)
    assert base64.b64decode(back["data_base64"]) == payload


@pytest.mark.asyncio
async def test_media_get_refuses_traversal_and_missing_files(env):
    reg, manager = env
    await media_tools.store_media_file(manager, reg, "pete", "ok.txt", base64.b64encode(b"x").decode())

    for bad in ("../x", "a/b", "..", "back\\slash"):
        with pytest.raises(CollectionToolError):
            await media_tools.get_media_file(reg, "pete", bad)
        with pytest.raises(CollectionToolError):
            await media_tools.store_media_file(manager, reg, "pete", bad, base64.b64encode(b"x").decode())

    with pytest.raises(CollectionToolError, match="not found"):
        await media_tools.get_media_file(reg, "pete", "nonexistent.txt")


@pytest.mark.asyncio
async def test_media_refuses_oversize_payload_and_download(env):
    reg, manager = env
    too_big = b"\x00" * (4 * 1024 * 1024 + 1)  # >4 MiB generated bytes

    # Upload path: the store validation refuses anything over 4 MiB decoded
    # BEFORE any mutation (same direct-call raise semantics as add_notes' gates).
    with pytest.raises(CollectionToolError, match="too large"):
        await media_tools.store_media_file(
            manager, reg, "quinn", "big.bin", base64.b64encode(too_big).decode("ascii")
        )
    listing = await media_tools.list_media_files(reg, "quinn")
    assert "big.bin" not in listing["files"]

    # Download path: plant an over-cap file directly on disk (the store path
    # can never create one) and read it back.
    def _plant(col):
        with open(os.path.join(col.media.dir(), "huge.bin"), "wb") as f:
            f.write(too_big)

    await reg.run("quinn", _plant)
    with pytest.raises(CollectionToolError, match="too large to return"):
        await media_tools.get_media_file(reg, "quinn", "huge.bin")

    # Invalid base64 is a domain error before any I/O (nothing stored).
    with pytest.raises(CollectionToolError, match="base64"):
        await media_tools.store_media_file(manager, reg, "quinn", "bad.bin", "!!!not-base64!!!")
    listing = await media_tools.list_media_files(reg, "quinn")
    assert "bad.bin" not in listing["files"]


@pytest.mark.asyncio
async def test_list_media_files_caps_and_sorts(env):
    reg, manager = env
    for name in ("c.txt", "a.txt", "b.txt"):
        res = await media_tools.store_media_file(
            manager, reg, "ruth", name, base64.b64encode(b"data").decode("ascii")
        )
        assert res.success is True, res.error

    full = await media_tools.list_media_files(reg, "ruth")
    assert full == {"count": 3, "total": 3, "truncated": False, "files": ["a.txt", "b.txt", "c.txt"]}

    capped = await media_tools.list_media_files(reg, "ruth", limit=2)
    assert capped == {"count": 2, "total": 3, "truncated": True, "files": ["a.txt", "b.txt"]}


# -- review-gate regressions: snapshot collisions and idempotent replays --------


@pytest.mark.asyncio
async def test_same_second_same_reason_snapshots_never_overwrite(env):
    """Collision regression: two snapshots with the same reason within one
    wall-clock second must BOTH survive — os.rename silently replaces on
    POSIX, which used to destroy the earlier (pre-change) snapshot."""
    reg, manager = env
    await reg.run("carl", add_basic_note, "v1", "a")
    first = await reg.backup_user("carl", "manual")
    await reg.run("carl", add_basic_note, "v2", "a")
    second = await reg.backup_user("carl", "manual")

    # Distinct names (new second, or the numeric collision suffix), and both
    # files really exist on disk.
    assert first != second
    backup_dir = os.path.join(reg.data_dir, "users", "carl", "backups")
    assert os.path.exists(os.path.join(backup_dir, first))
    assert os.path.exists(os.path.join(backup_dir, second))
    # Both parse as listing entries (the suffixed reason includes the digit).
    names = {entry["filename"] for entry in reg.list_backups("carl")}
    assert {first, second} <= names


@pytest.mark.asyncio
async def test_delete_notes_idempotency_key_replays_cached_result(env):
    reg, manager = env
    nid1 = await reg.run("ivan", add_basic_note, "r1", "a")
    nid2 = await reg.run("ivan", add_basic_note, "r2", "a")

    first = await note_tools.delete_notes(
        manager, reg, "ivan", [nid1], confirm=True, idempotency_key="del-once"
    )
    assert first.success is True, first.error
    snapshots_after_first = [
        e for e in reg.list_backups("ivan") if e["reason"].startswith("pre-delete-notes")
    ]

    # Same key, DIFFERENT payload: the ledger must replay the cached result
    # without deleting anything new or taking another snapshot.
    replay = await note_tools.delete_notes(
        manager, reg, "ivan", [nid2], confirm=True, idempotency_key="del-once"
    )
    assert replay.success is True
    assert replay.local_status == "committed"
    assert replay.data == first.data
    assert "Idempotent operation replay" in (replay.warnings or [""])[0]
    still = await note_tools.get_note(reg, "ivan", nid2)
    assert still["fields"]["Front"] == "r2"
    assert [
        e for e in reg.list_backups("ivan") if e["reason"].startswith("pre-delete-notes")
    ] == snapshots_after_first


@pytest.mark.asyncio
async def test_answer_card_idempotency_key_replays_cached_result(env):
    reg, manager = env
    await reg.run("pete", add_basic_note, "q", "a")
    cid = (await card_tools.find_cards(reg, "pete", "deck:Default"))["cards"][0]["card_id"]

    first = await card_tools.answer_card(
        manager, reg, "pete", cid, 3, idempotency_key="ans-once"
    )
    assert first.success is True, first.error
    revlog = await reg.run("pete", lambda col: col.db.scalar("SELECT COUNT() FROM revlog"))
    assert revlog == 1

    replay = await card_tools.answer_card(
        manager, reg, "pete", cid, 3, idempotency_key="ans-once"
    )
    assert replay.success is True
    assert replay.local_status == "committed"
    assert replay.data == first.data
    # The replay did NOT answer the card a second time.
    revlog2 = await reg.run("pete", lambda col: col.db.scalar("SELECT COUNT() FROM revlog"))
    assert revlog2 == 1
