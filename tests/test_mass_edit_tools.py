"""Tests for find_and_replace and find_duplicates (roadmap phase 2 mass-edit pair).

Same contract as test_mcp_tools_v2.py: the REAL tool functions run against
REAL per-user anki collections in a tmp data dir — no mocks. Every assertion
below is about real field contents in a real collection (the replace runs
through the wheel's backend operation, the dupe report through the wheel's
Python-side find_dupes).
"""

import logging

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.models import FindAndReplaceInput, FindDuplicatesInput
from src.sync_manager import SyncManager
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


def _add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return int(note.id)


async def _fronts(reg, username, nids):
    def _get(col):
        return {nid: col.get_note(nid).fields[0] for nid in nids}

    return await reg.run(username, _get)


# -- find_and_replace --------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_replace_moves_content_via_note_ids(env):
    reg, manager = env
    nid1 = await reg.run("alva", _add_basic_note, "the colour red", "colour note back")
    nid2 = await reg.run("alva", _add_basic_note, "another colour", "untouched back")
    nid3 = await reg.run("alva", _add_basic_note, "no match here", "untouched back")

    res = await note_tools.find_and_replace(
        manager, reg, "alva", search="colour", replacement="color",
        note_ids=[nid1, nid2, nid3],
    )
    assert res.success is True, res.error
    assert res.local_status == "committed"
    # Changed-note count from the backend plus the selection size: the third
    # note was selected but matched nothing.
    assert res.data["changed"] == 2
    assert res.data["selected"] == 3

    values = await _fronts(reg, "alva", [nid1, nid2, nid3])
    assert values == {nid1: "the color red", nid2: "another color", nid3: "no match here"}

    # Plain content edit (same class as update_note_fields): no confirm gate
    # and NO snapshot.
    assert _backup_reasons(reg, "alva") == []


@pytest.mark.asyncio
async def test_literal_replace_via_query_selection(env):
    reg, manager = env
    nid1 = await reg.run("berit", _add_basic_note, "colour word one")
    nid2 = await reg.run("berit", _add_basic_note, "plain word")
    nid3 = await reg.run("berit", _add_basic_note, "colour word two")

    res = await note_tools.find_and_replace(
        manager, reg, "berit", search="colour", replacement="color",
        query="Front:colour*",
    )
    assert res.success is True, res.error
    assert res.data["changed"] == 2
    assert res.data["selected"] == 2

    values = await _fronts(reg, "berit", [nid1, nid2, nid3])
    assert values == {nid1: "color word one", nid2: "plain word", nid3: "color word two"}


@pytest.mark.asyncio
async def test_regex_replace_references_capture_groups(env):
    """Probed on the wheel: the replacement speaks Rust regex — $1, $2 ... —
    while a literal '\\1' would insert a backslash-one."""
    reg, manager = env
    nid = await reg.run("cecilia", _add_basic_note, "Chapter 12: Der Titel")

    res = await note_tools.find_and_replace(
        manager, reg, "cecilia", search=r"Chapter (\d+):", replacement=r"Kapitel $1 —",
        note_ids=[nid], regex=True,
    )
    assert res.success is True, res.error
    assert res.data["changed"] == 1
    assert (await _fronts(reg, "cecilia", [nid]))[nid] == "Kapitel 12 — Der Titel"


@pytest.mark.asyncio
async def test_match_case_misses_case_differing_text(env):
    reg, manager = env
    nid = await reg.run("dagmar", _add_basic_note, "Colour is British")

    # Case-sensitive search misses the capitalized occurrence entirely ...
    strict = await note_tools.find_and_replace(
        manager, reg, "dagmar", search="colour", replacement="color",
        note_ids=[nid], match_case=True,
    )
    assert strict.success is True, strict.error
    assert strict.data["changed"] == 0
    assert (await _fronts(reg, "dagmar", [nid]))[nid] == "Colour is British"

    # ... while the default (case-insensitive) replaces it.
    loose = await note_tools.find_and_replace(
        manager, reg, "dagmar", search="colour", replacement="color", note_ids=[nid],
    )
    assert loose.success is True, loose.error
    assert loose.data["changed"] == 1
    assert (await _fronts(reg, "dagmar", [nid]))[nid] == "color is British"


@pytest.mark.asyncio
async def test_field_name_scopes_the_replace_to_one_field(env):
    reg, manager = env
    nid = await reg.run("erik", _add_basic_note, "target in front", back="target in back")

    res = await note_tools.find_and_replace(
        manager, reg, "erik", search="target", replacement="done",
        note_ids=[nid], field_name="Back",
    )
    assert res.success is True, res.error
    assert res.data["changed"] == 1
    assert res.data["field_name"] == "Back"

    fields = dict(await reg.run("erik", lambda col: col.get_note(nid).items()))
    assert fields == {"Front": "target in front", "Back": "done in back"}


@pytest.mark.asyncio
async def test_zero_matches_is_a_successful_noop(env):
    reg, manager = env
    nid = await reg.run("fredrik", _add_basic_note, "stable content")

    res = await note_tools.find_and_replace(
        manager, reg, "fredrik", search="no-such-substring", replacement="x",
        note_ids=[nid],
    )
    assert res.success is True
    assert res.local_status == "committed"
    # visible zero-match no-op: selection size reported, changed count 0
    assert res.data["selected"] == 1
    assert res.data["changed"] == 0
    assert (await _fronts(reg, "fredrik", [nid]))[nid] == "stable content"
    assert _backup_reasons(reg, "fredrik") == []


@pytest.mark.asyncio
async def test_unknown_note_ids_are_a_clean_error_before_the_backend(env):
    reg, manager = env
    nid = await reg.run("gunilla", _add_basic_note, "real note")

    res = await note_tools.find_and_replace(
        manager, reg, "gunilla", search="real", replacement="x",
        note_ids=[nid, 12345],
    )
    assert res.success is False and res.local_status == "failed"
    assert "note(s) not found" in res.error
    assert "12345" in res.error
    # The real note was never touched.
    assert (await _fronts(reg, "gunilla", [nid]))[nid] == "real note"


@pytest.mark.asyncio
async def test_invalid_regex_backend_error_stays_single_line_in_logs(env, caplog):
    """With regex=True a pattern that the backend fails to PARSE comes back as
    InvalidInput whose message embeds the search VERBATIM (Rust regex error
    rendering) — and the keep-line-breaks validator admits \n into that
    pattern. One such call used to author multi-line log records at the two
    generic %s sinks (the stale-handle warning, which fires on every
    InvalidInput, and the mutation-failure error): log-forging, CWE-117. Both
    sinks must %r-escape exception text, so one tool call can never emit a
    record spanning lines (the invariant tools/notes.py states for its own
    %r-logged inputs); the client-facing error keeps the raw diagnostic."""
    reg, manager = env
    nid = await reg.run("ivar", _add_basic_note, "the colour red")

    forged = "FAKE user=x\n2026-10-11 INFO auth login ok admin\n("
    with caplog.at_level(logging.INFO):
        res = await note_tools.find_and_replace(
            manager, reg, "ivar", search=forged, replacement="x",
            note_ids=[nid], regex=True,
        )

    # The failure surfaces to the client with the diagnostic intact (a JSON
    # string field is a single structured value; only LOG lines had to be
    # defended).
    assert res.success is False and res.local_status == "failed"
    assert "2026-10-11 INFO auth login ok admin" in res.error
    assert "regex parse error" in res.error

    # Both generic exception-text sinks fired for this call, and NO record
    # from this one tool call spans a line.
    assert any("Stale collection handle" in r.getMessage() for r in caplog.records)
    assert any("Local mutation 'find_and_replace' failed" in r.getMessage() for r in caplog.records)
    multiline = [r.getMessage() for r in caplog.records if "\n" in r.getMessage() or "\r" in r.getMessage()]
    assert multiline == []

    # The note is untouched (the backend refused before changing anything).
    assert (await _fronts(reg, "ivar", [nid]))[nid] == "the colour red"


# -- find_duplicates ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicates_group_shared_first_field_values(env):
    reg, manager = env
    nid1 = await reg.run("helga", _add_basic_note, "shared front", back="variant one")
    nid2 = await reg.run("helga", _add_basic_note, "shared front", back="variant two")
    distinct = await reg.run("helga", _add_basic_note, "unique front", back="single")

    res = await note_tools.find_duplicates(reg, "helga", "Front")
    assert res["total_groups"] == 1
    (group,) = res["duplicates"]
    assert group["field_value"] == "shared front"
    assert sorted(group["note_ids"]) == sorted([nid1, nid2])
    assert distinct not in group["note_ids"]
    # Read-only: no snapshot, nothing changed.
    assert _backup_reasons(reg, "helga") == []


@pytest.mark.asyncio
async def test_search_prefilter_narrows_duplicate_groups(env):
    reg, manager = env
    keep1 = await reg.run("ivan", _add_basic_note, "alpha twin", deck="Keep")
    keep2 = await reg.run("ivan", _add_basic_note, "alpha twin", deck="Keep")
    drop1 = await reg.run("ivan", _add_basic_note, "beta twin", deck="Drop")
    drop2 = await reg.run("ivan", _add_basic_note, "beta twin", deck="Drop")

    unfiltered = await note_tools.find_duplicates(reg, "ivan", "Front")
    assert unfiltered["total_groups"] == 2

    filtered = await note_tools.find_duplicates(reg, "ivan", "Front", search="deck:Keep")
    assert filtered["total_groups"] == 1
    (group,) = filtered["duplicates"]
    assert group["field_value"] == "alpha twin"
    assert sorted(group["note_ids"]) == sorted([keep1, keep2])
    assert drop1 not in group["note_ids"] and drop2 not in group["note_ids"]


@pytest.mark.asyncio
async def test_find_duplicates_pagination_covers_the_requested_page(env):
    reg, manager = env
    first1 = await reg.run("johan", _add_basic_note, "dupe one")
    first2 = await reg.run("johan", _add_basic_note, "dupe one")
    second1 = await reg.run("johan", _add_basic_note, "dupe two")
    second2 = await reg.run("johan", _add_basic_note, "dupe two")

    page2 = await note_tools.find_duplicates(reg, "johan", "Front", limit=1, offset=1)
    assert page2["total_groups"] == 2
    (group,) = page2["duplicates"]
    assert group["field_value"] == "dupe two"
    assert sorted(group["note_ids"]) == sorted([second1, second2])
    assert first1 not in group["note_ids"] and first2 not in group["note_ids"]


# -- input validation ---------------------------------------------------------------


def test_find_and_replace_requires_exactly_one_of_note_ids_or_query():
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x")  # neither
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", note_ids=[1], query="deck:Default")  # both
    assert FindAndReplaceInput(search="x", note_ids=[5]).note_ids == [5]
    assert FindAndReplaceInput(search="x", query="deck:Default").query == "deck:Default"


def test_find_and_replace_text_bounds_and_control_chars():
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="", note_ids=[1])  # empty search
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x" * 1001, note_ids=[1])  # search over 1000
    assert FindAndReplaceInput(search="x" * 1000, note_ids=[1])
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", replacement="y" * 10001, note_ids=[1])
    assert FindAndReplaceInput(search="x", replacement="y" * 10000, note_ids=[1])
    # Newlines and tabs are legitimate note content; other control chars are not.
    assert FindAndReplaceInput(search="a\nb", replacement="c\td", note_ids=[1])
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="a\x00b", note_ids=[1])
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", replacement="a\x1bb", note_ids=[1])
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", replacement="a\x7fb", note_ids=[1])


def test_find_and_replace_note_id_bounds():
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", note_ids=[0])
    with pytest.raises(ValidationError):
        FindAndReplaceInput(search="x", note_ids=list(range(1, 2002)))
    assert FindAndReplaceInput(search="x", note_ids=list(range(1, 2001)))


def test_find_duplicates_field_and_pagination_bounds():
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="")
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="   ")
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="F" * 101)
    assert FindDuplicatesInput(field_name="F" * 100)
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="Front\x02")
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="Front", limit=0)
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="Front", limit=501)
    assert FindDuplicatesInput(field_name="Front", limit=500)
    with pytest.raises(ValidationError):
        FindDuplicatesInput(field_name="Front", offset=-1)
    # search defaults to "" (no prefilter).
    assert FindDuplicatesInput(field_name="Front").search == ""
