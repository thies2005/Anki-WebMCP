"""Main entrypoint for Anki MCP Service."""

import asyncio
from contextlib import asynccontextmanager, suppress
import html
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import uvicorn

from .auth import AuthMiddleware
from .collections import CollectionRegistry
from .config import Settings, get_settings, validate_deployment_config
from .logging_util import setup_logging
from .models import AddNotesInput, AddTagsInput, GetNoteInput, SearchNotesInput, UpdateNoteFieldsInput
from .sync_manager import SyncManager
from .tools import decks as deck_tools
from .tools import notes as note_tools
from .tools import stats as stats_tools
from .tools import sync as sync_tools
from .users import USERNAME_RE, User, UserStore
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
from .webflash import (
    FLASH_COOKIE_NAME,
    FLASH_TTL_SECONDS,
    issue_flash,
    verify_flash,
)

# Interval between idle-collection sweeps (brief: close idle collections every minute).
COLLECTION_IDLE_SWEEP_SECONDS = 60.0

# Globals initialized in factory
settings: Settings = get_settings()
logger: logging.Logger = setup_logging(settings.log_level)
# Per-user account store: MCP tokens and AnkiWeb credentials.
user_store: UserStore = UserStore(settings.users_file)
# Per-user collection registry (Task 3): opened lazily per user; construction does no I/O.
collection_registry: CollectionRegistry = CollectionRegistry(
    data_dir=settings.data_dir,
    idle_timeout_minutes=settings.idle_timeout_minutes,
    max_open_collections=settings.max_open_collections,
    max_concurrent_syncs=settings.max_concurrent_syncs,
    backup_retention=settings.backup_retention,
)
# Write-safety + explicit sync coordination on top of the registry.
sync_manager: SyncManager = SyncManager(collection_registry, user_store, settings)
login_rate_limiter: LoginRateLimiter = LoginRateLimiter()


def _tool_error_response(exc: Exception) -> Dict[str, Any]:
    """Convert expected collection-layer failures into structured tool output.

    Letting these propagate triggers the MCP SDK's generic
    "Error executing tool <name>" mask, hiding the root cause from clients.
    """
    return {
        "success": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _username_from_context(ctx: Context) -> Optional[str]:
    """The authenticated username for THIS MCP request, or None.

    The Starlette middleware stashes it on ``request.state.username`` after
    resolving the caller's per-user token. Starlette backs ``request.state``
    with the shared ASGI ``scope["state"]`` dict, and the MCP SDK frames every
    inbound message with that very Request (``ServerMessageMetadata.request_context``),
    so tool wrappers read it via the injected tool ``Context`` — even though
    the SDK executes tool handlers in its own session task rather than the
    HTTP request's task (verified end-to-end against mcp 2.3.0).
    """
    try:
        request = ctx.request_context.request
    except ValueError:
        return None  # Context not bound to a request (cannot happen for tools/call)
    if request is None:
        return None
    return getattr(request.state, "username", None)


def _unauthorized_tool_response() -> Dict[str, Any]:
    """Structured 401-flavored output for a request with no username context.

    Unreachable while the middleware guards /mcp; kept fail-closed so a user
    context gap can never silently act on the wrong (or a default) collection.
    """
    return {
        "success": False,
        "error": "Unauthorized: no authenticated user on this request",
        "error_type": "UnauthorizedError",
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

# Shared stylesheet for the portal/admin pages: one consistent header, stacked
# section cards, plain HTML tables (wrapped for small screens).
_APP_CSS = """
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0; color: #1a1a1a; }
  main.app { max-width: 52rem; margin: 0 auto; padding: 1rem; }
  header.top { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; align-items: center;
               justify-content: space-between; background: #fff; border: 1px solid #ddd;
               border-radius: 6px; padding: 0.75rem 1rem; margin-bottom: 1rem; }
  header.top .brand { font-weight: 700; }
  header.top nav { display: flex; gap: 0.9rem; align-items: center; flex-wrap: wrap; }
  header.top form { display: inline; margin: 0; }
  section { background: #fff; border: 1px solid #ddd; border-radius: 6px;
            padding: 1rem 1.25rem; margin-bottom: 1rem; }
  h1 { font-size: 1.25rem; margin: 0 0 0.5rem; }
  h2 { font-size: 1.05rem; margin: 0 0 0.5rem; }
  label { display: block; margin: 0.5rem 0 0.2rem; }
  input, select { padding: 0.35rem; margin: 0 0.25rem 0.25rem 0; }
  button { padding: 0.4rem 0.8rem; cursor: pointer; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #e3e3e3; padding: 0.4rem 0.55rem; text-align: left; vertical-align: top; }
  th { background: #fafafa; }
  .tablewrap { overflow-x: auto; }
  td form { display: flex; flex-wrap: wrap; gap: 0.3rem; align-items: center;
            margin: 0 0 0.45rem; }
  .flash { border: 1px solid #b7d7b7; background: #eef7ee; color: #14532d;
           border-radius: 6px; padding: 0.75rem 1rem; margin-bottom: 1rem;
           white-space: pre-wrap; overflow-wrap: anywhere; }
  .flash.success { border-color: #b7d7b7; background: #eef7ee; color: #14532d; }
  .flash.error { border-color: #e5b4b4; background: #fdeeee; color: #b00020; }
  .flash.token { border-color: #b4c7e5; background: #eef2fd; color: #1e3a5f; }
  .flash.token pre { background: #fff; border: 1px solid #ccd; padding: 0.5rem;
                     margin: 0.5rem 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  .muted { color: #666; }
  .ok { color: #14532d; }
  .error { color: #b00020; }
  code { background: #f4f4f4; padding: 0.1rem 0.35rem; border-radius: 4px; overflow-wrap: anywhere; }
  pre.syncout { background: #fafafa; border: 1px solid #e3e3e3; border-radius: 4px;
                padding: 0.6rem; white-space: pre-wrap; overflow-wrap: anywhere; }
"""


def _page(title: str, body: str, css: str = _PAGE_CSS, main_class: str = "") -> str:
    class_attr = f' class="{main_class}"' if main_class else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{css}</style>
</head>
<body>
<main{class_attr}>
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


def _message_page(
    message: str, back_href: str = "/login", back_label: str = "Back to sign in"
) -> str:
    return _page(
        "Anki MCP",
        f'<p>{html.escape(message)}</p>\n<p><a href="{html.escape(back_href)}">{html.escape(back_label)}</a></p>',
    )


def _forbidden_page() -> str:
    return _page(
        "Anki MCP - Forbidden",
        '<p class="error">Admin access required.</p>'
        '<p><a href="/">Back to your portal</a></p>',
    )


def _mask_token(raw_token: Optional[str]) -> str:
    """Masked token display: first 4 + last 4 chars of the RAW token (``aB3d…9zQ``)."""
    if not raw_token:
        return ""
    if len(raw_token) < 8:
        return "…"
    return f"{raw_token[:4]}…{raw_token[-4:]}"


def _mask_email(email: str) -> str:
    """Masked AnkiWeb email: ``user@example.com`` -> ``u***@example.com``.

    The password is never displayed anywhere.
    """
    local, sep, domain = (email or "").partition("@")
    if not sep:
        return ""
    return f"{local[:1]}***@{domain}"


def _app_header(user: User, csrf_token: str) -> str:
    """Shared portal/admin header: brand, identity, portal/admin links, logout."""
    admin_link = '<a href="/admin">Admin</a>' if user.role == "admin" else ""
    return f"""<header class="top">
  <span class="brand">Anki MCP</span>
  <nav>
    <span>Logged in as <strong>{html.escape(user.username)}</strong></span>
    <a href="/">Portal</a>
    {admin_link}
    <form method="post" action="/logout">
      <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
      <button type="submit">Log out</button>
    </form>
  </nav>
</header>"""


def _flash_html(flash: Optional[Tuple[str, str]]) -> str:
    """Render a consumed flash message; ``token`` kind gets the one-time reveal."""
    if flash is None:
        return ""
    kind, message = flash
    if kind == "token":
        return (
            '<div class="flash token"><strong>'
            "Copy this now &mdash; it will not be shown again.</strong>"
            f'<pre id="flash-token">{html.escape(message)}</pre>'
            '<button type="button" onclick="navigator.clipboard.writeText('
            "document.getElementById('flash-token').textContent)\">Copy to clipboard</button>"
            "</div>"
        )
    css_class = "flash" if kind == "info" else f"flash {kind}"
    return f'<div class="{css_class}">{html.escape(message)}</div>'


def _collect_deck_stats(col) -> Dict[str, Any]:
    """Deck stats for the portal table; runs inside registry.run on a worker thread.

    Deliberately cheap and reliable: ONE grouped query over the cards table for
    per-deck note/card/due counts, plus the scheduler's overall due counts.
    Due semantics per card queue: 1 (learning) stores an epoch-seconds
    timestamp; 2 (review) and 3 (day-learn) store a day ordinal — each compared
    against the scheduler's own clock.
    """
    now_ts = int(time.time())
    today = col.sched.today
    rows = col.db.all(
        "SELECT did, COUNT(DISTINCT nid), COUNT(), "
        "SUM(CASE WHEN queue = 1 AND due <= ? THEN 1 "
        "WHEN queue IN (2, 3) AND due <= ? THEN 1 ELSE 0 END) "
        "FROM cards GROUP BY did",
        now_ts,
        today,
    )
    counts_by_did = {row[0]: row for row in rows}
    decks = [
        {
            "name": deck.name,
            "notes": counts_by_did.get(deck.id, (0, 0, 0, 0))[1],
            "cards": counts_by_did.get(deck.id, (0, 0, 0, 0))[2],
            "due": counts_by_did.get(deck.id, (0, 0, 0, 0))[3],
        }
        for deck in sorted(col.decks.all_names_and_ids(), key=lambda d: d.name.lower())
    ]
    new_count, learn_count, review_count = col.sched.counts()
    return {
        "decks": decks,
        "due": {"new": int(new_count), "learn": int(learn_count), "review": int(review_count)},
    }


def _deck_section(stats: Optional[Dict[str, Any]]) -> str:
    if stats is None:
        return (
            '<section><h2>Decks</h2>'
            '<p class="muted">Deck statistics are unavailable right now.</p></section>'
        )
    rows = "".join(
        f"<tr><td>{html.escape(str(deck['name']))}</td>"
        f"<td>{deck['notes']}</td><td>{deck['cards']}</td><td>{deck['due']}</td></tr>"
        for deck in stats["decks"]
    )
    if not rows:
        rows = '<tr><td colspan="4">No decks yet.</td></tr>'
    due = stats["due"]
    return f"""<section>
  <h2>Decks</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Deck</th><th>Notes</th><th>Cards</th><th>Due</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <p class="muted">Due now: {due['new']} new, {due['learn']} learning, {due['review']} review.</p>
</section>"""


def _portal_page(
    user: User,
    csrf_token: str,
    flash: Optional[Tuple[str, str]],
    stats: Optional[Dict[str, Any]],
    token_mask: str,
) -> str:
    if token_mask:
        token_state = f"<p>Current token: <code>{html.escape(token_mask)}</code></p>"
    else:
        # Legacy record without token_plaintext: the raw value was never stored.
        token_state = '<p class="muted">Token not viewable &mdash; regenerate to view.</p>'
    credentials_state = (
        f"<p>Saved for <code>{html.escape(_mask_email(user.ankiweb_email))}</code>.</p>"
        if user.ankiweb_email
        else '<p class="muted">Not set.</p>'
    )
    body = f"""{_app_header(user, csrf_token)}
{_flash_html(flash)}
<section>
  <h2>MCP token</h2>
  <p>Authenticate MCP clients with this account's token.</p>
  {token_state}
  <form method="post" action="/portal/regenerate-token">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <button type="submit">Regenerate token</button>
  </form>
</section>
{_deck_section(stats)}
<section>
  <h2>AnkiWeb credentials</h2>
  <p>Needed to synchronize with AnkiWeb. The password is stored for the sync
  service and never displayed.</p>
  {credentials_state}
  <form method="post" action="/portal/credentials">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <label for="ankiweb-email">AnkiWeb email</label>
    <input id="ankiweb-email" name="email" type="email" autocomplete="off">
    <label for="ankiweb-password">AnkiWeb password</label>
    <input id="ankiweb-password" name="password" type="password" autocomplete="new-password">
    <button type="submit">Save credentials</button>
  </form>
</section>
<section>
  <h2>Synchronize</h2>
  <p>Backs up this account's collection locally, then syncs it with AnkiWeb.</p>
  <form method="post" action="/portal/sync">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <button type="submit">Sync now</button>
  </form>
</section>"""
    return _page("Anki MCP - Portal", body, css=_APP_CSS, main_class="app")


def _sync_result_page(result: Dict[str, Any]) -> str:
    """Render the sync_user result dict on a dedicated page.

    A dedicated page (not a flash cookie) because sync output/error text is
    unbounded and would not fit the cookie budget.
    """
    if result.get("success"):
        detail = (
            '<p class="ok">Sync succeeded.</p>'
            f'<pre class="syncout">{html.escape(str(result.get("output", "")))}</pre>'
        )
    else:
        unknown_note = (
            " The transfer may or may not have been applied &mdash; check AnkiWeb before retrying."
            if result.get("outcome_unknown")
            else ""
        )
        detail = (
            f'<p class="error">Sync failed ({html.escape(str(result.get("error_type", "Error")))}): '
            f'{html.escape(str(result.get("error", "unknown error")))}.{unknown_note}</p>'
        )
    return _page(
        "Anki MCP - Sync result",
        f"<h1>Sync result</h1>\n{detail}\n<p><a href=\"/\">Back to portal</a></p>",
        css=_APP_CSS,
        main_class="app",
    )


def _admin_page(
    user: User, csrf_token: str, flash: Optional[Tuple[str, str]], accounts: List[User]
) -> str:
    """Admin console: create-user form plus one action cell per account.

    The table shows no secrets: credentials are shown as a masked email and the
    token column only says whether a token exists.
    """
    hidden = (
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
        '<input type="hidden" name="username" value="{name}">'
    )
    rows: List[str] = []
    for account in sorted(accounts, key=lambda a: a.username):
        name = html.escape(account.username)
        created = (
            time.strftime("%Y-%m-%d", time.localtime(account.created_at))
            if account.created_at > 0
            else "&mdash;"
        )
        creds = (
            f"set ({html.escape(_mask_email(account.ankiweb_email))})"
            if account.ankiweb_email
            else "not set"
        )
        role_options = "".join(
            f'<option value="{role}"{" selected" if role == account.role else ""}>{role}</option>'
            for role in ("user", "admin")
        )
        forms = {
            "set-role": f"""<form method="post" action="/admin/set-role">{hidden.format(name=name)}
      <select name="role" aria-label="New role for {name}">{role_options}</select>
      <button type="submit">Set role</button>
    </form>""",
            "reset-password": f"""<form method="post" action="/admin/reset-password">{hidden.format(name=name)}
      <input name="new_password" type="password" placeholder="new password" required aria-label="New password for {name}">
      <button type="submit">Reset password</button>
    </form>""",
            "regenerate-token": f"""<form method="post" action="/admin/regenerate-token">{hidden.format(name=name)}
      <button type="submit">Regenerate token</button>
    </form>""",
            "delete-user": f"""<form method="post" action="/admin/delete-user">{hidden.format(name=name)}
      <input name="confirm_username" placeholder="type username to confirm" aria-label="Confirm delete of {name}">
      <button type="submit">Delete</button>
    </form>""",
        }
        rows.append(
            f"""<tr>
  <td>{name}</td>
  <td>{html.escape(account.role)}</td>
  <td>{created}</td>
  <td>{creds}</td>
  <td>{"yes" if account.token_hash else "no"}</td>
  <td>{forms['set-role']}{forms['reset-password']}{forms['regenerate-token']}{forms['delete-user']}</td>
</tr>"""
        )
    body = f"""{_app_header(user, csrf_token)}
{_flash_html(flash)}
<section>
  <h2>Create user</h2>
  <form method="post" action="/admin/create-user">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <label for="new-username">Username</label>
    <input id="new-username" name="username" required pattern="[a-z0-9_-]{{1,32}}">
    <label for="new-password">Password</label>
    <input id="new-password" name="password" type="password" required>
    <label for="new-role">Role</label>
    <select id="new-role" name="role"><option value="user">user</option><option value="admin">admin</option></select>
    <button type="submit">Create user</button>
  </form>
  <p class="muted">The new account's MCP token is shown once after creation &mdash; copy it for the user.</p>
</section>
<section>
  <h2>Users</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Username</th><th>Role</th><th>Created</th><th>AnkiWeb</th><th>Token</th><th>Actions</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="6">No users.</td></tr>'}</tbody>
  </table></div>
  <p class="muted">Deleting a user requires typing their username to confirm; their data
  directory is moved under <code>deleted/</code>, never removed.</p>
</section>"""
    return _page("Anki MCP - Admin", body, css=_APP_CSS, main_class="app")


def _archive_user_data(data_dir: str, username: str) -> Optional[str]:
    """Move ``<data_dir>/users/<name>`` to ``<data_dir>/deleted/<name>-<unix_ts>``.

    Deletion never removes files: the whole directory is MOVED aside so the
    account's collection stays recoverable (``deleted/`` is created as needed;
    a same-second collision gets a numeric suffix). The per-user ledger
    directory (``<data_dir>/ledgers/<name>``) is intentionally left in place:
    it holds only idempotency keys and is harmless if the username is ever
    recreated.

    Returns the destination path, or None when the user had no data directory.
    Raises ValueError for a non-path-safe username and OSError when the rename
    fails, so callers can abort the account deletion instead of destroying a
    record whose data could not be archived.
    """
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Invalid username {username!r}")
    src = os.path.join(data_dir, "users", username)
    if not os.path.isdir(src):
        return None
    deleted_root = os.path.join(data_dir, "deleted")
    os.makedirs(deleted_root, exist_ok=True)
    base = f"{username}-{int(time.time())}"
    dest = os.path.join(deleted_root, base)
    suffix = 1
    while os.path.exists(dest):
        dest = os.path.join(deleted_root, f"{base}-{suffix}")
        suffix += 1
    os.rename(src, dest)
    return dest


def create_app(custom_settings: Optional[Settings] = None):
    """Create and configure the MCP Starlette ASGI application."""
    global settings, logger, user_store, sync_manager, login_rate_limiter, collection_registry
    if custom_settings:
        settings = custom_settings
        logger = setup_logging(settings.log_level)
        user_store = UserStore(settings.users_file)
        collection_registry = CollectionRegistry(
            data_dir=settings.data_dir,
            idle_timeout_minutes=settings.idle_timeout_minutes,
            max_open_collections=settings.max_open_collections,
            max_concurrent_syncs=settings.max_concurrent_syncs,
            backup_retention=settings.backup_retention,
        )
        sync_manager = SyncManager(collection_registry, user_store, settings)
    login_rate_limiter = LoginRateLimiter()  # fresh per app: lockout state must not leak across tests/restarts

    # Captured AFTER the possible custom_settings rebind above, so the tool
    # wrappers always use the registry/manager THIS app owns — never a later
    # create_app()'s (same pattern as portal_registry below).
    tool_registry = collection_registry
    tool_sync_manager = sync_manager
    # Health-probe handles, captured like the tool wrappers: THIS app's store
    # and data dir, never a later create_app()'s.
    health_store = user_store
    health_data_dir = settings.data_dir

    mcp = MCPServer(
        name="anki-mcp",
        version="1.0.0",
        description="Self-hosted Anki Desktop Model Context Protocol (MCP) server",
    )

    # 1. list_decks
    @mcp.tool(description="List all available decks and their IDs in the Anki collection.")
    async def list_decks(ctx: Context) -> Dict[str, Any]:
        username = _username_from_context(ctx)
        if username is None:
            return _unauthorized_tool_response()
        try:
            return await deck_tools.list_decks(tool_registry, username)
        except Exception as e:
            return _tool_error_response(e)

    # 2. search_notes
    @mcp.tool(description="Search notes in the collection by Anki query with bounded pagination.")
    async def search_notes(query: str, limit: int = 50, offset: int = 0, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            SearchNotesInput(query=query, limit=limit, offset=offset)
            return await note_tools.search_notes(tool_registry, username, query, limit, offset)
        except Exception as e:
            return _tool_error_response(e)

    # 3. get_note
    @mcp.tool(description="Retrieve details and fields for a specific note by ID.")
    async def get_note(note_id: int, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            GetNoteInput(note_id=note_id)
            return await note_tools.get_note(tool_registry, username, note_id)
        except Exception as e:
            return _tool_error_response(e)

    # 4. get_collection_stats
    @mcp.tool(description="Get reliably supported collection statistics and deck summaries.")
    async def get_collection_stats(ctx: Context) -> Dict[str, Any]:
        username = _username_from_context(ctx)
        if username is None:
            return _unauthorized_tool_response()
        try:
            return await stats_tools.get_collection_stats(tool_registry, username)
        except Exception as e:
            return _tool_error_response(e)

    # 5. sync_collection
    @mcp.tool(description="Trigger explicit synchronization with official AnkiWeb.")
    async def sync_collection(ctx: Context) -> Dict[str, Any]:
        username = _username_from_context(ctx)
        if username is None:
            return _unauthorized_tool_response()
        try:
            return await sync_tools.sync_collection(tool_sync_manager, username)
        except Exception as e:
            return _tool_error_response(e)

    # 6. add_notes
    @mcp.tool(
        description="Add notes to the collection with duplicate prevention and per-user idempotency."
    )
    async def add_notes(
        notes: List[Dict[str, Any]],
        idempotency_key: Optional[str] = None,
        allow_duplicates: bool = False,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            # Validate the full batch (non-empty, <=100 notes, field shapes) up front
            parsed_input = AddNotesInput(notes=notes, idempotency_key=idempotency_key, allow_duplicates=allow_duplicates)
            res = await note_tools.add_notes(
                tool_sync_manager, tool_registry, username,
                parsed_input.notes, parsed_input.idempotency_key, parsed_input.allow_duplicates,
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 7. update_note_fields
    @mcp.tool(description="Update fields on an existing note.")
    async def update_note_fields(note_id: int, fields: Dict[str, str], ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            UpdateNoteFieldsInput(note_id=note_id, fields=fields)
            res = await note_tools.update_note_fields(tool_sync_manager, tool_registry, username, note_id, fields)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 8. add_tags
    @mcp.tool(description="Add tags to notes.")
    async def add_tags(note_ids: List[int], tags: List[str], ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = AddTagsInput(note_ids=note_ids, tags=tags)
            res = await note_tools.add_tags(tool_sync_manager, tool_registry, username, parsed.note_ids, parsed.tags)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # Create Starlette app for streamable HTTP with reverse-proxy support
    from mcp.server.transport_security import TransportSecuritySettings

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Healthcheck endpoint (exempt from authentication)
    async def health_endpoint(request: Request) -> JSONResponse:
        # Real but CHEAP probes — this runs per healthcheck (Docker/K8s hit it
        # every few seconds), so no collection I/O and no temp files:
        #   (a) the users store must be readable: a corrupt/dead store breaks
        #       authentication for every account (list_users is an in-memory
        #       read under the store's lock);
        #   (b) the data dir must be writable: per-user collections, ledgers,
        #       and backups all live under it (os.access is the cheap,
        #       allocation-free probe; a missing dir reports not-writable).
        # Collections are served in-process by the registry, so there is no
        # remote AnkiConnect client to probe: anki_connected stays a compat key
        # that is always true.
        payload = {
            "status": "healthy",
            "service": "anki-mcp",
            "anki_connected": True,
            "maintenance_mode": tool_sync_manager.maintenance_mode,
            "auth_enabled": True,  # per-user token auth is unconditional now
        }
        problems: List[str] = []
        try:
            health_store.list_users()
        except Exception as e:
            logger.warning("Health probe: user store unavailable: %s", e)
            problems.append("user store unavailable")
        if not os.access(health_data_dir, os.W_OK):
            logger.warning("Health probe: data dir %r is missing or not writable", health_data_dir)
            problems.append("data directory not writable")
        if problems:
            payload["status"] = "degraded"
            return JSONResponse(payload, status_code=503)
        return JSONResponse(payload, status_code=200)

    app.add_route("/health", health_endpoint, methods=["GET"])

    # -- Web authentication routes (session-cookie based) ---------------------
    store = user_store

    # Captured AFTER the possible custom_settings rebind above, so the portal
    # handlers below always use the registry this app owns — never a later
    # create_app()'s (same pattern as registry_to_close further down).
    portal_registry = collection_registry

    def get_session_user(request: Request) -> Optional[User]:
        """Resolve the anki_session cookie to an account, or None.

        Signed-cookie validity alone is not enough: the named account must
        still exist, so deleted users lose access immediately even with a
        valid (stateless) cookie. Lookup goes through UserStore.get (a copy),
        not a list scan.
        """
        session_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_value:
            return None
        username = verify_session(settings.session_secret, session_value)
        if username is None:
            return None
        return store.get(username)

    # Shared guards and flash plumbing for the session-cookie routes below.

    def _csrf_ok(session_value: Optional[str], form) -> bool:
        return verify_csrf(session_value or "", str(form.get("csrf_token", "")))

    def _require_session(request: Request) -> Tuple[Optional[User], Optional[Response]]:
        user = get_session_user(request)
        if user is None:
            return None, RedirectResponse("/login", status_code=303)
        return user, None

    def _require_admin(request: Request) -> Tuple[Optional[User], Optional[Response]]:
        user = get_session_user(request)
        if user is None:
            return None, RedirectResponse("/login", status_code=303)
        if user.role != "admin":
            return None, HTMLResponse(_forbidden_page(), status_code=403)
        return user, None

    def _peek_flash(request: Request) -> Optional[Tuple[str, str]]:
        raw = request.cookies.get(FLASH_COOKIE_NAME)
        return verify_flash(settings.session_secret, raw) if raw else None

    def _clear_flash(response: Response) -> None:
        response.delete_cookie(FLASH_COOKIE_NAME, path="/", secure=secure_cookies_enabled())

    def _redirect_with_flash(url: str, message: str, kind: str = "info") -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLASH_COOKIE_NAME,
            issue_flash(settings.session_secret, message, kind),
            max_age=FLASH_TTL_SECONDS,
            path="/",
            httponly=True,
            samesite="strict",
            secure=secure_cookies_enabled(),
        )
        return response

    def _bad_request(message: str, back_href: str = "/admin", back_label: str = "Back to admin") -> HTMLResponse:
        return HTMLResponse(_message_page(message, back_href, back_label), status_code=400)

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
        user, err = _require_session(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_token = issue_csrf(session_value)
        flash = _peek_flash(request)
        token_mask = _mask_token(await run_in_threadpool(store.peek_token, user.username))
        # A stats failure must not take the whole portal down.
        stats: Optional[Dict[str, Any]] = None
        try:
            stats = await portal_registry.run(user.username, _collect_deck_stats)
        except Exception:
            logger.exception("Deck stats failed for %r", user.username)
        response = HTMLResponse(_portal_page(user, csrf_token, flash, stats, token_mask))
        if request.cookies.get(FLASH_COOKIE_NAME):
            _clear_flash(response)  # consumed: render at most once
        return response

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
        # Mirror the set-cookie Secure flag so the deletion cookie matches in
        # FORCE_SECURE_COOKIES deployments too.
        response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=secure_cookies_enabled())
        logger.info("Web session ended for %r", user.username)
        return response

    # -- Portal actions (session required, CSRF-protected) --------------------

    async def portal_regenerate_token(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        try:
            raw_token = await run_in_threadpool(store.regenerate_token, user.username)
        except ValueError as e:
            # Account deleted mid-request; mirror the admin handlers' 400.
            return _bad_request(f"Could not regenerate token: {e}", back_href="/", back_label="Back to portal")
        logger.info("Portal token regeneration for %r", user.username)
        # One-time reveal via the flash cookie; only the sha256 ever authenticates.
        return _redirect_with_flash("/", raw_token, kind="token")

    async def portal_set_credentials(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        if not email or not password:
            return _redirect_with_flash(
                "/", "Both AnkiWeb email and password are required.", kind="error"
            )
        await run_in_threadpool(store.set_ankiweb_credentials, user.username, email, password)
        logger.info("Portal AnkiWeb credentials update for %r", user.username)
        return _redirect_with_flash("/", "AnkiWeb credentials saved.", kind="success")

    async def portal_sync_now(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        if not user.ankiweb_email or not user.ankiweb_password:
            # Inline notice, and no sync call (sync is opt-in per user).
            return _redirect_with_flash(
                "/",
                "AnkiWeb credentials are not set — save them below before syncing.",
                kind="error",
            )
        # The result dict is rendered on a dedicated page: sync output is
        # unbounded and would not fit the flash cookie.
        try:
            result = await portal_registry.sync_user(
                user.username, user.ankiweb_email, user.ankiweb_password
            )
        except ValueError as e:
            # Account deleted mid-request; mirror the admin handlers' 400.
            return _bad_request(f"Could not sync: {e}", back_href="/", back_label="Back to portal")
        logger.info("Portal sync for %r: success=%s", user.username, result.get("success"))
        return HTMLResponse(_sync_result_page(result))

    # -- Admin console (session + admin role, CSRF-protected) ------------------

    async def admin_page(request: Request) -> Response:
        user, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_token = issue_csrf(session_value)
        flash = _peek_flash(request)
        accounts = await run_in_threadpool(store.list_users)
        response = HTMLResponse(_admin_page(user, csrf_token, flash, accounts))
        if request.cookies.get(FLASH_COOKIE_NAME):
            _clear_flash(response)  # consumed: render at most once
        return response

    async def admin_create_user(request: Request) -> Response:
        admin, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        role = str(form.get("role", "user"))
        if not password:
            return _bad_request("Password must not be empty.")
        try:
            _new_user, raw_token = await run_in_threadpool(store.create_user, username, password, role)
        except ValueError as e:
            return _bad_request(f"Could not create user: {e}")
        logger.info("Admin %r created account %r", admin.username, username)
        # One-time token reveal so the admin can hand it to the new user.
        return _redirect_with_flash("/admin", raw_token, kind="token")

    async def admin_reset_password(request: Request) -> Response:
        admin, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        new_password = str(form.get("new_password", ""))
        if not new_password:
            return _bad_request("New password must not be empty.")
        try:
            await run_in_threadpool(store.set_password, username, new_password)
        except ValueError as e:
            return _bad_request(f"Could not reset password: {e}")
        logger.info("Admin %r reset password of %r", admin.username, username)
        return _redirect_with_flash("/admin", f"Password updated for {username}.", kind="success")

    async def admin_regenerate_token(request: Request) -> Response:
        admin, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        try:
            raw_token = await run_in_threadpool(store.regenerate_token, username)
        except ValueError as e:
            return _bad_request(f"Could not regenerate token: {e}")
        logger.info("Admin %r regenerated token of %r", admin.username, username)
        return _redirect_with_flash("/admin", raw_token, kind="token")

    async def admin_set_role(request: Request) -> Response:
        admin, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        role = str(form.get("role", ""))
        try:
            await run_in_threadpool(store.set_role, username, role)
        except ValueError as e:
            return _bad_request(f"Could not set role: {e}")
        logger.info("Admin %r set role of %r to %r", admin.username, username, role)
        return _redirect_with_flash("/admin", f"Role of {username} set to {role}.", kind="success")

    async def admin_delete_user(request: Request) -> Response:
        admin, err = _require_admin(request)
        if err:
            return err
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        confirm = str(form.get("confirm_username", "")).strip()
        # Form-level confirmation: the exact username must be typed to delete.
        if not username or confirm != username:
            return _bad_request(
                "Deletion not confirmed: type the account's username exactly to confirm."
            )
        if store.get(username) is None:
            return _bad_request(f"Unknown user: {username}")
        try:
            # Archive FIRST: if the rename fails, the account record stays put
            # rather than losing a user whose data could not be moved aside.
            archive_path = _archive_user_data(settings.data_dir, username)
        except (OSError, ValueError) as e:
            logger.error("Failed to archive data directory for %r: %s", username, e)
            return _bad_request(f"Deletion failed while archiving data: {e}")
        await run_in_threadpool(store.delete_user, username)
        logger.info(
            "Admin %r deleted account %r (data archived at %s)",
            admin.username,
            username,
            archive_path or "no data directory",
        )
        notice = f"Deleted {username}." + (" Data directory moved to deleted/." if archive_path else "")
        return _redirect_with_flash("/admin", notice, kind="success")

    app.add_route("/login", login_page, methods=["GET"])
    app.add_route("/login", login_submit, methods=["POST"])
    app.add_route("/", index_page, methods=["GET"])
    app.add_route("/logout", logout_submit, methods=["POST"])
    app.add_route("/portal/regenerate-token", portal_regenerate_token, methods=["POST"])
    app.add_route("/portal/credentials", portal_set_credentials, methods=["POST"])
    app.add_route("/portal/sync", portal_sync_now, methods=["POST"])
    app.add_route("/admin", admin_page, methods=["GET"])
    app.add_route("/admin/create-user", admin_create_user, methods=["POST"])
    app.add_route("/admin/reset-password", admin_reset_password, methods=["POST"])
    app.add_route("/admin/regenerate-token", admin_regenerate_token, methods=["POST"])
    app.add_route("/admin/set-role", admin_set_role, methods=["POST"])
    app.add_route("/admin/delete-user", admin_delete_user, methods=["POST"])

    # Per-user token auth for the MCP surface; the web routes above are in the
    # middleware's explicit exempt allowlist (they carry their own session-
    # cookie authentication) along with /health and OPTIONS.
    app.add_middleware(AuthMiddleware, store=store, settings=settings)

    # Introspection handles for tests/ops: the registry and store THIS app owns.
    app.state.anki_registry = collection_registry
    app.state.anki_store = store

    # Per-user collection lifecycle (Task 3): a background task closes
    # collections idle beyond idle_timeout_minutes every minute, and shutdown
    # closes every open collection. Capture the registry BEFORE defining the
    # wrapper so a later create_app() cannot rebind which registry this
    # lifespan owns. (Starlette 1.x removed add_event_handler; wrap the
    # lifespan instead so cleanup runs after the SDK's session-manager
    # shutdown completes.)
    registry_to_close = collection_registry
    inner_lifespan = app.router.lifespan_context

    async def _close_idle_collections_forever() -> None:
        while True:
            await asyncio.sleep(COLLECTION_IDLE_SWEEP_SECONDS)
            try:
                await registry_to_close.close_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Idle collection sweep failed")

    @asynccontextmanager
    async def lifespan_with_registry_close(a):
        idle_sweep_task = asyncio.create_task(_close_idle_collections_forever())
        try:
            async with inner_lifespan(a):
                yield
        finally:
            idle_sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await idle_sweep_task
            await registry_to_close.close_all()

    app.router.lifespan_context = lifespan_with_registry_close

    return app


def _bootstrap_first_admin(app_settings: Settings) -> None:
    """Fail-closed first-boot admin bootstrap, operating on the module-global store.

    MUST rebind the module global (not build a local UserStore): create_app()
    captures ``user_store`` when called without custom_settings — the production
    path — so a bootstrap through a local instance would leave the serving app
    with the import-time (empty on first boot) store. Regression-tested by
    test_first_boot_bootstrap_visible_to_serving_app.
    """
    global user_store
    user_store = UserStore(app_settings.users_file)
    if not user_store.list_users():
        if app_settings.admin_username and app_settings.admin_password:
            try:
                user_store.bootstrap_admin(app_settings.admin_username, app_settings.admin_password)
            except ValueError as e:
                logger.error("FATAL: cannot bootstrap the initial admin account: %s", e)
                raise SystemExit(1)
            logger.info("Bootstrapped initial admin account %r", app_settings.admin_username)
        else:
            logger.error(
                "FATAL: no user accounts exist in %s and no bootstrap credentials are set. "
                "Set both ADMIN_USERNAME and ADMIN_PASSWORD to create the initial admin "
                "on first start.",
                app_settings.users_file,
            )
            raise SystemExit(1)


def main():
    """Start uvicorn server, refusing unsafe configuration and bootstrapping the first admin."""
    problems = validate_deployment_config(settings)
    if problems:
        for problem in problems:
            logger.error("FATAL: %s", problem)
        raise SystemExit(1)

    # Fail-closed bootstrap: a service with zero user accounts can never
    # authenticate anybody, so it must not come up.
    _bootstrap_first_admin(settings)

    logger.info("Starting Anki MCP Service on %s:%d (path: %s)", settings.mcp_host, settings.mcp_port, settings.mcp_path)
    logger.info("Authentication enabled (per-user MCP tokens via /data/users.json)")

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
