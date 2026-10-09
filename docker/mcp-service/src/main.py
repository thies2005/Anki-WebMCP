"""Main entrypoint for Anki MCP Service."""

import asyncio
from contextlib import asynccontextmanager
import html
import logging
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import uvicorn
from pydantic import ValidationError

from .anki_client import AnkiClient, AnkiConnectConnectionError, AnkiConnectError
from .auth import AuthMiddleware
from .config import Settings, get_settings, validate_deployment_config
from .ledger import IdempotencyLedger
from .logging_util import setup_logging
from .models import AddNotesInput, AddTagsInput, GetNoteInput, SearchNotesInput, UpdateNoteFieldsInput
from .sync_manager import SyncManager
from .tools import decks as deck_tools
from .tools import notes as note_tools
from .tools import stats as stats_tools
from .tools import sync as sync_tools
from .users import User, UserStore
from .webauth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    LoginRateLimiter,
    issue_csrf,
    issue_session,
    secure_cookies_enabled,
    verify_csrf,
    verify_session,
)

# Globals initialized in factory
settings: Settings = get_settings()
logger: logging.Logger = setup_logging(settings.log_level)
anki_client: AnkiClient = AnkiClient(settings)
ledger: IdempotencyLedger = IdempotencyLedger(settings.idempotency_ledger_path)
sync_manager: SyncManager = SyncManager(anki_client, ledger, settings)
login_rate_limiter: LoginRateLimiter = LoginRateLimiter()


def _tool_error_response(exc: Exception) -> Dict[str, Any]:
    """Convert expected AnkiConnect failures into structured tool output.

    Letting these propagate triggers the MCP SDK's generic
    "Error executing tool <name>" mask, hiding the root cause from clients.
    """
    return {
        "success": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


_PAGE_CSS = """
  body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0;
         display: flex; justify-content: center; padding-top: 10vh; }
  main { background: #fff; border: 1px solid #ddd; border-radius: 6px;
         padding: 2rem; width: 20rem; }
  h1 { font-size: 1.25rem; margin: 0 0 0.5rem; }
  label { display: block; margin: 0.75rem 0 0.25rem; }
  input { width: 100%; padding: 0.4rem; box-sizing: border-box; }
  button { margin-top: 1rem; padding: 0.45rem 1rem; cursor: pointer; }
  .error { color: #b00020; margin-top: 1rem; }
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>"""


def _login_page(error: Optional[str] = None) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _page(
        "Anki MCP - Sign in",
        f"""<h1>Sign in</h1>
<form method="post" action="/login">
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" autofocus required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>
{error_html}""",
    )


def _index_page(username: str, csrf_token: str) -> str:
    return _page(
        "Anki MCP",
        f"""<h1>Anki MCP</h1>
<p>Logged in as <strong>{html.escape(username)}</strong></p>
<form method="post" action="/logout">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
  <button type="submit">Log out</button>
</form>""",
    )


def _message_page(message: str) -> str:
    return _page(
        "Anki MCP",
        f'<p>{html.escape(message)}</p>\n<p><a href="/login">Back to sign in</a></p>',
    )


class ScopedAuthMiddleware(BaseHTTPMiddleware):
    """Legacy shared-token auth, scoped to the MCP API surface.

    The web-auth routes (/, /login, /logout) authenticate with session cookies
    and run their own CSRF checks, so the legacy bearer-token middleware must
    not guard them; everything else keeps the exact legacy behavior (including
    the /health and OPTIONS exemptions inside AuthMiddleware itself) until the
    Task 5 auth rewrite replaces this split.

    AuthMiddleware is constructed eagerly so its fail-closed contract (refusing
    to build the stack without a configured token) is preserved unchanged.
    """

    WEB_AUTH_PATHS = frozenset({"/", "/login", "/logout"})

    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self._legacy = AuthMiddleware(app, settings=settings)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.WEB_AUTH_PATHS:
            return await call_next(request)
        return await self._legacy.dispatch(request, call_next)


def create_app(custom_settings: Optional[Settings] = None):
    """Create and configure the MCP Starlette ASGI application."""
    global settings, logger, anki_client, ledger, sync_manager, login_rate_limiter
    if custom_settings:
        settings = custom_settings
        logger = setup_logging(settings.log_level)
        anki_client = AnkiClient(settings)
        ledger = IdempotencyLedger(settings.idempotency_ledger_path)
        sync_manager = SyncManager(anki_client, ledger, settings)
    login_rate_limiter = LoginRateLimiter()  # fresh per app: lockout state must not leak across tests/restarts

    mcp = MCPServer(
        name="anki-mcp",
        version="1.0.0",
        description="Self-hosted Anki Desktop Model Context Protocol (MCP) server",
    )

    # 1. list_decks
    @mcp.tool(description="List all available decks and their IDs in the Anki collection.")
    async def list_decks() -> Dict[str, Any]:
        try:
            return await deck_tools.list_decks(anki_client)
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # 2. search_notes
    @mcp.tool(description="Search notes in the collection by Anki query with bounded pagination.")
    async def search_notes(query: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        try:
            SearchNotesInput(query=query, limit=limit, offset=offset)
            return await note_tools.search_notes(anki_client, query, limit, offset)
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # 3. get_note
    @mcp.tool(description="Retrieve details and fields for a specific note by ID.")
    async def get_note(note_id: int) -> Dict[str, Any]:
        try:
            GetNoteInput(note_id=note_id)
            return await note_tools.get_note(anki_client, note_id)
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # 4. get_collection_stats
    @mcp.tool(description="Get reliably supported collection statistics and deck summaries.")
    async def get_collection_stats() -> Dict[str, Any]:
        try:
            return await stats_tools.get_collection_stats(anki_client)
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

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
        try:
            # Validate the full batch (non-empty, <=100 notes, field shapes) before any sync happens
            parsed_input = AddNotesInput(notes=notes, idempotency_key=idempotency_key, allow_duplicates=allow_duplicates)
            res = await note_tools.add_notes(
                sync_manager, parsed_input.notes, parsed_input.idempotency_key, parsed_input.allow_duplicates
            )
            return res.model_dump()
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # 7. update_note_fields
    @mcp.tool(description="Update fields on an existing note with pre-sync and post-sync.")
    async def update_note_fields(note_id: int, fields: Dict[str, str]) -> Dict[str, Any]:
        try:
            UpdateNoteFieldsInput(note_id=note_id, fields=fields)
            res = await note_tools.update_note_fields(sync_manager, note_id, fields)
            return res.model_dump()
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # 8. add_tags
    @mcp.tool(description="Add tags to notes with pre-sync and post-sync.")
    async def add_tags(note_ids: List[int], tags: List[str]) -> Dict[str, Any]:
        try:
            parsed = AddTagsInput(note_ids=note_ids, tags=tags)
            res = await note_tools.add_tags(sync_manager, parsed.note_ids, parsed.tags)
            return res.model_dump()
        except (AnkiConnectError, AnkiConnectConnectionError, ValidationError) as e:
            return _tool_error_response(e)

    # Create Starlette app for streamable HTTP with reverse-proxy support
    from mcp.server.transport_security import TransportSecuritySettings

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Healthcheck endpoint (exempt from authentication)
    async def health_endpoint(request: Request) -> JSONResponse:
        anki_healthy = await anki_client.check_health()
        payload = {
            "status": "healthy" if anki_healthy else "degraded",
            "service": "anki-mcp",
            "anki_connected": anki_healthy,
            "maintenance_mode": sync_manager.maintenance_mode,
            "auth_enabled": settings.is_auth_enabled,
        }
        # 503 when degraded so Docker healthchecks and orchestrators can tell
        # a live-but-broken service from a functional one.
        return JSONResponse(payload, status_code=200 if anki_healthy else 503)

    app.add_route("/health", health_endpoint, methods=["GET"])

    # -- Web authentication routes (session-cookie based; interim until the
    # Task 5 auth rewrite unifies the middleware) ---------------------------
    store = UserStore(settings.users_file)

    def get_session_user(request: Request) -> Optional[User]:
        """Resolve the anki_session cookie to an account, or None.

        Signed-cookie validity alone is not enough: the named account must
        still exist, so deleted users lose access immediately even with a
        valid (stateless) cookie.
        """
        session_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_value:
            return None
        username = verify_session(settings.session_secret, session_value)
        if username is None:
            return None
        return next((u for u in store.list_users() if u.username == username), None)

    async def login_page(request: Request) -> Response:
        if get_session_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_page())

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        ip = request.client.host if request.client else ""
        # Brute-force gate first: a locked key is rejected before any password
        # work happens, and the page reveals nothing about the account.
        if not login_rate_limiter.check(username, ip):
            logger.warning("Login rate-limited for %r from %s", username, ip)
            return HTMLResponse(
                _message_page("Too many failed attempts. Please try again later."),
                status_code=429,
            )
        valid = False
        if username:
            # scrypt is CPU-bound; keep it off the event loop.
            valid = await run_in_threadpool(store.verify_password, username, password)
        if valid:
            login_rate_limiter.record_success(username, ip)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE_NAME,
                issue_session(settings.session_secret, username),
                max_age=SESSION_TTL_SECONDS,
                path="/",
                httponly=True,
                samesite="strict",
                secure=secure_cookies_enabled(),
            )
            logger.info("Web session issued for %r from %s", username, ip)
            return response
        login_rate_limiter.record_failure(username, ip)
        # Generic message: identical for unknown usernames and wrong passwords.
        return HTMLResponse(_login_page("Invalid username or password."), status_code=401)

    async def index_page(request: Request) -> Response:
        user = get_session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        return HTMLResponse(_index_page(user.username, issue_csrf(session_value)))

    async def logout_submit(request: Request) -> Response:
        session_value = request.cookies.get(SESSION_COOKIE_NAME)
        user = get_session_user(request)
        if user is None or not session_value:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not verify_csrf(session_value, str(form.get("csrf_token", ""))):
            return HTMLResponse(
                _message_page("Invalid or missing CSRF token."), status_code=403
            )
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        logger.info("Web session ended for %r", user.username)
        return response

    app.add_route("/login", login_page, methods=["GET"])
    app.add_route("/login", login_submit, methods=["POST"])
    app.add_route("/", index_page, methods=["GET"])
    app.add_route("/logout", logout_submit, methods=["POST"])

    # Legacy shared-token auth for the MCP surface, bypassed for the web routes
    # above (which carry their own session-cookie authentication).
    app.add_middleware(ScopedAuthMiddleware, settings=settings)

    # Close the shared AnkiConnect HTTP client on app shutdown.
    # (Starlette 1.x removed add_event_handler; wrap the lifespan instead so the
    # client is closed after the SDK's session-manager shutdown completes.)
    # Capture the client BEFORE defining the wrapper: close must run even if the
    # inner shutdown raises, and a later create_app() must not be able to rebind
    # which client this wrapper closes.
    client_to_close = anki_client
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_client_close(a):
        try:
            async with inner_lifespan(a):
                yield
        finally:
            await client_to_close.close()

    app.router.lifespan_context = lifespan_with_client_close

    return app


def main():
    """Start uvicorn server, refusing unsafe configuration and bootstrapping the first admin."""
    problems = validate_deployment_config(settings)
    if problems:
        for problem in problems:
            logger.error("FATAL: %s", problem)
        raise SystemExit(1)

    # Fail-closed bootstrap: a service with zero user accounts can never
    # authenticate anybody, so it must not come up.
    user_store = UserStore(settings.users_file)
    if not user_store.list_users():
        if settings.admin_username and settings.admin_password:
            user_store.bootstrap_admin(settings.admin_username, settings.admin_password)
            logger.info("Bootstrapped initial admin account %r", settings.admin_username)
        else:
            logger.error(
                "FATAL: no user accounts exist in %s and no bootstrap credentials are set. "
                "Set both ADMIN_USERNAME and ADMIN_PASSWORD to create the initial admin "
                "on first start.",
                settings.users_file,
            )
            raise SystemExit(1)

    logger.info("Starting Anki MCP Service on %s:%d (path: %s)", settings.mcp_host, settings.mcp_port, settings.mcp_path)
    logger.info("Authentication enabled (legacy shared-token middleware active until the auth rewrite)")

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
