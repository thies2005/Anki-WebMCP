"""Collection statistics tools exposing reliably supported information."""

import logging
from typing import Any, Dict, List
from ..anki_client import AnkiClient

logger = logging.getLogger("anki_mcp.tools.stats")


async def get_collection_stats(client: AnkiClient) -> Dict[str, Any]:
    """Retrieve reliably supported collection and deck statistics."""
    logger.info("Executing get_collection_stats")

    # 1. Fetch deck names
    deck_names = await client.invoke("deckNames") or []

    # 2. Fetch deck-level stats
    raw_stats = await client.invoke("getDeckStats", {"decks": deck_names}) or {}

    # 3. Fetch reviewed today count
    try:
        reviewed_today = await client.invoke("getNumCardsReviewedToday")
        if not isinstance(reviewed_today, (int, float)):
            reviewed_today = 0
    except Exception as e:
        logger.warning("Could not fetch reviewed cards today: %s", e)
        reviewed_today = None

    # Aggregate summaries
    decks: List[Dict[str, Any]] = []
    total_new = 0
    total_learn = 0
    total_review = 0
    total_cards = 0

    if isinstance(raw_stats, dict):
        for _, d_stat in raw_stats.items():
            if not isinstance(d_stat, dict):
                continue
            name = d_stat.get("name", "")
            new_c = d_stat.get("new_count", 0)
            learn_c = d_stat.get("learn_count", 0)
            rev_c = d_stat.get("review_count", 0)
            tot_c = d_stat.get("total_in_deck", 0)

            total_new += new_c
            total_learn += learn_c
            total_review += rev_c
            total_cards += tot_c

            decks.append({
                "deck_id": d_stat.get("deck_id"),
                "name": name,
                "new_count": new_c,
                "learn_count": learn_c,
                "review_count": rev_c,
                "total_in_deck": tot_c,
            })

    return {
        "cards_reviewed_today": reviewed_today,
        "deck_count": len(decks),
        "summary": {
            "total_new": total_new,
            "total_learn": total_learn,
            "total_review": total_review,
            "total_cards": total_cards,
        },
        "decks": decks,
    }
