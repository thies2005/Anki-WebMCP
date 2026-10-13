"""Tests for the note-type control set (tools 31-41: detail, update, field and
template operations, delete, restore to stock).

Same contract as test_change_note_type.py: the REAL tool functions run against
REAL per-user anki collections in a tmp data dir — no mocks. Persisted state
is asserted by RE-READING through col.models.by_name in a fresh registry.run
(update_dict evicts the wheel's model cache, so those reads hit the backend),
not via the dict the mutation happened to hold.
"""

import os

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.models import ModelManager
from anki.notes import Note

from src.errors import CollectionToolError
from src.models import (
    AddNoteTypeFieldInput,
    AddNoteTypeTemplateInput,
    DeleteNoteTypeInput,
    RemoveNoteTypeFieldInput,
    RemoveNoteTypeTemplateInput,
    RenameNoteTypeFieldInput,
    RepositionNoteTypeFieldInput,
    RepositionNoteTypeTemplateInput,
    RestoreNoteTypeToStockInput,
    TemplateInput,
    TemplateUpdateInput,
    UpdateNoteTypeInput,
)
from src.sync_manager import SyncManager
from src.tools import notes as note_tools
from src.tools import notetypes as notetype_tools
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


def _make_note_type(col, name, fields, templates=None, css=None, cloze=False):
    """A note type with the given fields; one default template unless given."""
    mm = col.models
    nt = mm.new(name=name)
    if cloze:
        nt["type"] = 1
    for field_name in fields:
        mm.add_field(nt, mm.new_field(field_name))
    if templates is None:
        template = mm.new_template("Card 1")
        template["qfmt"] = "{{" + fields[0] + "}}"
        template["afmt"] = "{{FrontSide}}\n<hr id=answer>\n{{" + fields[-1] + "}}"
        templates = [(template["name"], template["qfmt"], template["afmt"])]
    for tmpl_name, front, back in templates:
        template = mm.new_template(tmpl_name)
        template["qfmt"] = front
        template["afmt"] = back
        mm.add_template(nt, template)
    if css:
        nt["css"] = css
    mm.add(nt)
    return nt


def _add_note_of(col, model_name, values, deck="Default"):
    note = Note(col, col.models.by_name(model_name))
    for field_name, value in values.items():
        note[field_name] = value
    col.add_note(note, col.decks.id(deck))
    return int(note.id)


async def _field_state(reg, username, name):
    """(field names in order, persisted ords) read fresh from the backend."""
    return await reg.run(username, lambda col: (
        [f["name"] for f in col.models.by_name(name)["flds"]],
        [f["ord"] for f in col.models.by_name(name)["flds"]],
    ))


# -- get_note_type -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_note_type_reports_full_detail(env):
    reg, _manager = env

    def _setup(col):
        _make_note_type(
            col, "Vocab",
            ["Word", "Meaning", "Hint"],
            templates=[("Card 1", "{{Word}}", "{{FrontSide}}<hr>{{Meaning}}"),
                       ("Reverse", "{{Meaning}}", "{{FrontSide}}<hr>{{Word}}")],
            css=".card { font-family: serif; }",
        )
        return _add_note_of(col, "Vocab", {"Word": "kaufen", "Meaning": "to buy", "Hint": ""})

    nid = await reg.run("alva", _setup)
    detail = await notetype_tools.get_note_type(reg, "alva", "Vocab")

    assert detail["name"] == "Vocab"
    assert isinstance(detail["id"], int)
    assert detail["type"] == "standard"
    assert detail["fields"] == [
        {"name": "Word", "ordinal": 0},
        {"name": "Meaning", "ordinal": 1},
        {"name": "Hint", "ordinal": 2},
    ]
    assert detail["templates"] == [
        {"name": "Card 1", "ordinal": 0, "front": "{{Word}}", "back": "{{FrontSide}}<hr>{{Meaning}}"},
        {"name": "Reverse", "ordinal": 1, "front": "{{Meaning}}", "back": "{{FrontSide}}<hr>{{Word}}"},
    ]
    assert detail["css"] == ".card { font-family: serif; }"
    assert detail["sort_field"] == "Word"
    assert detail["use_count"] == 1  # the one note created above


@pytest.mark.asyncio
async def test_get_note_type_reports_cloze_and_standard(env):
    reg, _manager = env
    detail = await notetype_tools.get_note_type(reg, "britt", "Cloze")
    assert detail["type"] == "cloze"
    assert detail["fields"][0]["name"] == "Text"

    detail = await notetype_tools.get_note_type(reg, "britt", "Basic")
    assert detail["type"] == "standard"


@pytest.mark.asyncio
async def test_get_note_type_unknown_name_lists_available_types(env):
    reg, _manager = env
    with pytest.raises(CollectionToolError) as exc:
        await notetype_tools.get_note_type(reg, "cedric", "No Such Type")
    assert "Unknown note type 'No Such Type'" in str(exc.value)
    assert "Basic" in str(exc.value) and "Cloze" in str(exc.value)


# -- update_note_type ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_note_type_applies_every_edit_and_persists_them(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "Grammar",
            ["Front", "Back"],
            templates=[("Card 1", "{{Front}}", "{{FrontSide}}<hr>{{Back}}")],
        )
        return _add_note_of(col, "Grammar", {"Front": "ist", "Back": "is"})

    await reg.run("dagny", _setup)
    res = await notetype_tools.update_note_type(
        manager, reg, "dagny", "Grammar",
        css=".card { color: navy; }",
        template_updates=[
            TemplateUpdateInput(template="Card 1", new_name="Both Ways", front="{{Back}}"),
        ],
        sort_field="Back",
    )
    assert res.success is True, res.error
    assert res.local_status == "committed"
    assert res.data["changed"] == ["css", "templates", "sort_field"]

    # Persisted state re-read through by_name (NOT the dict we mutated).
    def _read(col):
        nt = col.models.by_name("Grammar")
        return {
            "css": nt["css"],
            "sortf": nt["sortf"],
            "templates": [(t["name"], t["qfmt"], t["afmt"]) for t in nt["tmpls"]],
        }

    state = await reg.run("dagny", _read)
    assert state["css"] == ".card { color: navy; }"
    assert state["sortf"] == 1
    assert state["templates"] == [("Both Ways", "{{Back}}", "{{FrontSide}}<hr>{{Back}}")]


@pytest.mark.asyncio
async def test_update_note_type_renames_the_type(env):
    reg, manager = env
    await reg.run("einar", lambda col: _make_note_type(col, "Old", ["F1", "F2"]))

    res = await notetype_tools.update_note_type(manager, reg, "einar", "Old", new_name="New")
    assert res.success is True, res.error
    listing = await note_tools.list_note_types(reg, "einar")
    names = {m["name"] for m in listing["note_types"]}
    assert "New" in names and "Old" not in names


@pytest.mark.asyncio
async def test_update_note_type_name_collision_is_refused_and_persists_nothing(env):
    reg, manager = env
    await reg.run("fredrik", lambda col: _make_note_type(col, "Mine", ["F1", "F2"]))

    res = await notetype_tools.update_note_type(
        manager, reg, "fredrik", "Mine", new_name="Basic"
    )
    assert res.success is False and res.local_status == "failed"
    assert "already exists" in res.error
    # ensure_name_unique would have silently renamed to "Basic-xxxxx"; the
    # pre-check means the type keeps its name AND its id.
    detail = await notetype_tools.get_note_type(reg, "fredrik", "Mine")
    assert detail["name"] == "Mine"


@pytest.mark.asyncio
async def test_update_note_type_rejects_unknown_template_and_sort_field(env):
    reg, manager = env
    await reg.run("greta", lambda col: _make_note_type(col, "T", ["F1", "F2"]))

    res = await notetype_tools.update_note_type(
        manager, reg, "greta", "T",
        template_updates=[TemplateUpdateInput(template="Missing", front="{{F1}}")],
    )
    assert res.success is False
    assert "Unknown template name(s) ['Missing']" in res.error
    assert "'Card 1'" in res.error

    res = await notetype_tools.update_note_type(manager, reg, "greta", "T", sort_field="Nope")
    assert res.success is False
    assert "Unknown sort field 'Nope'" in res.error
    assert "'F1', 'F2'" in res.error


@pytest.mark.asyncio
async def test_update_note_type_template_rename_collision_is_refused(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "TwoT", ["F1", "F2"],
            templates=[("Card 1", "{{F1}}", "{{FrontSide}}<hr>{{F2}}"),
                       ("Card 2", "{{F2}}", "{{FrontSide}}<hr>{{F1}}")],
        )

    await reg.run("henrik", _setup)
    res = await notetype_tools.update_note_type(
        manager, reg, "henrik", "TwoT",
        template_updates=[TemplateUpdateInput(template="Card 1", new_name="Card 2")],
    )
    assert res.success is False
    assert "Two templates" in res.error and "'Card 2'" in res.error


@pytest.mark.asyncio
async def test_update_note_type_refuses_duplicate_template_entries_and_persists_nothing(env):
    """debt-3: [A→X, A→B] used to slip past the rename-collision check — the
    check's next() saw only the FIRST entry per template while the apply loop
    ran BOTH (later edits silently overwriting earlier ones). A repeated
    template name is now refused before any write."""
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "DupT", ["F1", "F2"],
            templates=[("Card 1", "{{F1}}", "{{FrontSide}}<hr>{{F2}}"),
                       ("Card 2", "{{F2}}", "{{FrontSide}}<hr>{{F1}}")],
        )

    await reg.run("hilde", _setup)
    res = await notetype_tools.update_note_type(
        manager, reg, "hilde", "DupT",
        template_updates=[
            TemplateUpdateInput(template="Card 1", new_name="X"),
            TemplateUpdateInput(template="Card 1", new_name="B", front="{{F2}}"),
        ],
    )
    assert res.success is False and res.local_status == "failed"
    assert "same template twice" in res.error
    assert "'Card 1'" in res.error

    # Nothing persisted: both templates keep their names and fronts.
    state = await reg.run("hilde", lambda col: [
        (t["name"], t["qfmt"]) for t in col.models.by_name("DupT")["tmpls"]
    ])
    assert state == [("Card 1", "{{F1}}"), ("Card 2", "{{F2}}")]


@pytest.mark.asyncio
async def test_update_note_type_with_no_changes_is_refused(env):
    reg, manager = env
    await reg.run("inger", lambda col: _make_note_type(col, "Idle", ["F1", "F2"]))
    # Raised before execute_mutation (the add_notes "at least one note" pattern):
    # the MCP wrapper turns it into the structured error response.
    with pytest.raises(CollectionToolError) as exc:
        await notetype_tools.update_note_type(manager, reg, "inger", "Idle")
    assert "Nothing to update" in str(exc.value)


# -- field operations ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_field_appends_and_inserts_with_persisted_ordinals(env):
    reg, manager = env
    await reg.run("jarl", lambda col: _make_note_type(col, "Grow", ["A", "B"]))

    res = await notetype_tools.add_note_type_field(manager, reg, "jarl", "Grow", "C")
    assert res.success is True, res.error
    assert res.data["position"] == 2 and res.data["field_count"] == 3
    assert await _field_state(reg, "jarl", "Grow") == (["A", "B", "C"], [0, 1, 2])

    res = await notetype_tools.add_note_type_field(
        manager, reg, "jarl", "Grow", "Lead", position=0
    )
    assert res.success is True, res.error
    assert res.data["position"] == 0
    assert await _field_state(reg, "jarl", "Grow") == (["Lead", "A", "B", "C"], [0, 1, 2, 3])

    # Notes of the type see the new empty field.
    detail = await notetype_tools.get_note_type(reg, "jarl", "Grow")
    assert [f["name"] for f in detail["fields"]] == ["Lead", "A", "B", "C"]


@pytest.mark.asyncio
async def test_add_field_rejects_duplicate_name_and_bad_position(env):
    reg, manager = env
    await reg.run("kaja", lambda col: _make_note_type(col, "Dup", ["A", "B"]))

    res = await notetype_tools.add_note_type_field(manager, reg, "kaja", "Dup", "A")
    assert res.success is False
    assert "already exists" in res.error
    assert "'A', 'B'" in res.error

    res = await notetype_tools.add_note_type_field(manager, reg, "kaja", "Dup", "Z", position=5)
    assert res.success is False
    assert "between 0 and 2" in res.error

    # position == field count means "appended last" and is accepted.
    res = await notetype_tools.add_note_type_field(manager, reg, "kaja", "Dup", "Z", position=2)
    assert res.success is True, res.error
    assert (await _field_state(reg, "kaja", "Dup"))[0] == ["A", "B", "Z"]


@pytest.mark.asyncio
async def test_remove_field_deletes_content_on_notes_and_snapshots(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "Trim", ["Keep", "Drop"])
        return _add_note_of(col, "Trim", {"Keep": "stay", "Drop": "gone"})

    nid = await reg.run("linnea", _setup)
    res = await notetype_tools.remove_note_type_field(
        manager, reg, "linnea", "Trim", "Drop", confirm=True
    )
    assert res.success is True, res.error
    assert res.data["removed_field"] == "Drop"
    assert res.data["remaining_fields"] == ["Keep"]
    assert res.data["snapshot_reason"] == "pre-remove-note-type-field"
    assert "pre-remove-note-type-field" in _backup_reasons(reg, "linnea")

    # Persisted schema AND real note content: the dropped value is really gone.
    items = dict(await reg.run("linnea", lambda col: col.get_note(nid).items()))
    assert items == {"Keep": "stay"}


@pytest.mark.asyncio
async def test_remove_field_refuses_unknown_name_and_last_remaining_field(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "Single", ["Only"])
        return _add_note_of(col, "Single", {"Only": "x"})

    await reg.run("malte", _setup)
    res = await notetype_tools.remove_note_type_field(
        manager, reg, "malte", "Single", "Nope", confirm=True
    )
    assert res.success is False
    assert "Unknown field 'Nope'" in res.error

    res = await notetype_tools.remove_note_type_field(
        manager, reg, "malte", "Single", "Only", confirm=True
    )
    assert res.success is False and res.local_status == "failed"
    assert "last remaining field" in res.error
    # Refusals never snapshot.
    assert _backup_reasons(reg, "malte") == []
    assert await _field_state(reg, "malte", "Single") == (["Only"], [0])


@pytest.mark.asyncio
async def test_rename_field_persists_and_rejects_collisions(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "Ren", ["Old1", "Old2"])
        return _add_note_of(col, "Ren", {"Old1": "v1", "Old2": "v2"})

    nid = await reg.run("nellie", _setup)
    res = await notetype_tools.rename_note_type_field(
        manager, reg, "nellie", "Ren", "Old1", "New1"
    )
    assert res.success is True, res.error
    items = dict(await reg.run("nellie", lambda col: col.get_note(nid).items()))
    assert items == {"New1": "v1", "Old2": "v2"}

    res = await notetype_tools.rename_note_type_field(
        manager, reg, "nellie", "Ren", "Old2", "New1"
    )
    assert res.success is False
    assert "already exists" in res.error

    res = await notetype_tools.rename_note_type_field(
        manager, reg, "nellie", "Ren", "Ghost", "Whatever"
    )
    assert res.success is False
    assert "Unknown field 'Ghost'" in res.error


@pytest.mark.asyncio
async def test_reposition_field_moves_and_bounds_checks(env):
    reg, manager = env
    await reg.run("odin", lambda col: _make_note_type(col, "Move", ["A", "B", "C"]))

    res = await notetype_tools.reposition_note_type_field(manager, reg, "odin", "Move", "C", 0)
    assert res.success is True, res.error
    assert res.data["fields"] == ["C", "A", "B"]
    assert await _field_state(reg, "odin", "Move") == (["C", "A", "B"], [0, 1, 2])

    for bad in (-1, 3):
        res = await notetype_tools.reposition_note_type_field(
            manager, reg, "odin", "Move", "A", bad
        )
        assert res.success is False
        assert "between 0 and 2" in res.error

    res = await notetype_tools.reposition_note_type_field(manager, reg, "odin", "Move", "X", 0)
    assert res.success is False
    assert "Unknown field 'X'" in res.error


# -- template operations -------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_template_appends_and_generates_cards_for_new_notes(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "GrowT", ["Front", "Back"])
        return _add_note_of(col, "GrowT", {"Front": "f", "Back": "b"})

    nid = await reg.run("petronella", _setup)
    before = await reg.run("petronella", lambda col: len(col.get_note(nid).cards()))
    assert before == 1

    res = await notetype_tools.add_note_type_template(
        manager, reg, "petronella", "GrowT",
        TemplateInput(name="Reverse", front="{{Back}}", back="{{FrontSide}}<hr>{{Front}}"),
    )
    assert res.success is True, res.error
    assert res.data["template"] == "Reverse"
    assert res.data["position"] == 1 and res.data["template_count"] == 2

    names = await reg.run("petronella", lambda col: [t["name"] for t in col.models.by_name("GrowT")["tmpls"]])
    assert names == ["Card 1", "Reverse"]

    # A NEW note of the type now gets both cards.
    nid2 = await reg.run("petronella", lambda col: _add_note_of(
        col, "GrowT", {"Front": "f2", "Back": "b2"}
    ))
    cards = await reg.run("petronella", lambda col: col.get_note(nid2).cards())
    assert len(cards) == 2


@pytest.mark.asyncio
async def test_add_template_inserts_at_position_and_rejects_duplicates(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "InsT", ["F1", "F2"],
            templates=[("Card 1", "{{F1}}", "{{FrontSide}}<hr>{{F2}}"),
                       ("Card 2", "{{F2}}", "{{FrontSide}}<hr>{{F1}}")],
        )

    await reg.run("quintus", _setup)
    res = await notetype_tools.add_note_type_template(
        manager, reg, "quintus", "InsT",
        TemplateInput(name="First", front="Q: {{F1}}", back="{{F1}}"), position=0,
    )
    assert res.success is True, res.error
    names = await reg.run("quintus", lambda col: [t["name"] for t in col.models.by_name("InsT")["tmpls"]])
    assert names == ["First", "Card 1", "Card 2"]

    res = await notetype_tools.add_note_type_template(
        manager, reg, "quintus", "InsT",
        TemplateInput(name="Card 1", front="{{F1}}", back="{{F2}}"),
    )
    assert res.success is False
    assert "already exists" in res.error

    res = await notetype_tools.add_note_type_template(
        manager, reg, "quintus", "InsT",
        TemplateInput(name="Later", front="{{F1}}", back="{{F2}}"), position=7,
    )
    assert res.success is False
    assert "between 0 and 3" in res.error

    # A front identical to another template's is refused by the WHEEL's
    # template validation on update_dict (clean CardTypeError, not a stale
    # handle) — the tool surfaces it as a failed response.
    res = await notetype_tools.add_note_type_template(
        manager, reg, "quintus", "InsT",
        TemplateInput(name="Clone", front="{{F2}}", back="{{F2}}"),
    )
    assert res.success is False and res.local_status == "failed"
    assert "identical to card template" in res.error


@pytest.mark.asyncio
async def test_remove_template_deletes_its_cards_and_snapshots(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "CutT", ["Front", "Back"],
            templates=[("Card 1", "{{Front}}", "{{FrontSide}}<hr>{{Back}}"),
                       ("Card 2", "{{Back}}", "{{FrontSide}}<hr>{{Front}}")],
        )
        return _add_note_of(col, "CutT", {"Front": "frage", "Back": "antwort"})

    nid = await reg.run("ragnar", _setup)
    card_ids = await reg.run("ragnar", lambda col: [int(c.id) for c in col.get_note(nid).cards()])
    assert len(card_ids) == 2

    res = await notetype_tools.remove_note_type_template(
        manager, reg, "ragnar", "CutT", "Card 2", confirm=True
    )
    assert res.success is True, res.error
    assert res.data["removed_template"] == "Card 2"
    assert res.data["cards_removed"] == 1
    assert res.data["remaining_templates"] == ["Card 1"]
    assert res.data["snapshot_reason"] == "pre-remove-note-type-template"
    assert "pre-remove-note-type-template" in _backup_reasons(reg, "ragnar")

    # The template's card is really deleted; the other one survives.
    remaining = await reg.run("ragnar", lambda col: [int(c.id) for c in col.get_note(nid).cards()])
    assert remaining == [card_ids[0]]


@pytest.mark.asyncio
async def test_remove_template_refuses_unknown_name_and_last_template(env):
    reg, manager = env
    await reg.run("sigrid", lambda col: _make_note_type(col, "OneT", ["F1", "F2"]))

    res = await notetype_tools.remove_note_type_template(
        manager, reg, "sigrid", "OneT", "Nope", confirm=True
    )
    assert res.success is False
    assert "Unknown template 'Nope'" in res.error
    assert "'Card 1'" in res.error

    res = await notetype_tools.remove_note_type_template(
        manager, reg, "sigrid", "OneT", "Card 1", confirm=True
    )
    assert res.success is False
    assert "last remaining template" in res.error
    assert _backup_reasons(reg, "sigrid") == []


@pytest.mark.asyncio
async def test_reposition_template_moves_and_bounds_checks(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "MoveT", ["F1", "F2"],
            templates=[("Card 1", "{{F1}}", "{{FrontSide}}<hr>{{F2}}"),
                       ("Card 2", "{{F2}}", "{{FrontSide}}<hr>{{F1}}")],
        )

    await reg.run("tore", _setup)
    res = await notetype_tools.reposition_note_type_template(
        manager, reg, "tore", "MoveT", "Card 2", 0
    )
    assert res.success is True, res.error
    assert res.data["templates"] == ["Card 2", "Card 1"]

    for bad in (-1, 2):
        res = await notetype_tools.reposition_note_type_template(
            manager, reg, "tore", "MoveT", "Card 1", bad
        )
        assert res.success is False
        assert "between 0 and 1" in res.error

    res = await notetype_tools.reposition_note_type_template(
        manager, reg, "tore", "MoveT", "Ghost", 0
    )
    assert res.success is False
    assert "Unknown template 'Ghost'" in res.error


# -- delete_note_type ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_note_type_removes_an_unused_type_and_snapshots(env):
    reg, manager = env
    await reg.run("ulrika", lambda col: _make_note_type(col, "Doomed", ["F1", "F2"]))

    res = await notetype_tools.delete_note_type(manager, reg, "ulrika", "Doomed", confirm=True)
    assert res.success is True, res.error
    assert res.data["deleted"] is True
    assert res.data["snapshot_reason"] == "pre-delete-note-type"
    assert "pre-delete-note-type" in _backup_reasons(reg, "ulrika")

    # Persisted removal: by-name lookup now fails with the remaining types.
    with pytest.raises(CollectionToolError) as exc:
        await notetype_tools.get_note_type(reg, "ulrika", "Doomed")
    assert "Unknown note type 'Doomed'" in str(exc.value)
    assert "Doomed" not in str(exc.value).split("Available models:")[1]


@pytest.mark.asyncio
async def test_delete_note_type_refuses_used_type_with_use_count(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "Busy", ["F1", "F2"])
        _add_note_of(col, "Busy", {"F1": "a", "F2": "b"})
        return _add_note_of(col, "Busy", {"F1": "c", "F2": "d"})

    await reg.run("valdemar", _setup)
    res = await notetype_tools.delete_note_type(manager, reg, "valdemar", "Busy", confirm=True)
    assert res.success is False and res.local_status == "failed"
    assert "2 note(s) still use it" in res.error
    assert "Change or delete those notes first" in res.error
    # The gate precedes the snapshot: nothing backed up, type intact.
    assert _backup_reasons(reg, "valdemar") == []
    detail = await notetype_tools.get_note_type(reg, "valdemar", "Busy")
    assert detail["use_count"] == 2

    # After emptying the type out, the deletion goes through.
    nids = await reg.run("valdemar", lambda col: col.models.nids(col.models.by_name("Busy")["id"]))
    await reg.run("valdemar", lambda col: col.remove_notes(nids))
    res = await notetype_tools.delete_note_type(manager, reg, "valdemar", "Busy", confirm=True)
    assert res.success is True, res.error


@pytest.mark.asyncio
async def test_delete_note_type_unknown_name_lists_available_types(env):
    reg, manager = env
    res = await notetype_tools.delete_note_type(manager, reg, "wanda", "Ghost", confirm=True)
    assert res.success is False
    assert "Unknown note type 'Ghost'" in res.error
    assert "Available models" in res.error


# -- restore_note_type_to_stock ------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_to_stock_resets_fields_css_and_note_content(env):
    reg, manager = env

    def _setup(col):
        nt = col.models.by_name("Basic")
        col.models.add_field(nt, col.models.new_field("Extra"))
        col.models.update_dict(nt)
        return _add_note_of(col, "Basic", {"Front": "q", "Back": "a", "Extra": "precious"})

    nid = await reg.run("victor", _setup)
    res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "victor", "Basic", confirm=True
    )
    assert res.success is True, res.error
    assert res.data["restored"] is True
    assert res.data["force_kind"] is None
    assert res.data["fields"] == ["Front", "Back"]  # the extra field is gone
    assert res.data["snapshot_reason"] == "pre-restore-note-type-stock"
    assert "pre-restore-note-type-stock" in _backup_reasons(reg, "victor")

    # Persisted schema AND note content: values survive only where a stock
    # field lines up — the "Extra" content is lost (as disclosed).
    def _read(col):
        nt = col.models.by_name("Basic")
        return ([f["name"] for f in nt["flds"]], dict(col.get_note(nid).items()))

    fields, items = await reg.run("victor", _read)
    assert fields == ["Front", "Back"]
    assert items == {"Front": "q", "Back": "a"}


@pytest.mark.asyncio
async def test_restore_with_force_kind_applies_that_stock_kind(env):
    reg, manager = env

    def _setup(col):
        nt = col.models.by_name("Basic")
        nt["css"] = ".card { color: pink; }"
        col.models.update_dict(nt)
        return _add_note_of(col, "Basic", {"Front": "q", "Back": "a"})

    nid = await reg.run("wilhelmina", _setup)
    res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "wilhelmina", "Basic", force_kind="cloze", confirm=True
    )
    assert res.success is True, res.error
    assert res.data["force_kind"] == "cloze"
    assert res.data["type"] == "cloze"
    assert res.data["fields"] == ["Text", "Back Extra"]
    assert res.data["templates"] == ["Cloze"]

    def _read(col):
        nt = col.models.by_name("Basic")
        return (nt["type"], nt["css"], dict(col.get_note(nid).items()))

    ntype, css, items = await reg.run("wilhelmina", _read)
    assert ntype == 1  # a forced cloze stock kind makes the type a cloze type
    assert "pink" not in css  # the modified css was replaced by the stock css
    assert items == {"Text": "q", "Back Extra": "a"}


@pytest.mark.asyncio
async def test_restore_rejects_bad_force_kind_and_unknown_stock_origin(env):
    reg, manager = env

    # Mapped BEFORE the confirm gate, so the typo surfaces even when the call
    # would otherwise be skipped (raised, not a skipped response).
    with pytest.raises(CollectionToolError) as exc:
        await notetype_tools.restore_note_type_to_stock(
            manager, reg, "yvette", "Basic", force_kind="kind_of_maybe", confirm=True
        )
    assert "Unknown force_kind 'kind_of_maybe'" in str(exc.value)
    assert "'basic'" in str(exc.value) and "'cloze'" in str(exc.value)
    assert _backup_reasons(reg, "yvette") == []

    # A type that does not record its stock origin cannot be restored without
    # an explicit force_kind: the backend's InvalidInput for that would be
    # mistaken for a stale handle, so the tool refuses it in Python first.

    def _strip_origin(col):
        nt = col.models.by_name("Basic")
        nt["originalStockKind"] = 0
        col.models.update_dict(nt)  # the wheel stores 0 by omitting the key

    await reg.run("yvette", _strip_origin)
    res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "yvette", "Basic", confirm=True
    )
    assert res.success is False
    assert "originalStockKind is unknown" in res.error
    assert "force_kind" in res.error
    assert _backup_reasons(reg, "yvette") == []

    # With an explicit kind it goes through.
    res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "yvette", "Basic", force_kind="basic", confirm=True
    )
    assert res.success is True, res.error
    assert "pre-restore-note-type-stock" in _backup_reasons(reg, "yvette")


# -- confirm gates -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_false_skips_all_four_destructive_tools_without_snapshots(env):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "Guarded", ["Keep", "Drop"])
        _add_note_of(col, "Guarded", {"Keep": "k", "Drop": "d"})
        _make_note_type(col, "Empty", ["F1", "F2"])

    await reg.run("axel", _setup)

    field_res = await notetype_tools.remove_note_type_field(
        manager, reg, "axel", "Guarded", "Drop", confirm=False
    )
    tmpl_res = await notetype_tools.remove_note_type_template(
        manager, reg, "axel", "Empty", "Card 1", confirm=False
    )
    delete_res = await notetype_tools.delete_note_type(manager, reg, "axel", "Empty", confirm=False)
    restore_res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "axel", "Guarded", confirm=False
    )

    for res, tool in ((field_res, "field"), (tmpl_res, "template"), (delete_res, "delete"), (restore_res, "restore")):
        assert res.success is False, tool
        assert res.local_status == "skipped", tool
        assert "confirm=true" in res.error, tool

    # Nothing changed anywhere, and not one snapshot exists.
    assert _backup_reasons(reg, "axel") == []
    assert (await _field_state(reg, "axel", "Guarded"))[0] == ["Keep", "Drop"]
    names = await reg.run("axel", lambda col: sorted(
        m.name for m in col.models.all_names_and_ids()
    ))
    assert "Empty" in names


# -- fail-closed mid-operation aborts (debt-4) ---------------------------------------

# Simulates the wheel's backend dying BETWEEN the pre-op snapshot and the
# write: the mutation call each destructive tool makes AFTER
# registry.snapshot_locked is patched to raise once.
def _crash_mid_operation(*_args, **_kwargs):
    raise RuntimeError("simulated wheel crash mid-mutation")


async def _assert_fail_closed(res, reg, username, reason):
    """The aborted mutation reports failure and left the reason-tagged
    pre-op snapshot ON DISK — the recovery path."""
    assert res.success is False and res.local_status == "failed"
    assert "simulated wheel crash mid-mutation" in res.error
    entries = [e for e in reg.list_backups(username) if e["reason"] == reason]
    assert len(entries) == 1, entries
    snapshot_path = reg.backup_path(username, entries[0]["filename"])
    assert os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0


@pytest.mark.asyncio
async def test_remove_field_crash_after_snapshot_fails_closed(env, monkeypatch):
    reg, manager = env

    def _setup(col):
        _make_note_type(col, "FragileF", ["Keep", "Drop"])
        return _add_note_of(col, "FragileF", {"Keep": "k", "Drop": "d"})

    nid = await reg.run("petra", _setup)
    monkeypatch.setattr(ModelManager, "remove_field", _crash_mid_operation)

    res = await notetype_tools.remove_note_type_field(
        manager, reg, "petra", "FragileF", "Drop", confirm=True
    )
    await _assert_fail_closed(res, reg, "petra", "pre-remove-note-type-field")
    # Unchanged: the crash hit before any write (update_dict never ran).
    assert await _field_state(reg, "petra", "FragileF") == (["Keep", "Drop"], [0, 1])
    items = dict(await reg.run("petra", lambda col: col.get_note(nid).items()))
    assert items == {"Keep": "k", "Drop": "d"}


@pytest.mark.asyncio
async def test_remove_template_crash_after_snapshot_fails_closed(env, monkeypatch):
    reg, manager = env

    def _setup(col):
        _make_note_type(
            col, "FragileT", ["Front", "Back"],
            templates=[("Card 1", "{{Front}}", "{{FrontSide}}<hr>{{Back}}"),
                       ("Card 2", "{{Back}}", "{{FrontSide}}<hr>{{Front}}")],
        )
        return _add_note_of(col, "FragileT", {"Front": "f", "Back": "b"})

    nid = await reg.run("quilla", _setup)
    monkeypatch.setattr(ModelManager, "remove_template", _crash_mid_operation)

    res = await notetype_tools.remove_note_type_template(
        manager, reg, "quilla", "FragileT", "Card 2", confirm=True
    )
    await _assert_fail_closed(res, reg, "quilla", "pre-remove-note-type-template")
    # Both templates survive, and the template's card was NOT deleted.
    names = await reg.run("quilla", lambda col: [
        t["name"] for t in col.models.by_name("FragileT")["tmpls"]
    ])
    assert names == ["Card 1", "Card 2"]
    cards = await reg.run("quilla", lambda col: [int(c.id) for c in col.get_note(nid).cards()])
    assert len(cards) == 2


@pytest.mark.asyncio
async def test_delete_note_type_crash_after_snapshot_fails_closed(env, monkeypatch):
    reg, manager = env
    await reg.run("rolf", lambda col: _make_note_type(col, "Doomed2", ["F1", "F2"]))
    monkeypatch.setattr(ModelManager, "remove", _crash_mid_operation)

    res = await notetype_tools.delete_note_type(manager, reg, "rolf", "Doomed2", confirm=True)
    await _assert_fail_closed(res, reg, "rolf", "pre-delete-note-type")
    # The type is still there.
    names = await reg.run("rolf", lambda col: sorted(
        m.name for m in col.models.all_names_and_ids()
    ))
    assert "Doomed2" in names


@pytest.mark.asyncio
async def test_restore_to_stock_crash_after_snapshot_fails_closed(env, monkeypatch):
    reg, manager = env

    def _setup(col):
        nt = col.models.by_name("Basic")
        col.models.add_field(nt, col.models.new_field("Extra"))
        col.models.update_dict(nt)
        return _add_note_of(col, "Basic", {"Front": "q", "Back": "a", "Extra": "precious"})

    nid = await reg.run("sigge", _setup)
    monkeypatch.setattr(ModelManager, "restore_notetype_to_stock", _crash_mid_operation)

    res = await notetype_tools.restore_note_type_to_stock(
        manager, reg, "sigge", "Basic", confirm=True
    )
    await _assert_fail_closed(res, reg, "sigge", "pre-restore-note-type-stock")
    # Schema and note content are untouched (restorable from the snapshot
    # even if they were not — but here nothing was written at all).
    fields, items = await reg.run("sigge", lambda col: (
        [f["name"] for f in col.models.by_name("Basic")["flds"]],
        dict(col.get_note(nid).items()),
    ))
    assert fields == ["Front", "Back", "Extra"]
    assert items == {"Front": "q", "Back": "a", "Extra": "precious"}


# -- input model validation ----------------------------------------------------------


def test_update_note_type_input_requires_at_least_one_change():
    with pytest.raises(ValidationError):
        UpdateNoteTypeInput(name="T")
    # Any single aspect is enough.
    assert UpdateNoteTypeInput(name="T", css=".card {}").css == ".card {}"
    assert UpdateNoteTypeInput(name="T", new_name="U").new_name == "U"
    assert UpdateNoteTypeInput(name="T", sort_field="F").sort_field == "F"
    with pytest.raises(ValidationError):
        UpdateNoteTypeInput(name="T", template_updates=[])


def test_note_type_control_input_shapes():
    with pytest.raises(ValidationError):
        AddNoteTypeFieldInput(note_type="T", field_name="")  # empty field name
    with pytest.raises(ValidationError):
        AddNoteTypeTemplateInput(note_type="T", template={"name": "X"})  # missing front/back
    with pytest.raises(ValidationError):
        RestoreNoteTypeToStockInput(name="T", force_kind="  ")
    with pytest.raises(ValidationError):
        DeleteNoteTypeInput(name="T\0")  # control characters
    parsed = RestoreNoteTypeToStockInput(name="T", force_kind="  CLOZE  ".strip())
    assert parsed.force_kind == "cloze"


# -- isolation -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_type_tools_are_isolated_per_user(env):
    reg, manager = env

    def _setup(col, css):
        _make_note_type(col, "Shared", ["F1", "F2"], css=css)
        return _add_note_of(col, "Shared", {"F1": col.models.by_name("Shared")["name"], "F2": "x"})

    await reg.run("alva2", lambda col: _setup(col, ".alice {}"))
    await reg.run("bob2", lambda col: _setup(col, ".bob {}"))

    res = await notetype_tools.update_note_type(
        manager, reg, "alva2", "Shared", css=".alice-updated {}"
    )
    assert res.success is True, res.error

    # Alice's update took effect on HER copy ...
    alice_css = await reg.run("alva2", lambda col: col.models.by_name("Shared")["css"])
    assert alice_css == ".alice-updated {}"
    # ... but Bob's same-named type keeps his CSS, and get_note_type stays
    # scoped to the caller's collection.
    bob_css = await reg.run("bob2", lambda col: col.models.by_name("Shared")["css"])
    assert bob_css == ".bob {}"
    bob_detail = await notetype_tools.get_note_type(reg, "bob2", "Shared")
    assert bob_detail["css"] == ".bob {}"

    # A type only alice has is unknown to bob, with bob's types listed.
    await reg.run("alva2", lambda col: _make_note_type(col, "AliceOnly", ["F1", "F2"]))
    with pytest.raises(CollectionToolError) as exc:
        await notetype_tools.get_note_type(reg, "bob2", "AliceOnly")
    assert "Unknown note type 'AliceOnly'" in str(exc.value)
    assert "AliceOnly" not in str(exc.value).split("Available models:")[1]
