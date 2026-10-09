"""Collection statistics tools exposing reliably supported information."""

import logging
from typing import Any, Dict, List

logger = logging.getLogger("anki_mcp.tools.stats")


def _stats_impl(col) -> Dict[str, Any]:
    """Per-deck counts from ONE grouped cards-table query, plus reviewed-today.

    Deck queue semantics (same conventions as the portal's deck stats):
    0 = new, 1 = learning, 2/3 = review (incl. day-learn). Reviewed-today is
    the revlog entry count since the scheduler's day cutoff (revlog ids are
    epoch milliseconds).
    """
    rows = col.db.all(
        "SELECT did, SUM(queue = 0), SUM(queue = 1), SUM(queue IN (2, 3)), COUNT() "
        "FROM cards GROUP BY did"
    )
    counts_by_did = {row[0]: row for row in rows}

    decks: List[Dict[str, Any]] = []
    total_new = 0
    total_learn = 0
    total_review = 0
    total_cards = 0
    for deck in sorted(col.decks.all_names_and_ids(), key=lambda d: d.name.lower()):
        row = counts_by_did.get(deck.id, (0, 0, 0, 0, 0))
        new_c = int(row[1] or 0)
        learn_c = int(row[2] or 0)
        rev_c = int(row[3] or 0)
        tot_c = int(row[4] or 0)
        total_new += new_c
        total_learn += learn_c
        total_review += rev_c
        total_cards += tot_c
        decks.append({
            "deck_id": deck.id,
            "name": deck.name,
            "new_count": new_c,
            "learn_count": learn_c,
            "review_count": rev_c,
            "total_in_deck": tot_c,
        })

    cutoff_ms = int(col.sched.day_cutoff) * 1000
    reviewed_today = col.db.scalar("SELECT COUNT() FROM revlog WHERE id >= ?", cutoff_ms)
    return {
        "cards_reviewed_today": int(reviewed_today or 0),
        "deck_count": len(decks),
        "summary": {
            "total_new": total_new,
            "total_learn": total_learn,
            "total_review": total_review,
            "total_cards": total_cards,
        },
        "decks": decks,
    }


async def get_collection_stats(registry, username: str) -> Dict[str, Any]:
    """Retrieve reliably supported collection and deck statistics."""
    logger.info("Executing get_collection_stats for %r", username)
    return await registry.run(username, _stats_impl)
