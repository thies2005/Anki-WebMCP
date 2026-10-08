"""Deck management tools."""

import logging
from typing import Any, Dict, List
from ..anki_client import AnkiClient

logger = logging.getLogger("anki_mcp.tools.decks")


async def list_decks(client: AnkiClient) -> Dict[str, Any]:
    """Retrieve all deck names and their IDs from the local Anki collection."""
    logger.info("Executing list_decks")
    raw_decks = await client.invoke("deckNamesAndIds")
    if not isinstance(raw_decks, dict):
        raw_names = await client.invoke("deckNames")
        decks_list = [{"name": name, "id": None} for name in (raw_names or [])]
    else:
        decks_list = [{"name": name, "id": deck_id} for name, deck_id in raw_decks.items()]

    return {
        "count": len(decks_list),
        "decks": decks_list,
    }
