"""Explicit synchronization tools."""

import logging
import time
from typing import Any, Dict, Optional

from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.sync")


async def sync_collection(
    sync_manager: SyncManager,
    username: str,
    full_sync_direction: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Trigger manual collection synchronization with AnkiWeb.

    Thin pass-through: the manager assembles the schema-compatible response
    (and raises ValueError when the user's AnkiWeb credentials are missing,
    which the wrapper converts into a structured error). ``full_sync_direction``
    is only consulted when AnkiWeb demands a user-chosen full-sync direction
    (see SyncManager.sync_now).
    """
    logger.info("Executing sync_collection for %r", username)
    return await sync_manager.sync_now(
        username,
        idempotency_key=idempotency_key,
        full_sync_direction=full_sync_direction,
    )


async def get_sync_status(sync_manager: SyncManager, username: str) -> Dict[str, Any]:
    """Inspect AnkiWeb sync configuration and cooldown state without triggering a network sync."""
    logger.info("Executing get_sync_status for %r", username)
    user = sync_manager.store.get_user(username)
    ankiweb_email = user.ankiweb_email if user is not None else ""
    configured = bool(user is not None and user.ankiweb_email and user.ankiweb_password)
    last_stamp = sync_manager._last_pretool_sync.get(username)
    last_age = round(max(0.0, time.monotonic() - last_stamp), 1) if last_stamp is not None else None
    return {
        "ankiweb_configured": configured,
        "ankiweb_email": ankiweb_email,
        "sync_before_tools": bool(sync_manager.settings.sync_before_tools),
        "sync_before_tools_ttl_seconds": int(sync_manager.settings.sync_before_tools_ttl_seconds),
        "maintenance_mode": bool(sync_manager.maintenance_mode),
        "last_sync_age_seconds": last_age,
    }
