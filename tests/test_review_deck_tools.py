"""Deck-scoped review tools (get_review_queue / answer_card `deck` support).

Regression for the deck-blindness bug: the v3 scheduler serves ONLY the
currently selected deck, nothing in the service ever selected one, so
headless collections sat on 'Default' forever — get_review_queue returned 0
cards for real decks and answer_card refused every card outside Default.
"""

import pytest
import pytest_asyncio

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.errors import CollectionToolError
from src.sync_manager import SyncManager
from src.users import UserStore
from src.tools import cards as card_tools


def add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return note.id


@pytest_asyncio.fixture
async def env(tmp_path):
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


@pytest_asyncio.fixture
async def korean_cards(env):
    """Three new cards in 'Korean::Vocab' (NOT Default) for user 'riri'."""
    reg, manager = env
    for i in range(3):
        await reg.run("riri", add_basic_note, f"ko-{i}", f"en-{i}", deck="Korean::Vocab")
    return env


async def _current_deck_id(reg, username):
    return await reg.run(username, lambda col: col.decks.get_current_id())


# -- get_review_queue: deck parameter -------------------------------------------------


@pytest.mark.asyncio
async def test_queue_for_named_deck_returns_its_cards(korean_cards):
    reg, _ = korean_cards
    q = await card_tools.get_review_queue(reg, "riri", deck="Korean::Vocab")
    assert q["deck_name"] == "Korean::Vocab"
    assert q["total_queue_count"] == 3
    assert [c["deck_name"] for c in q["cards"]] == ["Korean::Vocab"] * 3


@pytest.mark.asyncio
async def test_queue_without_deck_reports_the_usually_empty_default(korean_cards):
    """No deck arg = currently selected deck. On a fresh headless collection
    that is 'Default' — the response must SAY so instead of silently looking
    like the collection has no cards."""
    reg, _ = korean_cards
    q = await card_tools.get_review_queue(reg, "riri")
    assert q["deck_name"] == "Default"
    assert q["total_queue_count"] == 0
    assert q["cards"] == []


@pytest.mark.asyncio
async def test_queue_unknown_deck_error_lists_hint(env):
    reg, _ = env
    await reg.run("riri", add_basic_note, "q", "a")
    try:
        await card_tools.get_review_queue(reg, "riri", deck="Nope")
    except CollectionToolError as e:
        assert "Nope" in str(e)
        assert "list_decks" in str(e)
    else:
        raise AssertionError("unknown deck must raise CollectionToolError")


@pytest.mark.asyncio
async def test_queue_deck_selection_is_restored(korean_cards):
    """curDeck is persisted collection state; a review read must put it back."""
    reg, _ = korean_cards
    before = await _current_deck_id(reg, "riri")
    await card_tools.get_review_queue(reg, "riri", deck="Korean::Vocab")
    # also through the error path
    try:
        await card_tools.get_review_queue(reg, "riri", deck="Missing")
    except CollectionToolError:
        pass
    assert await _current_deck_id(reg, "riri") == before


# -- answer_card: auto deck resolution ------------------------------------------------


@pytest.mark.asyncio
async def test_answer_card_answers_card_outside_default_without_deck_arg(korean_cards):
    """The reported bug: a card due in a real deck was refused because the
    queue only ever covered Default. The card's own deck is now used."""
    reg, manager = korean_cards
    found = await card_tools.find_cards(reg, "riri", "deck:Korean::Vocab")
    cid = found["cards"][0]["card_id"]

    res = await card_tools.answer_card(manager, reg, "riri", cid, 3)  # Good
    assert res.success is True, res.error
    assert res.data["card_id"] == cid
    assert res.data["deck_name"] == "Korean::Vocab"


@pytest.mark.asyncio
async def test_answer_card_explicit_deck_pins_the_scope(korean_cards):
    """deck= requires the card to be queued THERE: a Korean card 'queued in
    Default' is a refusal, and the error names the deck it checked."""
    reg, manager = korean_cards
    found = await card_tools.find_cards(reg, "riri", "deck:Korean::Vocab")
    cid = found["cards"][0]["card_id"]

    res = await card_tools.answer_card(manager, reg, "riri", cid, 3, deck="Default")
    assert res.success is False
    assert "not currently in the review queue" in res.error
    assert "Default" in res.error

    # ...while the same card with its own deck answers fine.
    res2 = await card_tools.answer_card(manager, reg, "riri", cid, 3, deck="Korean::Vocab")
    assert res2.success is True, res2.error


@pytest.mark.asyncio
async def test_answer_card_selection_is_restored(korean_cards):
    reg, manager = korean_cards
    found = await card_tools.find_cards(reg, "riri", "deck:Korean::Vocab")
    cid = found["cards"][0]["card_id"]
    before = await _current_deck_id(reg, "riri")
    await card_tools.answer_card(manager, reg, "riri", cid, 3)
    assert await _current_deck_id(reg, "riri") == before


@pytest.mark.asyncio
async def test_queue_total_counts_beyond_fetch_limit(korean_cards):
    """total_queue_count is the TRUE remaining queue, not the fetch-limited
    prefix: 3 queued cards fetched with limit=1 must still report 3."""
    reg, _ = korean_cards
    q = await card_tools.get_review_queue(reg, "riri", limit=1, deck="Korean::Vocab")
    assert len(q["cards"]) == 1
    assert q["total_queue_count"] == 3


# -- parent decks include subdecks ----------------------------------------------------


@pytest.mark.asyncio
async def test_parent_deck_queue_includes_subdeck_cards(env):
    reg, _ = env
    await reg.run("riri", add_basic_note, "parent-q", "a", deck="Korean")
    await reg.run("riri", add_basic_note, "child-q", "a", deck="Korean::Vocab")
    q = await card_tools.get_review_queue(reg, "riri", deck="Korean")
    names = {c["question"] for c in q["cards"]}
    assert any("parent-q" in n for n in names)
    assert any("child-q" in n for n in names)
