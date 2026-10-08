"""Explicit synchronization tools."""

import logging
from typing import Any, Dict
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.sync")


async def sync_collection(sync_manager: SyncManager) -> Dict[str, Any]:
    """Trigger manual collection synchronization with official AnkiWeb."""
    logger.info("Executing sync_collection")
    sync_status = await sync_manager.sync_now()

    success = (sync_status.post_sync == "ok")
    message = "Synchronization completed successfully." if success else "Synchronization did not succeed."
    if sync_status.post_sync == "auth_not_configured":
        message = "Official AnkiWeb account is not configured yet. Please log in via the Desktop GUI first."
    elif sync_status.post_sync == "failed":
        message = f"Synchronization error: {sync_status.post_sync_error}"

    return {
        "success": success,
        "sync_status": sync_status.model_dump(),
        "message": message,
    }
