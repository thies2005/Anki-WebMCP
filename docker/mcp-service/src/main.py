"""Main entrypoint for Anki MCP Service."""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
import uvicorn

from .anki_client import AnkiClient
from .auth import AuthMiddleware
from .config import Settings, get_settings, validate_deployment_config
from .ledger import IdempotencyLedger
from .logging_util import setup_logging
from .models import NoteInput
from .sync_manager import SyncManager
from .tools import decks as deck_tools
from .tools import notes as note_tools
from .tools import stats as stats_tools
from .tools import sync as sync_tools

# Globals initialized in factory
settings: Settings = get_settings()
logger: logging.Logger = setup_logging(settings.log_level)
anki_client: AnkiClient = AnkiClient(settings)
ledger: IdempotencyLedger = IdempotencyLedger(settings.idempotency_ledger_path)
sync_manager: SyncManager = SyncManager(anki_client, ledger, settings)


def create_app(custom_settings: Optional[Settings] = None):
    """Create and configure the MCP Starlette ASGI application."""
    global settings, logger, anki_client, ledger, sync_manager
    if custom_settings:
        settings = custom_settings
        logger = setup_logging(settings.log_level)
        anki_client = AnkiClient(settings)
        ledger = IdempotencyLedger(settings.idempotency_ledger_path)
        sync_manager = SyncManager(anki_client, ledger, settings)

    mcp = MCPServer(
        name="anki-mcp",
        version="1.0.0",
        description="Self-hosted Anki Desktop Model Context Protocol (MCP) server",
    )

    # 1. list_decks
    @mcp.tool(description="List all available decks and their IDs in the Anki collection.")
    async def list_decks() -> Dict[str, Any]:
        return await deck_tools.list_decks(anki_client)

    # 2. search_notes
    @mcp.tool(description="Search notes in the collection by Anki query with bounded pagination.")
    async def search_notes(query: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        return await note_tools.search_notes(anki_client, query, limit, offset)

    # 3. get_note
    @mcp.tool(description="Retrieve details and fields for a specific note by ID.")
    async def get_note(note_id: int) -> Dict[str, Any]:
        return await note_tools.get_note(anki_client, note_id)

    # 4. get_collection_stats
    @mcp.tool(description="Get reliably supported collection statistics and deck summaries.")
    async def get_collection_stats() -> Dict[str, Any]:
        return await stats_tools.get_collection_stats(anki_client)

    # 5. sync_collection
    @mcp.tool(description="Trigger explicit synchronization with official AnkiWeb.")
    async def sync_collection() -> Dict[str, Any]:
        return await sync_tools.sync_collection(sync_manager)

    # 6. add_notes
    @mcp.tool(
        description="Add notes to the collection with pre-sync, duplicate prevention, and post-sync."
    )
    async def add_notes(
        notes: List[Dict[str, Any]],
        idempotency_key: Optional[str] = None,
        allow_duplicates: bool = False,
    ) -> Dict[str, Any]:
        # Validate note inputs
        parsed_notes = [NoteInput(**n) for n in notes]
        res = await note_tools.add_notes(sync_manager, parsed_notes, idempotency_key, allow_duplicates)
        return res.model_dump()

    # 7. update_note_fields
    @mcp.tool(description="Update fields on an existing note with pre-sync and post-sync.")
    async def update_note_fields(note_id: int, fields: Dict[str, str]) -> Dict[str, Any]:
        res = await note_tools.update_note_fields(sync_manager, note_id, fields)
        return res.model_dump()

    # 8. add_tags
    @mcp.tool(description="Add tags to notes with pre-sync and post-sync.")
    async def add_tags(note_ids: List[int], tags: List[str]) -> Dict[str, Any]:
        res = await note_tools.add_tags(sync_manager, note_ids, tags)
        return res.model_dump()

    # Create Starlette app for streamable HTTP with reverse-proxy support
    from mcp.server.transport_security import TransportSecuritySettings

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Healthcheck endpoint (exempt from authentication)
    async def health_endpoint(request: Request) -> JSONResponse:
        anki_healthy = await anki_client.check_health()
        return JSONResponse({
            "status": "healthy" if anki_healthy else "degraded",
            "service": "anki-mcp",
            "anki_connected": anki_healthy,
            "maintenance_mode": sync_manager.maintenance_mode,
            "auth_enabled": settings.is_auth_enabled,
        })

    app.add_route("/health", health_endpoint, methods=["GET"])

    # Attach AuthMiddleware
    app.add_middleware(AuthMiddleware, settings=settings)

    return app


def main():
    """Start uvicorn server, refusing unsafe configuration."""
    problems = validate_deployment_config(settings)
    if problems:
        for problem in problems:
            logger.error("FATAL: %s", problem)
        raise SystemExit(1)

    logger.info("Starting Anki MCP Service on %s:%d (path: %s)", settings.mcp_host, settings.mcp_port, settings.mcp_path)
    logger.info("Authentication enabled with configured MCP_AUTH_TOKEN")

    app = create_app()
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
