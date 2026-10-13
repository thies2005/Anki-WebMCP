"""Card search, inspection, and scheduling tools.

Read-only tools run via ``registry.run`` directly; scheduling mutations go
through ``sync_manager.execute_mutation``. Wheel facts this module relies on
(all probed against the pinned 26.9.3 wheel):

- ``col.sched.get_queued_cards`` returns a ``QueuedCards`` protobuf WITHOUT a
  ``card_count`` attribute (the fallback is the fetched list's length), and
  its items' ``.card`` is the cards_pb2 PROTO, not an ``anki.cards.Card`` —
  ``build_answer`` needs the real object from ``col.get_card``.
- ``Card.timer_started`` is None until ``start_timer()``; answering without
  starting the timer raises TypeError inside ``build_answer``.
- ``CardAnswer.Rating`` proto values are AGAIN=0, HARD=1, GOOD=2, EASY=3; the
  tool surface's 1..4 rating maps onto them by the named mapping below.
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from anki.errors import NotFoundError, SearchError  # isort: skip
from anki.scheduler.v3 import CardAnswer

from ..errors import CollectionToolError
from ..models import MutationResponse, SyncStatus
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.cards")

# Tool-surface rating (1..4) -> proto CardAnswer.Rating (AGAIN=0..EASY=3).
_RATING_BY_LEVEL = {
    1: CardAnswer.Rating.AGAIN,
    2: CardAnswer.Rating.HARD,
    3: CardAnswer.Rating.GOOD,
    4: CardAnswer.Rating.EASY,
}


def _get_card_or_none(col, card_id: int):
    try:
        return col.get_card(card_id)
    except NotFoundError:
        return None


def _card_row(col, card) -> Dict[str, Any]:
    """The find_cards summary row shape."""
    return {
        "card_id": int(card.id),
        "note_id": int(card.nid),
        "deck_name": col.decks.name(card.did),
        "queue": card.queue,
        "type": card.type,
        "due": card.due,
        "ivl": card.ivl,
        "reps": card.reps,
        "lapses": card.lapses,
    }


def _truncate_html(text: str, cap: int) -> Tuple[str, bool]:
    """Truncate rendered HTML to ``cap`` chars; report whether it was cut."""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _partition_existing(col, card_ids: List[int]) -> Tuple[List[int], List[int]]:
    """Split requested card IDs into existing and missing (single read each)."""
    existing = []
    missing = []
    for cid in card_ids:
        (existing if _get_card_or_none(col, cid) is not None else missing).append(cid)
    return existing, missing


async def find_cards(
    registry,
    username: str,
    query: str,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search cards in the collection with bounded pagination."""
    # Clamp pagination bounds (direct calls bypass the pydantic input model).
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    logger.info("Executing find_cards for %r: query=%r, limit=%d, offset=%d", username, query, limit, offset)

    def _find(col) -> Dict[str, Any]:
        card_ids = list(col.find_cards(query))
        total = len(card_ids)
        rows = []
        for cid in card_ids[offset : offset + limit]:
            card = _get_card_or_none(col, cid)
            if card is None:
                continue  # vanished between find_cards and the read
            rows.append(_card_row(col, card))
        return {"total": total, "cards": rows}

    result = await registry.run(username, _find)
    return {
        "total_matches": result["total"],
        "returned_count": len(result["cards"]),
        "offset": offset,
        "limit": limit,
        "cards": result["cards"],
    }


async def get_card(registry, username: str, card_id: int) -> Dict[str, Any]:
    """Retrieve a card with note fields, rendered question/answer (capped) and scheduling state."""
    logger.info("Executing get_card for %r, id %d", username, card_id)

    def _get(col) -> Dict[str, Any]:
        card = _get_card_or_none(col, card_id)
        if card is None:
            raise CollectionToolError(f"Card with ID {card_id} not found in collection.")
        note = col.get_note(card.nid)
        question, q_trunc = _truncate_html(card.question(), 10000)
        answer, a_trunc = _truncate_html(card.answer(), 10000)
        return {
            "card_id": int(card.id),
            "note_id": int(card.nid),
            "deck_name": col.decks.name(card.did),
            "model_name": note.note_type()["name"],
            "tags": list(note.tags),
            "fields": dict(note.items()),
            "question": question,
            "answer": answer,
            "truncated": q_trunc or a_trunc,
            "queue": card.queue,
            "type": card.type,
            "due": card.due,
            "ivl": card.ivl,
            "reps": card.reps,
            "lapses": card.lapses,
            "flags": card.flags,
        }

    return await registry.run(username, _get)


def _require_any_existing(card_ids: List[int], existing: List[int], missing: List[int]) -> None:
    if not existing:
        raise CollectionToolError(f"None of the requested cards exist: {missing}")


async def _ids_mutation(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: List[int],
    operation_name: str,
    apply_fn,
    result_key: str,
) -> MutationResponse:
    """Shared batch shape: report missing IDs, apply to the existing ones.

    Batches with SOME unknown IDs still apply to the known ones (the response
    carries ``missing``); a batch with NO known IDs is a domain error.
    ``apply_fn(col, existing)`` performs the wheel call and returns extra
    response fields.
    """

    def _apply(col) -> Dict[str, Any]:
        existing, missing = _partition_existing(col, card_ids)
        _require_any_existing(card_ids, existing, missing)
        payload = apply_fn(col, existing)
        return {"card_ids": existing, "missing": missing, result_key: len(existing), **payload}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _apply)

    return await sync_manager.execute_mutation(
        username,
        operation_name=operation_name,
        mutation_fn=_mutation,
    )


async def suspend_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: List[int],
) -> MutationResponse:
    """Suspend cards (they leave the review queue until unsuspended)."""
    logger.info("Executing suspend_cards for %r over %d card(s)", username, len(card_ids))

    def _apply(col, existing):
        col.sched.suspend_cards(existing)
        return {}

    return await _ids_mutation(
        sync_manager, registry, username, card_ids, "suspend_cards", _apply, "suspended",
    )


async def unsuspend_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: List[int],
) -> MutationResponse:
    """Unsuspend previously suspended cards."""
    logger.info("Executing unsuspend_cards for %r over %d card(s)", username, len(card_ids))

    def _apply(col, existing):
        col.sched.unsuspend_cards(existing)
        return {}

    return await _ids_mutation(
        sync_manager, registry, username, card_ids, "unsuspend_cards", _apply, "unsuspended",
    )


async def set_due_date(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: List[int],
    days: str,
) -> MutationResponse:
    """Set cards' due date to N days from today (or a random range like '0-2')."""
    logger.info("Executing set_due_date for %r over %d card(s): days=%r", username, len(card_ids), days)

    def _apply(col, existing):
        col.sched.set_due_date(existing, days, None)
        return {"days": days}

    return await _ids_mutation(
        sync_manager, registry, username, card_ids, "set_due_date", _apply, "updated",
    )


async def move_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: List[int],
    deck_id: int,
) -> MutationResponse:
    """Move cards to another deck (existence of the target deck is verified)."""
    logger.info("Executing move_cards for %r over %d card(s) -> deck %d", username, len(card_ids), deck_id)

    def _apply(col, existing):
        if deck_id not in {d.id for d in col.decks.all_names_and_ids()}:
            raise CollectionToolError(f"Deck with ID {deck_id} not found in collection.")
        col.set_deck(existing, deck_id)
        return {"deck_id": deck_id}

    return await _ids_mutation(
        sync_manager, registry, username, card_ids, "move_cards", _apply, "moved",
    )


def _queue_deck_id(col, deck: Optional[str], card_id: Optional[int] = None) -> int:
    """Resolve which deck's queue a review operation should use.

    The v3 scheduler's ``get_queued_cards`` serves ONLY the currently
    selected deck (plus its subdecks), and nothing else in the service ever
    selects a deck — headless collections sit on 'Default' forever, which is
    why real decks' cards used to be invisible to the review tools. Rules:

    - explicit ``deck`` name -> that deck (must exist; ``decks.id_for_name``
      only RESOLVES, unlike ``decks.id`` which would create it);
    - no deck + ``card_id`` (answer_card's default) -> the card's own deck,
      so any due card can be answered without knowing its deck;
    - no deck + no card -> the currently selected deck (Anki desktop
      semantics; 'Default' on a fresh collection).
    """
    if deck is not None:
        deck_id = col.decks.id_for_name(deck)
        if not deck_id:
            raise CollectionToolError(
                f"Deck {deck!r} not found in the collection. "
                "Call list_decks for the valid deck names."
            )
        return int(deck_id)
    if card_id is not None:
        card = _get_card_or_none(col, card_id)
        if card is None:
            raise CollectionToolError(f"Card {card_id} not found in the collection.")
        return int(card.did)
    return int(col.decks.get_current_id())


@contextmanager
def _deck_selection(col, deck_id: int):
    """Point the scheduler at ``deck_id`` for the duration, then restore.

    ``curDeck`` is PERSISTED collection state; a review read must not
    silently rewrite it, so the previous selection always goes back (even on
    error). Selecting a parent deck natively includes its subdecks.
    """
    previous = col.decks.get_current_id()
    col.decks.select(deck_id)
    try:
        yield
    finally:
        col.decks.select(previous)


async def get_review_queue(
    registry, username: str, limit: int = 10, deck: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch the scheduler's current queue (due + new cards), questions capped.

    The queue covers exactly ONE deck (subdecks included): ``deck`` when
    given, else the collection's currently selected deck. The response names
    the deck it came from, so an empty queue is self-explaining instead of
    looking like the collection has no cards.
    """
    limit = max(1, min(limit, 50))
    logger.info("Executing get_review_queue for %r: limit=%d, deck=%r", username, limit, deck)

    def _queue(col) -> Dict[str, Any]:
        deck_id = _queue_deck_id(col, deck)
        with _deck_selection(col, deck_id):
            out = col.sched.get_queued_cards(fetch_limit=limit)
        items = []
        for queued in out.cards:
            card = col.get_card(queued.card.id)
            question, _ = _truncate_html(card.question(), 2000)
            items.append(
                {
                    "card_id": int(card.id),
                    "note_id": int(card.nid),
                    "deck_name": col.decks.name(card.did),
                    "question": question,
                }
            )
        # The wheel's QueuedCards proto (26.9.3, probed) has no card_count
        # attribute; the true remaining queue is the sum of the three per-kind
        # counts (len(out.cards) is only the fetch-limited prefix).
        total = int(out.new_count) + int(out.learning_count) + int(out.review_count)
        return {
            "deck_id": deck_id,
            "deck_name": col.decks.name(deck_id),
            "total_queue_count": total,
            "cards": items,
        }

    return await registry.run(username, _queue)


async def answer_card(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_id: int,
    rating: int,
    deck: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> MutationResponse:
    """Answer a queued card with a 1..4 rating (Again/Hard/Good/Easy).

    The card must currently be in the scheduler's queue; the answer is built
    from the queue's scheduling states (the v3 path) and applied via
    ``sched.answer_card``. The card's answer timer is started at answer time —
    an unattended tool has no "question shown" moment, and build_answer
    raises on a never-started timer (probed against the wheel).

    Deck scoping mirrors get_review_queue: by default the card's OWN deck's
    queue is consulted (any due card is answerable); an explicit ``deck``
    requires the card to be queued there.
    """
    logger.info(
        "Executing answer_card for %r: card %d, rating %d, deck=%r", username, card_id, rating, deck
    )

    def _answer(col) -> Dict[str, Any]:
        deck_id = _queue_deck_id(col, deck, card_id=card_id)
        with _deck_selection(col, deck_id):
            out = col.sched.get_queued_cards(fetch_limit=50)
            queued = next((q for q in out.cards if q.card.id == card_id), None)
            if queued is None:
                raise CollectionToolError(
                    f"card {card_id} is not currently in the review queue (not due or not "
                    f"scheduled under today's limits), or it sits deeper than the first 50 "
                    f"queued cards of deck {col.decks.name(deck_id)!r}. Call get_review_queue "
                    "with the same deck to see what is currently queued."
                )
            card = col.get_card(card_id)
            card.start_timer()
            answer = col.sched.build_answer(card=card, states=queued.states, rating=_RATING_BY_LEVEL[rating])
            col.sched.answer_card(answer)
            reloaded = col.get_card(card_id)
            after = col.sched.get_queued_cards(fetch_limit=50)
            # The deck's TRUE remaining queue: the same per-kind sum
            # get_review_queue reports (len(after.cards) is only the
            # fetch-limited prefix, not the whole queue). The answered card
            # may legitimately still be counted here when it re-enters
            # today's learning queue (Again / a remaining learning step).
            remaining = int(after.new_count) + int(after.learning_count) + int(after.review_count)
        return {
            "card_id": card_id,
            "rating": rating,
            "deck_name": col.decks.name(deck_id),
            "next_due": reloaded.due,
            "remaining_in_queue": remaining,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _answer)

    return await sync_manager.execute_mutation(
        username,
        operation_name="answer_card",
        mutation_fn=_mutation,
        idempotency_key=idempotency_key,
    )


# -- flagging, burying, and new-card repositioning (roadmap phase 4) -----------------


# Chunk size for IN-queries over card selections (mirrors notes.py
# _QUERY_CHUNK): selections are unbounded and SQLite caps host parameters
# (999 on old builds), so selection-wide reads run in bounded batches.
_QUERY_CHUNK = 500


def _resolve_card_selection(
    col,
    card_ids: Optional[List[int]],
    query: Optional[str],
    missing_prefix: str,
) -> List[int]:
    """Resolve a card_ids-XOR-query selection to deduplicated, existing card IDs.

    Mirror of ``_resolve_note_selection`` (tools/notes.py), shared by all four
    phase-4 card tools: both branches verify existence BEFORE any backend
    mutation — a bad selection must fail the request in Python, never as a
    backend error (the registry would mistake an ``InvalidInput`` for a stale
    handle and re-run the closure once). A malformed search raises the wheel's
    ``SearchError``; it surfaces as a clean CollectionToolError echoing the
    query instead of the backend's raw error.
    """
    if query is not None:
        # Client-controlled search string: %r-escape in errors/logs (CWE-117).
        try:
            cids = list(col.find_cards(query))
        except SearchError as exc:
            raise CollectionToolError(f"Invalid search query {query!r}: {exc}") from exc
        if not cids:
            raise CollectionToolError(f"No cards matched query {query!r}; nothing to change.")
        return cids
    # Dedupe preserving order: a repeated ID would inflate the reported counts.
    cids = list(dict.fromkeys(int(cid) for cid in card_ids))
    placeholders = ",".join("?" * len(cids))
    found = set(col.db.list(f"SELECT id FROM cards WHERE id IN ({placeholders})", *cids))
    missing = [cid for cid in cids if cid not in found]
    if missing:
        raise CollectionToolError(f"{missing_prefix}: card(s) not found: {missing}")
    return cids


async def set_card_flags(
    sync_manager: SyncManager,
    registry,
    username: str,
    flag: int,
    card_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
) -> MutationResponse:
    """Set (or clear, flag=0) the user flag of the selected cards."""
    logger.info(
        "Executing set_card_flags for %r: flag=%d (%d id(s), query=%r)",
        username, flag, len(card_ids) if card_ids else 0, query,
    )

    def _apply(col) -> Dict[str, Any]:
        cids = _resolve_card_selection(col, card_ids, query, "Cannot set card flags")
        result = col.set_user_flag_for_cards(flag, cids)
        return {"flag": flag, "changed": int(result.count), "selected": len(cids)}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _apply)

    return await sync_manager.execute_mutation(
        username,
        operation_name="set_card_flags",
        mutation_fn=_mutation,
    )


async def bury_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
) -> MutationResponse:
    """Bury the selected cards until the next day rollover (or unbury_cards)."""
    logger.info(
        "Executing bury_cards for %r (%d id(s), query=%r)",
        username, len(card_ids) if card_ids else 0, query,
    )

    def _apply(col) -> Dict[str, Any]:
        cids = _resolve_card_selection(col, card_ids, query, "Cannot bury cards")
        result = col.sched.bury_cards(cids, manual=True)
        return {"buried": int(result.count), "selected": len(cids)}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _apply)

    return await sync_manager.execute_mutation(
        username,
        operation_name="bury_cards",
        mutation_fn=_mutation,
    )


async def unbury_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    card_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
) -> MutationResponse:
    """Unbury the selected cards, reporting how many were actually restored.

    Wheel semantics (probed: the backend operation is
    ``restore_buried_and_suspended_cards``): this restores BURIED AND
    SUSPENDED cards alike, so suspended cards in the selection are
    unsuspended too — disclosed in the tool description. ``unburied`` counts
    the selection cards that were actually buried or suspended (queue < 0)
    just before the call; ``selected`` is the selection size.
    """
    logger.info(
        "Executing unbury_cards for %r (%d id(s), query=%r)",
        username, len(card_ids) if card_ids else 0, query,
    )

    def _apply(col) -> Dict[str, Any]:
        cids = _resolve_card_selection(col, card_ids, query, "Cannot unbury cards")
        # OpChanges carries no count (probed wheel API), so count the cards
        # the restore actually affects: buried (-3) or suspended (-1) cards
        # have queue < 0; active queues are >= 0 (probed wheel behavior).
        restored_count = 0
        for start in range(0, len(cids), _QUERY_CHUNK):
            chunk = cids[start : start + _QUERY_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            restored_count += col.db.scalar(
                f"SELECT COUNT() FROM cards WHERE id IN ({placeholders}) AND queue < 0",
                *chunk,
            )
        col.sched.unbury_cards(cids)
        return {"unburied": int(restored_count), "selected": len(cids)}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _apply)

    return await sync_manager.execute_mutation(
        username,
        operation_name="unbury_cards",
        mutation_fn=_mutation,
    )


async def reposition_new_cards(
    sync_manager: SyncManager,
    registry,
    username: str,
    start: int,
    step: int,
    card_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
    randomize: bool = False,
    shift_existing: bool = False,
) -> MutationResponse:
    """Assign new queue positions to the selected NEW cards.

    Only NEW cards move (the backend ignores review/learning cards in the
    selection, probed wheel API), so the backend count can be smaller than
    the selection — both are reported.
    """
    logger.info(
        "Executing reposition_new_cards for %r: start=%d, step=%d, randomize=%r, "
        "shift_existing=%r (%d id(s), query=%r)",
        username, start, step, randomize, shift_existing,
        len(card_ids) if card_ids else 0, query,
    )

    def _apply(col) -> Dict[str, Any]:
        cids = _resolve_card_selection(col, card_ids, query, "Cannot reposition cards")
        result = col.sched.reposition_new_cards(
            cids,
            starting_from=start,
            step_size=step,
            randomize=randomize,
            shift_existing=shift_existing,
        )
        return {
            "repositioned": int(result.count),
            "selected": len(cids),
            "start": start,
            "step": step,
            "randomize": randomize,
            "shift_existing": shift_existing,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _apply)

    return await sync_manager.execute_mutation(
        username,
        operation_name="reposition_new_cards",
        mutation_fn=_mutation,
    )
