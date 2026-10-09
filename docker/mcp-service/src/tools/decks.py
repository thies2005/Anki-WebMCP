"""Deck management tools."""

import logging
from typing import Any, Dict, List

logger = logging.getLogger("anki_mcp.tools.decks")


def _list_decks_impl(col) -> List[Dict[str, Any]]:
    return [{"name": deck.name, "id": deck.id} for deck in col.decks.all_names_and_ids()]


async def list_decks(registry, username: str) -> Dict[str, Any]:
    """Retrieve all deck names and their IDs from the user's collection."""
    logger.info("Executing list_decks for %r", username)
    decks_list = await registry.run(username, _list_decks_impl)
    return {
        "count": len(decks_list),
        "decks": decks_list,
    }
