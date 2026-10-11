"""Tests for the phase-4 scheduling and organization tools.

Same contract as test_mcp_tools_v2.py: the REAL tool functions run against
REAL per-user anki collections in a tmp data dir — no mocks. Every assertion
below is about real queue states, real flags, and real preset values in a
real collection (bury/unbury and reposition run through the wheel's backend
scheduler operations; the unbury-suspension disclosure is pinned to observed
backend behavior — the wheel's unbury IS restore_buried_and_suspended_cards).
"""

import pytest
import pytest_asyncio
from pydantic import ValidationError

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.models import (
    BuryCardsInput,
    RepositionNewCardsInput,
    SetCardFlagsInput,
    UnburyCardsInput,
    UpdateDeckConfigInput,
)
from src.sync_manager import SyncManager
from src.tools import cards as card_tools
from src.tools import decks as deck_tools
from src.users import UserStore


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data dir."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    # Real (empty) user store: the mutation path reads it for the local-only nudge
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


def _add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return int(note.cards()[0].id)


async def _card_state(reg, username, cid):
    """(queue, type, due, flags) of one card, read fresh from the collection."""
    def _get(col):
        c = col.get_card(cid)
        return (c.queue, c.type, c.due, c.flags)

    return await reg.run(username, _get)


# -- get_deck_config ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deck_config_returns_dict_and_unknown_deck_errors(env):
    reg, _manager = env
    await reg.run("greta", lambda col: _add_basic_note(col, "front"))

    res = await deck_tools.get_deck_config(reg, "greta", 1)
    assert res["deck_id"] == 1
    assert res["deck_name"] == "Default"
    conf = res["config"]
    # The dict is plain JSON with the documented scheduling keys.
    assert conf["name"] == "Default"
    assert conf["new"]["perDay"] == 20
    assert conf["rev"]["perDay"] == 200
    assert conf["maxTaken"] == 60
    assert conf["autoplay"] is True
    assert "timer" in conf

    with pytest.raises(CollectionToolError) as exc:
        await deck_tools.get_deck_config(reg, "greta", 999999)
    assert "Deck with ID 999999 not found" in str(exc.value)


# -- update_deck_config ------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_deck_config_applies_each_whitelisted_key_and_shares_preset(env):
    reg, manager = env
    await reg.run("heidi", lambda col: _add_basic_note(col, "front"))
    # A second deck: created on the default preset (config id 1), so it must
    # change WITH Default — the shared-preset behavior the description warns
    # about, pinned here. (New decks get epoch-ms ids; capture the real one.)
    second_did = await reg.run("heidi", lambda col: col.decks.add_normal_deck_with_name("Second").id)

    res = await deck_tools.update_deck_config(
        manager, reg, "heidi", 1,
        UpdateDeckConfigInput(deck_id=1, new_per_day=7, reviews_per_day=55,
                              max_answer_secs=45, autoplay_audio=False),
    )
    assert res.success is True, res.error
    assert res.local_status == "committed"
    assert res.data["deck_id"] == 1
    assert res.data["deck_name"] == "Default"
    assert res.data["updated_keys"] == ["autoplay_audio", "max_answer_secs", "new_per_day", "reviews_per_day"]
    assert res.data["config"]["new"]["perDay"] == 7
    assert res.data["config"]["rev"]["perDay"] == 55
    assert res.data["config"]["maxTaken"] == 45
    assert res.data["config"]["autoplay"] is False

    # Persisted: fresh reads through both decks (same preset) see the values.
    for deck_id in (1, second_did):
        conf = await reg.run("heidi", lambda col: col.decks.config_dict_for_deck_id(deck_id))
        assert conf["new"]["perDay"] == 7
        assert conf["rev"]["perDay"] == 55
        assert conf["maxTaken"] == 45
        assert conf["autoplay"] is False


@pytest.mark.asyncio
async def test_update_deck_config_rejects_unknown_key_listing_valid_and_bounds_checks():
    # Unknown key: refused with the valid keys named.
    with pytest.raises(ValidationError) as exc:
        UpdateDeckConfigInput(deck_id=1, new_per_day=5, bogus=1)
    message = str(exc.value)
    assert "bogus" in message
    for valid in ("new_per_day", "reviews_per_day", "max_answer_secs", "autoplay_audio"):
        assert valid in message
    # Nothing without any known key.
    with pytest.raises(ValidationError):
        UpdateDeckConfigInput(deck_id=1)
    # Bounds: per-day 0-999, max answer secs 10-120.
    for kwargs in (
        {"new_per_day": -1}, {"new_per_day": 1000},
        {"reviews_per_day": -1}, {"reviews_per_day": 1000},
        {"max_answer_secs": 9}, {"max_answer_secs": 121},
    ):
        with pytest.raises(ValidationError):
            UpdateDeckConfigInput(deck_id=1, **kwargs)
    assert UpdateDeckConfigInput(deck_id=1, new_per_day=0, reviews_per_day=999, max_answer_secs=10)
    assert UpdateDeckConfigInput(deck_id=1, max_answer_secs=120)


# -- set_card_flags ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_card_flags_sets_and_clears_via_ids_and_query(env):
    reg, manager = env
    cid1 = await reg.run("isabel", lambda col: _add_basic_note(col, "one"))
    cid2 = await reg.run("isabel", lambda col: _add_basic_note(col, "two"))

    res = await card_tools.set_card_flags(manager, reg, "isabel", flag=3, card_ids=[cid1])
    assert res.success is True, res.error
    assert res.data == {"flag": 3, "changed": 1, "selected": 1}
    assert (await _card_state(reg, "isabel", cid1))[3] == 3  # get_card exposes the flag

    # flag=0 clears.
    res = await card_tools.set_card_flags(manager, reg, "isabel", flag=0, card_ids=[cid1])
    assert res.success is True, res.error
    assert (await _card_state(reg, "isabel", cid1))[3] == 0

    # The query path reaches every matched card.
    res = await card_tools.set_card_flags(manager, reg, "isabel", flag=1, query="deck:Default")
    assert res.success is True, res.error
    assert res.data["selected"] == 2
    assert (await _card_state(reg, "isabel", cid1))[3] == 1
    assert (await _card_state(reg, "isabel", cid2))[3] == 1


# -- bury / unbury -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_bury_then_unbury_round_trip_restores_queue_state(env):
    reg, manager = env
    cid = await reg.run("jonas", lambda col: _add_basic_note(col, "one"))
    before = await _card_state(reg, "jonas", cid)
    assert before[0] == 0  # a fresh card sits in the new queue

    res = await card_tools.bury_cards(manager, reg, "jonas", card_ids=[cid])
    assert res.success is True, res.error
    assert res.data == {"buried": 1, "selected": 1}
    buried = await _card_state(reg, "jonas", cid)
    assert buried[0] == -3  # the manual-bury queue

    res = await card_tools.unbury_cards(manager, reg, "jonas", card_ids=[cid])
    assert res.success is True, res.error
    assert res.data["unburied"] == 1
    # The queue state the bury interrupted is restored exactly.
    assert await _card_state(reg, "jonas", cid) == before


@pytest.mark.asyncio
async def test_unbury_also_restores_suspended_cards(env):
    """Pins the disclosed semantics: the wheel's unbury IS
    restore_buried_and_suspended_cards, so a SUSPENDED card in the selection
    comes back too. Documented in the tool description."""
    reg, manager = env
    cids = await reg.run("katja", lambda col: [
        _add_basic_note(col, "bury me"), _add_basic_note(col, "suspend me"),
    ])

    def _bury_and_suspend(col):
        col.sched.bury_cards([cids[0]], manual=True)
        col.sched.suspend_cards([cids[1]])

    await reg.run("katja", _bury_and_suspend)
    assert (await _card_state(reg, "katja", cids[0]))[0] == -3  # buried
    assert (await _card_state(reg, "katja", cids[1]))[0] == -1  # suspended

    res = await card_tools.unbury_cards(manager, reg, "katja", card_ids=cids)
    assert res.success is True, res.error
    # BOTH restored to their pre-bury/suspend queues (0 = new).
    assert (await _card_state(reg, "katja", cids[0]))[0] == 0
    assert (await _card_state(reg, "katja", cids[1]))[0] == 0


# -- reposition_new_cards ----------------------------------------------------------


@pytest.mark.asyncio
async def test_reposition_changes_due_order_and_ignores_review_cards(env):
    reg, manager = env
    cids = await reg.run("lars", lambda col: [
        _add_basic_note(col, "new one"), _add_basic_note(col, "new two"),
        _add_basic_note(col, "review me"),
    ])
    review_cid, new_cids = cids[2], [cids[0], cids[1]]

    def _to_review(col):
        col.sched.set_due_date([review_cid], "5")

    await reg.run("lars", _to_review)
    review_before = await _card_state(reg, "lars", review_cid)
    assert review_before[0] == 2  # now a review card

    res = await card_tools.reposition_new_cards(
        manager, reg, "lars", start=1000, step=5, card_ids=[review_cid, *new_cids],
    )
    assert res.success is True, res.error
    # The review card was selected but not repositioned; each NEW card got
    # start + input_index * step (probed: the wheel skips non-new cards
    # without collapsing their slots, so the review card's index is lost).
    assert res.data["repositioned"] == 2
    assert res.data["selected"] == 3
    assert (await _card_state(reg, "lars", new_cids[0]))[2] == 1005
    assert (await _card_state(reg, "lars", new_cids[1]))[2] == 1010
    assert await _card_state(reg, "lars", review_cid) == review_before


# -- selection validation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_card_selection_is_exactly_one_of_card_ids_or_query_with_the_2000_cap():
    for model in (BuryCardsInput, UnburyCardsInput, SetCardFlagsInput, RepositionNewCardsInput):
        kwargs = {"flag": 1} if model is SetCardFlagsInput else (
            {"start": 0, "step": 1} if model is RepositionNewCardsInput else {}
        )
        with pytest.raises(ValidationError):
            model(**kwargs)  # neither
        with pytest.raises(ValidationError):
            model(**kwargs, card_ids=[1], query="deck:Default")  # both
        assert model(**kwargs, query="deck:Default").query == "deck:Default"
        assert model(**kwargs, card_ids=[5]).card_ids == [5]
        # Same 1-2000 bound and rationale as the note-type change.
        with pytest.raises(ValidationError):
            model(**kwargs, card_ids=[0])
        with pytest.raises(ValidationError):
            model(**kwargs, card_ids=list(range(1, 2002)))
        assert model(**kwargs, card_ids=list(range(1, 2001)))

    # flag 0-7 in pydantic; reposition start >= 0, step 1-9999.
    for flag in (-1, 8):
        with pytest.raises(ValidationError):
            SetCardFlagsInput(flag=flag, card_ids=[1])
    assert SetCardFlagsInput(flag=0, card_ids=[1]).flag == 0
    assert SetCardFlagsInput(flag=7, card_ids=[1]).flag == 7
    with pytest.raises(ValidationError):
        RepositionNewCardsInput(start=-1, step=1, card_ids=[1])
    for step in (0, 10000):
        with pytest.raises(ValidationError):
            RepositionNewCardsInput(start=0, step=step, card_ids=[1])
    assert RepositionNewCardsInput(start=0, step=9999, card_ids=[1])


@pytest.mark.asyncio
async def test_bad_selections_fail_clean_before_any_backend_call(env):
    reg, manager = env
    await reg.run("mara", lambda col: _add_basic_note(col, "one"))

    # A malformed search becomes a clean error echoing the query, not the
    # wheel's raw SearchError (mutation failures surface as a failed
    # MutationResponse, the same contract as change_note_type).
    res = await card_tools.bury_cards(manager, reg, "mara", query='"')
    assert res.success is False
    assert res.local_status == "failed"
    assert "Invalid search query" in res.error
    assert "'\"'" in res.error

    # A matching-but-empty query is refused rather than applied as a no-op.
    res = await card_tools.bury_cards(manager, reg, "mara", query="deck:NoSuchDeck")
    assert res.success is False and res.local_status == "failed"
    assert "No cards matched query" in res.error

    # Unknown IDs are refused before the mutation (backend calls would
    # silently ignore them).
    res = await card_tools.unbury_cards(manager, reg, "mara", card_ids=[424242])
    assert res.success is False and res.local_status == "failed"
    assert "card(s) not found: [424242]" in res.error
    res = await card_tools.set_card_flags(manager, reg, "mara", flag=1, card_ids=[424242])
    assert res.success is False and res.local_status == "failed"
    assert "card(s) not found: [424242]" in res.error
