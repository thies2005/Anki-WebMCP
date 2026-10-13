"""Explicit synchronization tools."""

import logging
from typing import Any, Dict, Optional

from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.sync")


async def sync_collection(
    sync_manager: SyncManager, username: str, full_sync_direction: Optional[str] = None
) -> Dict[str, Any]:
    """Trigger manual collection synchronization with AnkiWeb.

    Thin pass-through: the manager assembles the schema-compatible response
    (and raises ValueError when the user's AnkiWeb credentials are missing,
    which the wrapper converts into a structured error). ``full_sync_direction``
    is only consulted when AnkiWeb demands a user-chosen full-sync direction
    (see SyncManager.sync_now).
    """
    logger.info("Executing sync_collection for %r", username)
    return await sync_manager.sync_now(username, full_sync_direction=full_sync_direction)
