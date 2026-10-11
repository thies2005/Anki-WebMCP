"""Tests for preview_note_type_change and change_note_type (tool stage 3).

Same contract as test_mcp_tools_v2.py: the REAL tool functions run against
REAL per-user anki collections in a tmp data dir — no mocks. The change runs
through the wheel's backend operation (get_single_notetype_of_notes +
change_notetype_info + change_notetype_of_notes), so every assertion below is
about real regenerated cards, real field contents, and real snapshot files.
"""

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.models import ChangeNoteTypeInput, PreviewNoteTypeChangeInput
from src.sync_manager import SyncManager
from src.tools import cards as card_tools
from src.tools import notes as note_tools
from src.users import UserStore


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data dir."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    # Real (empty) user store: the mutation path reads it for the local-only nudge
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


def _backup_reasons(reg, username):
    return [entry["reason"] for entry in reg.list_backups(username)]


def _make_note_type(col, name, fields, cloze=False):
    """A note type with the given fields and a default template over fields[0:2]."""
    mm = col.models
    nt = mm.new(name=name)
    if cloze:
        nt["type"] = 1
    for field_name in fields:
        mm.add_field(nt, mm.new_field(field_name))
    template = mm.new_template("Cloze" if cloze else "Card 1")
    template["qfmt"] = "{{cloze:" + fields[0] + "}}" if cloze else "{{" + fields[0] + "}}"
    template["afmt"] = (
        "{{cloze:" + fields[0] + "}}<br>{{" + fields[1] + "}}"
        if cloze
        else "{{FrontSide}}\n<hr id=answer>\n{{" + fields[1] + "}}"
    )
    mm.add_template(nt, template)
    mm.add(nt)
    return nt


def _add_note_of(col, model_name, values, deck="Default"):
    note = Note(col, col.models.by_name(model_name))
    for field_name, value in values.items():
        note[field_name] = value
    col.add_note(note, col.decks.id(deck))
    return int(note.id)


async def _setup_vocab_user(reg, username):
    """Source type with 3 fields + a 2-field target; one note with content in all three."""

    def _setup(col):
        _make_note_type(col, "SrcVocab", ["Word", "Meaning", "Notes"])
        _make_note_type(col, "TgtVocab", ["Front", "Extra"])
        return _add_note_of(
            col, "SrcVocab", {"Word": "kaufen", "Meaning": "to buy", "Notes": "strong verb"}
        )

    return await reg.run(username, _setup)


# -- happy path + confirm gate -----------------------------------------------------


@pytest.mark.asyncio
async def test_change_note_type_moves_mapped_content_and_snapshots(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "greta")
    before = (await card_tools.find_cards(reg, "greta", "deck:Default"))["cards"]

    res = await note_tools.change_note_type(
        manager, reg, "greta", "TgtVocab", note_ids=[nid],
        field_map={"Front": "Word", "Extra": "Meaning"}, confirm=True,
    )
    assert res.success is True, res.error
    assert res.local_status == "committed"
    assert res.data["old_note_type"] == "SrcVocab"
    assert res.data["new_note_type"] == "TgtVocab"
    assert res.data["changed"] == 1
    assert res.data["field_map"] == {"Front": "Word", "Extra": "Meaning"}
    assert res.data["template_map"] == {"Card 1": "Card 1"}
    assert res.data["snapshot_reason"] == "pre-change-note-type"

    # The note carries the target model; mapped content moved; "Notes" is gone.
    model_name, items = await reg.run(
        "greta", lambda col: (col.get_note(nid).note_type()["name"], dict(col.get_note(nid).items()))
    )
    assert model_name == "TgtVocab"
    assert items == {"Front": "kaufen", "Extra": "to buy"}

    # Cards were regenerated on the new type: same count, real new card.
    after = (await card_tools.find_cards(reg, "greta", "deck:Default"))["cards"]
    assert len(after) == len(before) == 1
    assert await reg.run("greta", lambda col: len(col.get_note(nid).cards())) == 1

    # The fail-closed snapshot ran before the change.
    assert "pre-change-note-type" in _backup_reasons(reg, "greta")


@pytest.mark.asyncio
async def test_change_note_type_without_confirm_is_skipped_and_changes_nothing(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "heidi")

    res = await note_tools.change_note_type(
        manager, reg, "heidi", "TgtVocab", note_ids=[nid], confirm=False
    )
    assert res.success is False
    assert res.local_status == "skipped"
    assert "preview_note_type_change" in res.error
    assert "confirm=true" in res.error
    # No snapshot, and the note is untouched.
    assert _backup_reasons(reg, "heidi") == []
    model_name, items = await reg.run(
        "heidi", lambda col: (col.get_note(nid).note_type()["name"], dict(col.get_note(nid).items()))
    )
    assert model_name == "SrcVocab"
    assert items["Word"] == "kaufen"


# -- preview -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_resolves_maps_and_warns_without_changing_anything(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "isabel")

    res = await note_tools.preview_note_type_change(reg, "isabel", "TgtVocab", note_ids=[nid])
    assert res["old_note_type"] == "SrcVocab"
    assert res["new_note_type"] == "TgtVocab"
    assert res["is_cloze"] is False
    assert res["note_count"] == 1
    # Positional default: Front<-Word, Extra<-Meaning; "Notes" has no target.
    assert res["field_map"] == {"Front": "Word", "Extra": "Meaning"}
    assert res["template_map"] == {"Card 1": "Card 1"}
    assert any("Notes" in w and "discarded" in w for w in res["warnings"])

    # Read-only: no snapshot, no change of type or content.
    assert _backup_reasons(reg, "isabel") == []
    model_name, items = await reg.run(
        "isabel", lambda col: (col.get_note(nid).note_type()["name"], dict(col.get_note(nid).items()))
    )
    assert model_name == "SrcVocab"
    assert items["Notes"] == "strong verb"


# -- mapping semantics -------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_note_type_without_field_map_uses_positional_default(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "jonas")

    res = await note_tools.change_note_type(manager, reg, "jonas", "TgtVocab", note_ids=[nid], confirm=True)
    assert res.success is True, res.error
    assert res.data["field_map"] == {"Front": "Word", "Extra": "Meaning"}
    items = dict(await reg.run("jonas", lambda col: col.get_note(nid).items()))
    assert items == {"Front": "kaufen", "Extra": "to buy"}



@pytest.mark.asyncio
async def test_null_field_map_value_discards_content(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "katja")

    res = await note_tools.change_note_type(
        manager, reg, "katja", "TgtVocab", note_ids=[nid],
        field_map={"Front": None, "Extra": "Word"}, confirm=True,
    )
    assert res.success is True, res.error
    assert res.data["field_map"] == {"Front": None, "Extra": "Word"}
    items = dict(await reg.run("katja", lambda col: col.get_note(nid).items()))
    assert items["Front"] == ""
    assert items["Extra"] == "kaufen"


@pytest.mark.asyncio
async def test_duplicate_old_targets_fail_before_any_write(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "lars")

    res = await note_tools.change_note_type(
        manager, reg, "lars", "TgtVocab", note_ids=[nid],
        field_map={"Front": "Word", "Extra": "Word"}, confirm=True,
    )
    assert res.success is False
    assert res.local_status == "failed"
    assert "same OLD field" in res.error
    # Data intact and NO snapshot: validation fails before the snapshot runs.
    model_name, items = await reg.run(
        "lars", lambda col: (col.get_note(nid).note_type()["name"], dict(col.get_note(nid).items()))
    )
    assert model_name == "SrcVocab"
    assert items == {"Word": "kaufen", "Meaning": "to buy", "Notes": "strong verb"}
    assert _backup_reasons(reg, "lars") == []


# -- name/selection errors ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_new_note_type_lists_available_types(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "mara")

    with pytest.raises(CollectionToolError) as exc:
        await note_tools.preview_note_type_change(reg, "mara", "No Such Type", note_ids=[nid])
    assert "Unknown note type 'No Such Type'" in str(exc.value)
    assert "SrcVocab" in str(exc.value) and "TgtVocab" in str(exc.value)

    res = await note_tools.change_note_type(
        manager, reg, "mara", "No Such Type", note_ids=[nid], confirm=True
    )
    assert res.success is False and res.local_status == "failed"
    assert "Available models" in res.error
    assert _backup_reasons(reg, "mara") == []


@pytest.mark.asyncio
async def test_unknown_field_names_are_rejected_with_valid_names(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "nils")

    # Unknown NEW field name (key) ...
    res = await note_tools.change_note_type(
        manager, reg, "nils", "TgtVocab", note_ids=[nid],
        field_map={"Font": "Word"}, confirm=True,
    )
    assert res.success is False
    assert "Unknown field name(s) ['Font']" in res.error
    assert "'Extra', 'Front'" in res.error  # valid names of the NEW type
    # ... and unknown OLD field name (value).
    res2 = await note_tools.change_note_type(
        manager, reg, "nils", "TgtVocab", note_ids=[nid],
        field_map={"Front": "Wort"}, confirm=True,
    )
    assert res2.success is False
    assert "Unknown field name(s) ['Wort']" in res2.error
    assert "'Meaning', 'Notes', 'Word'" in res2.error  # valid names of the OLD type
    assert _backup_reasons(reg, "nils") == []


@pytest.mark.asyncio
async def test_mixed_note_type_selection_is_a_clean_error(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "olaf")

    def _add_basic(col):
        note = Note(col, col.models.by_name("Basic"))
        note["Front"] = "plain"
        note["Back"] = "simple"
        col.add_note(note, col.decks.id("Default"))
        return int(note.id)

    basic_nid = await reg.run("olaf", _add_basic)

    # The backend answers a mixed selection with InvalidInput() (EMPTY args) —
    # the exact signature the registry reads as a stale handle. The tools must
    # surface a clean named error instead, BEFORE any backend call.
    with pytest.raises(CollectionToolError) as exc:
        await note_tools.preview_note_type_change(
            reg, "olaf", "TgtVocab", note_ids=[nid, basic_nid]
        )
    message = str(exc.value)
    assert "multiple note types" in message
    assert "SrcVocab" in message and "Basic" in message

    res = await note_tools.change_note_type(
        manager, reg, "olaf", "TgtVocab", note_ids=[nid, basic_nid], confirm=True
    )
    assert res.success is False and res.local_status == "failed"
    assert "multiple note types" in res.error
    assert _backup_reasons(reg, "olaf") == []


# -- cloze ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloze_transition_rejects_template_map_and_succeeds_without(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "petra")
    await reg.run("petra", lambda col: _make_note_type(col, "ClozeVocab", ["Text", "Extra"], cloze=True))

    rejected = await note_tools.change_note_type(
        manager, reg, "petra", "ClozeVocab", note_ids=[nid],
        template_map={"Cloze": "Card 1"}, confirm=True,
    )
    assert rejected.success is False and rejected.local_status == "failed"
    assert "Cloze" in rejected.error and "template_map" in rejected.error
    assert _backup_reasons(reg, "petra") == []

    ok = await note_tools.change_note_type(
        manager, reg, "petra", "ClozeVocab", note_ids=[nid], confirm=True
    )
    assert ok.success is True, ok.error
    assert ok.data["is_cloze"] is True
    assert ok.data["template_map"] == {}
    items = dict(await reg.run("petra", lambda col: col.get_note(nid).items()))
    assert items == {"Text": "kaufen", "Extra": "to buy"}


# -- selection equivalence + input validation --------------------------------------


@pytest.mark.asyncio
async def test_query_selection_equals_note_ids_selection(env):
    reg, manager = env
    nid = await _setup_vocab_user(reg, "quentin")
    second_nid = await reg.run("quentin", lambda col: _add_note_of(
        col, "SrcVocab", {"Word": "sehen", "Meaning": "to see", "Notes": ""}
    ))

    by_ids = await note_tools.preview_note_type_change(
        reg, "quentin", "TgtVocab", note_ids=[nid, second_nid]
    )
    by_query = await note_tools.preview_note_type_change(reg, "quentin", "TgtVocab", query="note:SrcVocab")
    assert by_query == by_ids
    assert by_query["note_count"] == 2

    # The query path really mutates everything it selected.
    res = await note_tools.change_note_type(
        manager, reg, "quentin", "TgtVocab", query="note:SrcVocab", confirm=True
    )
    assert res.success is True, res.error
    assert res.data["changed"] == 2
    names = await reg.run("quentin", lambda col: [
        col.get_note(n).note_type()["name"] for n in sorted([nid, second_nid])
    ])
    assert names == ["TgtVocab", "TgtVocab"]


@pytest.mark.asyncio
async def test_selection_is_exactly_one_of_note_ids_or_query():
    for model in (PreviewNoteTypeChangeInput, ChangeNoteTypeInput):
        with pytest.raises(ValidationError):
            model(new_note_type="TgtVocab")  # neither
        with pytest.raises(ValidationError):
            model(new_note_type="TgtVocab", note_ids=[1], query="deck:Default")  # both
    # Exactly one is accepted for both shapes.
    assert PreviewNoteTypeChangeInput(new_note_type="TgtVocab", query="deck:Default").query
    assert ChangeNoteTypeInput(new_note_type="TgtVocab", note_ids=[5], confirm=True).note_ids == [5]


@pytest.mark.asyncio
async def test_change_note_type_input_bounds():
    with pytest.raises(ValidationError):
        ChangeNoteTypeInput(new_note_type="TgtVocab", note_ids=[0])
    with pytest.raises(ValidationError):
        ChangeNoteTypeInput(new_note_type="TgtVocab", note_ids=list(range(1, 2002)))
    assert ChangeNoteTypeInput(new_note_type="TgtVocab", note_ids=list(range(1, 2001)))


# -- idempotency + isolation -------------------------------------------------------


@pytest.mark.asyncio
async def test_change_note_type_idempotency_key_replays_cached_result(env):
    reg, manager = env
    nid1 = await _setup_vocab_user(reg, "sven")
    nid2 = await reg.run("sven", lambda col: _add_note_of(
        col, "SrcVocab", {"Word": "lesen", "Meaning": "to read", "Notes": ""}
    ))

    first = await note_tools.change_note_type(
        manager, reg, "sven", "TgtVocab", note_ids=[nid1], confirm=True, idempotency_key="ct-once"
    )
    assert first.success is True, first.error
    snapshots_after_first = [
        e for e in reg.list_backups("sven") if e["reason"] == "pre-change-note-type"
    ]

    # Same key, DIFFERENT payload: the ledger replays the cached result without
    # changing the second note or taking another snapshot.
    replay = await note_tools.change_note_type(
        manager, reg, "sven", "TgtVocab", note_ids=[nid2], confirm=True, idempotency_key="ct-once"
    )
    assert replay.success is True
    assert replay.local_status == "committed"
    assert replay.data == first.data
    assert "Idempotent operation replay" in (replay.warnings or [""])[0]
    assert await reg.run("sven", lambda col: col.get_note(nid2).note_type()["name"]) == "SrcVocab"
    assert [
        e for e in reg.list_backups("sven") if e["reason"] == "pre-change-note-type"
    ] == snapshots_after_first


@pytest.mark.asyncio
async def test_change_note_type_is_isolated_per_user(env):
    reg, manager = env
    alice_nid = await _setup_vocab_user(reg, "alice")
    bob_nid = await _setup_vocab_user(reg, "bob")

    res = await note_tools.change_note_type(
        manager, reg, "alice", "TgtVocab", note_ids=[alice_nid], confirm=True
    )
    assert res.success is True, res.error
    # Alice's note changed and her snapshot exists ...
    assert await reg.run("alice", lambda col: col.get_note(alice_nid).note_type()["name"]) == "TgtVocab"
    assert "pre-change-note-type" in _backup_reasons(reg, "alice")
    # ... but Bob's collection is untouched in both respects.
    assert await reg.run("bob", lambda col: col.get_note(bob_nid).note_type()["name"]) == "SrcVocab"
    assert dict(await reg.run("bob", lambda col: col.get_note(bob_nid).items()))["Word"] == "kaufen"
    assert _backup_reasons(reg, "bob") == []


# -- same-note-type change ---------------------------------------------------------


@pytest.mark.asyncio
async def test_same_note_type_change_reorders_fields_in_place(env):
    """Probed against the wheel (anki==26.9.3): a same-notetype change is
    ACCEPTED — it is the desktop dialog's field-reorder mechanism — so the
    tool exposes it as an in-place reorder rather than a refusal."""
    reg, manager = env
    nid = await _setup_vocab_user(reg, "tessa")

    res = await note_tools.change_note_type(
        manager, reg, "tessa", "SrcVocab", note_ids=[nid],
        field_map={"Word": "Meaning", "Meaning": "Word", "Notes": "Notes"}, confirm=True,
    )
    assert res.success is True, res.error
    assert res.data["old_note_type"] == "SrcVocab"
    assert res.data["new_note_type"] == "SrcVocab"
    assert dict(await reg.run("tessa", lambda col: col.get_note(nid).items())) == {
        "Word": "to buy", "Meaning": "kaufen", "Notes": "strong verb",
    }
