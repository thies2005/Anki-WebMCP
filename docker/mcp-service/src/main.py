"""Main entrypoint for Anki MCP Service."""

import asyncio
import anyio
from contextlib import asynccontextmanager, suppress
import hashlib
import html
import hmac
import logging
import os
import secrets
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
import uvicorn

from .auth import AuthMiddleware
from .collections import CollectionRegistry
from .config import Settings, get_settings, load_or_create_session_secret, validate_deployment_config
from .logging_util import setup_logging
from .models import (
    AddNotesInput,
    AddTagsInput,
    AnswerCardInput,
    CreateDeckInput,
    CreateNoteTypeInput,
    DeleteDeckInput,
    DeleteNotesInput,
    FindCardsInput,
    GetCardInput,
    GetMediaInput,
    GetNoteInput,
    ListMediaInput,
    MoveCardsInput,
    RemoveTagsInput,
    RenameDeckInput,
    ReviewQueueInput,
    SearchNotesInput,
    SetDueDateInput,
    StoreMediaInput,
    SuspendCardsInput,
    TemplateInput,
    UpdateNoteFieldsInput,
)
from .oauth import OAuthStore, add_oauth_routes
from .sync_manager import SyncManager
from .tools import cards as cards_tools
from .tools import decks as deck_tools
from .tools import media as media_tools
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
    session_issued_before,
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

# Newest snapshots shown in the portal's Version history table; older ones are
# summarised in one muted "+N older" line (they stay downloadable on disk).
BACKUP_LIST_LIMIT = 20

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


# --- Pre-login CSRF binding (login-CSRF fix) --------------------------------
#
# POST /login is the one state-changing route OUTSIDE the session-bound CSRF
# sweep (verify_csrf needs a session, which a pre-login visitor has none of),
# so it carries its own binding: GET /login mints a random nonce plus an HMAC
# over it and hands BOTH to the browser — the nonce|MAC value as a short-lived
# cookie scoped to /login and, embedded in the form, the same value as a
# hidden field. POST /login must return the identical value via cookie AND
# field, and the MAC must verify under a key derived from SESSION_SECRET under
# the ``anki-mcp/prelogin/v1`` domain label. A cross-site page can neither
# read the cookie (SameSite=strict, HttpOnly) nor know the field value, so a
# forged login POST fails closed. Deliberately NOT an Origin/Referer
# comparison: that breaks behind TLS-terminating proxies and is unnecessary
# here — the bearer token the attacker cannot obtain is the cookie itself.
#
# Kept in main.py (not webauth.py) as a self-contained app-layer concern; the
# helpers mirror webauth's pure-function style so they unit test the same way.
PRELOGIN_COOKIE_NAME = "anki_prelogin"
PRELOGIN_FIELD_NAME = "prelogin_token"
PRELOGIN_KEY_DOMAIN = b"anki-mcp/prelogin/v1"
PRELOGIN_TTL_SECONDS = 600  # long enough to read the form, short by design
PRELOGIN_NONCE_BYTES = 32

# Cap on form POST bodies for the web routes: read from the declared
# Content-Length BEFORE starlette spools any body bytes into memory/disk.
MAX_FORM_BODY_BYTES = 64 * 1024

# Transport-level concurrency cap handed to uvicorn (bounds half-open
# connections plus in-flight requests per worker; uvicorn's default is None).
UVICORN_LIMIT_CONCURRENCY = 32

# Dedicated cap on pre-auth password verification: unauthenticated callers
# cannot occupy more than this many of the shared worker tokens with scrypt
# burns, whatever uvicorn's overall ceiling is (audit remediation half 2).
login_verify_capacity = anyio.CapacityLimiter(4)


def _prelogin_key(session_secret: str) -> bytes:
    """Derive the prelogin-binding key, domain-separated from every other use of the secret."""
    return hmac.new(session_secret.encode("utf-8"), PRELOGIN_KEY_DOMAIN, hashlib.sha256).digest()


def _issue_prelogin(session_secret: str) -> str:
    """Mint a ``nonce|HMAC(nonce)`` pre-login CSRF token (cookie + form field)."""
    nonce = secrets.token_urlsafe(PRELOGIN_NONCE_BYTES)
    mac = hmac.new(_prelogin_key(session_secret), nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{nonce}|{mac}"


def _verify_prelogin(session_secret: str, value: str) -> bool:
    """Check a pre-login token's MAC; any malformed input verifies to False."""
    if not session_secret or not isinstance(value, str):
        return False
    nonce, sep, mac = value.partition("|")
    if not sep or not nonce or not mac or len(nonce) > 256 or len(mac) != 64:
        return False
    try:
        expected = hmac.new(
            _prelogin_key(session_secret), nonce.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(
            expected.encode("ascii"), mac.encode("ascii")
        )
    except (UnicodeEncodeError, TypeError):
        # Attacker-controlled segments may be non-ASCII; that can never match
        # our ASCII-only format, so fail closed without raising.
        return False


def _body_too_large(request: Request) -> bool:
    """True when the request body must be refused before parsing.

    Reads headers only and touches no body bytes, so an oversized form POST is
    refused before starlette's form parser allocates for it (the parser caps
    are per-field, not per-body, and file parts spool to disk unchecked).
    A chunked body (no Content-Length) is refused outright: browsers never
    chunk form posts, and starlette's per-field caps do not bound multipart
    file parts, so admitting chunked would reopen the pre-auth spool path.
    """
    if request.headers.get("transfer-encoding"):
        return True
    raw = request.headers.get("content-length", "")
    try:
        return int(raw) > MAX_FORM_BODY_BYTES
    except ValueError:
        # Unparseable/absent without transfer-encoding: nothing sane to read.
        return raw != ""


def _uvicorn_run_kwargs(settings: Settings) -> Dict[str, Any]:
    """Keyword arguments for uvicorn.run in main(), testable without a server.

    Keeping this in one place lets tests pin the availability-relevant kwargs
    (limit_concurrency) by source inspection instead of booting uvicorn. The
    concurrency ceiling stays above twice the MCP session cap because every
    stateful MCP session holds a stream that also counts toward it.
    """
    return {
        "host": settings.mcp_host,
        "port": settings.mcp_port,
        "log_level": settings.log_level.lower(),
        "access_log": False,
        "limit_concurrency": max(UVICORN_LIMIT_CONCURRENCY, 2 * settings.mcp_max_sessions),
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
  .decknode { border: 1px solid #e3e3e3; border-radius: 6px; margin: 0.25rem 0;
              padding: 0.15rem 0.4rem; background: #fafafa; }
  .decknode summary { cursor: pointer; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: baseline; }
  .decknode .deckname { font-weight: 600; flex: 1; }
  .decknode .counts { color: #666; font-size: 0.9rem; }
  .decknode .decknode { margin-left: 1.1rem; }
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


def _safe_next(candidate: object) -> Optional[str]:
    """A validated same-origin return path for post-login redirects, or None.

    Only site-absolute paths qualify ("/…", never "//…" or scheme-ful URLs),
    so a `next` parameter can never turn /login into an open redirector. Used
    by the OAuth authorize flow to send a freshly signed-in user back to the
    consent screen instead of stranding them on the portal home.
    """
    if not isinstance(candidate, str) or not candidate.startswith("/"):
        return None
    if candidate.startswith("//") or len(candidate) > 512 or "\\" in candidate:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return None
    return candidate


def _login_page(
    error: Optional[str] = None, prelogin_token: str = "", next_path: Optional[str] = None
) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    next_input = (
        f'\n  <input type="hidden" name="next" value="{html.escape(next_path)}">' if next_path else ""
    )
    return _page(
        "Anki MCP - Sign in",
        f"""<h1>Sign in</h1>
<form method="post" action="/login">
  <input type="hidden" name="{PRELOGIN_FIELD_NAME}" value="{html.escape(prelogin_token)}">{next_input}
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


def _deck_tree(decks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Nest the flat deck list into a tree keyed by the ``::`` path separator.

    Each node carries the LAST path segment as its label plus own counts
    (what the deck itself holds) and subtree counts, which start as the own
    values and gain every descendant's on the way up in :func:`_aggregate_deck`.
    Sibling order follows the input order (``_collect_deck_stats`` already
    sorts case-insensitively by full name). A parent path that arrives without
    its own entry (cannot happen in Anki, which materialises parents) still
    gets a placeholder node so the nesting stays well-formed.
    """
    roots: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for deck in decks:
        parts = str(deck["name"]).split("::")
        parent: Optional[Dict[str, Any]] = None
        for depth, label in enumerate(parts):
            path = "::".join(parts[: depth + 1])
            node = index.get(path)
            if node is None:
                node = {
                    "label": label,
                    "children": [],
                    "own_notes": 0,
                    "own_cards": 0,
                    "own_due": 0,
                    "notes": 0,
                    "cards": 0,
                    "due": 0,
                }
                index[path] = node
                (roots if parent is None else parent["children"]).append(node)
            parent = node
        assert parent is not None
        parent["own_notes"] += int(deck["notes"])
        parent["own_cards"] += int(deck["cards"])
        parent["own_due"] += int(deck["due"])
        parent["notes"] += int(deck["notes"])
        parent["cards"] += int(deck["cards"])
        parent["due"] += int(deck["due"])
    return roots


def _aggregate_deck(node: Dict[str, Any]) -> None:
    """Fold each child's subtree counts into ``node`` (in place, recursive)."""
    for child in node["children"]:
        _aggregate_deck(child)
        node["notes"] += child["notes"]
        node["cards"] += child["cards"]
        node["due"] += child["due"]


def _deck_node_html(node: Dict[str, Any]) -> str:
    """One <details class="decknode"> row: last-segment name, subtree counts.

    Numbers on a parent are SUBTREE aggregates (own + all descendants); when
    the parent itself directly holds cards a muted ``(own ...)`` hint shows
    its own numbers so the aggregate cannot be misread. Child nodes are
    nested inside the parent's <details>, which also indents them via CSS.
    """
    counts = (
        f'<span class="counts">{node["notes"]} notes &middot; {node["cards"]} cards '
        f'&middot; {node["due"]} due</span>'
    )
    own = ""
    if node["children"] and (node["own_notes"] or node["own_cards"] or node["own_due"]):
        own = (
            f'<span class="muted">(own {node["own_notes"]} notes, '
            f'{node["own_cards"]} cards, {node["own_due"]} due)</span>'
        )
    children_html = "".join(_deck_node_html(child) for child in node["children"])
    leaf_class = " leaf" if not node["children"] else ""
    return (
        f'<details class="decknode{leaf_class}">'
        f'<summary><span class="deckname">{html.escape(node["label"])}</span>{counts}{own}</summary>'
        f"{children_html}</details>"
    )


def _deck_section(stats: Optional[Dict[str, Any]]) -> str:
    if stats is None:
        return (
            '<section><h2>Decks</h2>'
            '<p class="muted">Deck statistics are unavailable right now.</p></section>'
        )
    roots = _deck_tree(stats["decks"])
    for root in roots:
        _aggregate_deck(root)
    tree = "".join(_deck_node_html(root) for root in roots)
    if not tree:
        tree = '<p class="muted">No decks yet.</p>'
    due = stats["due"]
    return f"""<section>
  <h2>Decks</h2>
  {tree}
  <p class="muted">Due now: {due['new']} new, {due['learn']} learning, {due['review']} review.</p>
</section>"""


def _human_size(num_bytes: int) -> str:
    """Human-readable byte size ("1.2 MB"); anything below 1 KB stays exact."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _backup_section(csrf_token: str, backups: Optional[List[Dict[str, Any]]]) -> str:
    """Version history: newest snapshots (When / Reason / Size) + row actions.

    ``backups`` is a ``registry.list_backups`` record list (newest first) or
    None when the listing failed — a listing failure must not take the portal
    down, mirroring the deck-stats posture. At most BACKUP_LIST_LIMIT rows
    render; the rest collapse into one muted "+N older" line. Filenames ride
    in query strings, so they are URL-escaped via html.escape(quote=True).
    """
    if backups is None:
        listing = '<p class="muted">Snapshot list is unavailable right now.</p>'
    elif not backups:
        listing = (
            '<p class="muted">No snapshots yet &mdash; one is created before '
            "every sync and destructive change.</p>"
        )
    else:
        visible = backups[:BACKUP_LIST_LIMIT]
        rows = "".join(
            f"<tr><td>{html.escape(str(entry['timestamp']))}</td>"
            f"<td><code>{html.escape(str(entry['reason']))}</code></td>"
            f"<td>{_human_size(int(entry['size_bytes']))}</td>"
            '<td><a href="/portal/backup/confirm?file='
            f'{html.escape(str(entry["filename"]))}">Restore</a> '
            '<a href="/portal/backup/download?file='
            f'{html.escape(str(entry["filename"]))}">Download</a></td></tr>'
            for entry in visible
        )
        older = len(backups) - len(visible)
        older_note = f'<p class="muted">+{older} older</p>' if older > 0 else ""
        listing = (
            f"""<div class="tablewrap"><table>
      <thead><tr><th>When</th><th>Reason</th><th>Size</th><th>Actions</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
    {older_note}"""
        )
    return f"""<section>
  <h2>Version history</h2>
  <p>A snapshot is taken before every sync and destructive change. Restoring
  replaces the current collection &mdash; the replaced state is kept
  automatically as a pre-restore snapshot. Snapshots exclude media files.</p>
  <form method="post" action="/portal/backup/create">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <button type="submit">Back up now</button>
  </form>
  {listing}
</section>"""


def _backup_confirm_page(user: User, csrf_token: str, entry: Dict[str, Any]) -> str:
    """Small confirm page for restoring one validated snapshot.

    Shows the snapshot's timestamp/reason/size, warns that restoring replaces
    the current collection (the pre-restore snapshot keeps the replaced
    state), and carries the CSRF'd POST form with the filename hidden field.
    """
    filename = str(entry["filename"])
    return _page(
        "Anki MCP - Restore snapshot",
        f"""{_app_header(user, csrf_token)}
<section>
  <h2>Restore snapshot</h2>
  <p>You are about to restore <code>{html.escape(filename)}</code></p>
  <div class="tablewrap"><table>
    <thead><tr><th>When</th><th>Reason</th><th>Size</th></tr></thead>
    <tbody><tr>
      <td>{html.escape(str(entry["timestamp"]))}</td>
      <td><code>{html.escape(str(entry["reason"]))}</code></td>
      <td>{_human_size(int(entry["size_bytes"]))}</td>
    </tr></tbody>
  </table></div>
  <p class="error">Restoring replaces the CURRENT collection with this snapshot.
  The collection as it is now is kept automatically as a pre-restore snapshot,
  so this restore is itself reversible.</p>
  <p>After restoring, the next AnkiWeb sync reconciles the differences with
  AnkiWeb &mdash; review the changes before syncing.</p>
  <form method="post" action="/portal/backup/restore">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <input type="hidden" name="file" value="{html.escape(filename)}">
    <button type="submit">Restore this snapshot</button>
  </form>
  <p><a href="/">Back to portal</a></p>
</section>""",
        css=_APP_CSS,
        main_class="app",
    )


def _portal_page(
    user: User,
    csrf_token: str,
    flash: Optional[Tuple[str, str]],
    stats: Optional[Dict[str, Any]],
    backups: Optional[List[Dict[str, Any]]],
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
{_backup_section(csrf_token, backups)}
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


def _deleted_archive_dir(data_dir: str, username: str) -> str:
    """Collision-free archive destination ``<data_dir>/deleted/<name>-<unix_ts>[n]``.

    Deletion never removes files: archived account data is MOVED here so it
    stays recoverable (``deleted/`` is created as needed; a same-second
    collision gets a numeric suffix). Raises ValueError for a non-path-safe
    username and OSError when the directory cannot be created.
    """
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Invalid username {username!r}")
    deleted_root = os.path.join(data_dir, "deleted")
    os.makedirs(deleted_root, exist_ok=True)
    base = f"{username}-{int(time.time())}"
    dest = os.path.join(deleted_root, base)
    suffix = 1
    while os.path.exists(dest):
        dest = os.path.join(deleted_root, f"{base}-{suffix}")
        suffix += 1
    return dest


def _archive_user_data(data_dir: str, username: str) -> Optional[str]:
    """Move ``<data_dir>/users/<name>`` to ``<data_dir>/deleted/<name>-<unix_ts>``.

    The whole directory is MOVED aside so the account's collection stays
    recoverable. The per-user ledger directory is archived separately by
    :func:`_archive_ledger_dir` (it lives outside ``users/`` and must be
    detached before a same-username recreate can reload it).

    Returns the destination path, or None when the user had no data directory.
    Raises ValueError for a non-path-safe username and OSError when the rename
    fails, so callers can abort the account deletion instead of destroying a
    record whose data could not be archived.
    """
    src = os.path.join(data_dir, "users", username)
    if not os.path.isdir(src):
        return None
    dest = _deleted_archive_dir(data_dir, username)
    os.rename(src, dest)
    return dest


def _archive_ledger_dir(data_dir: str, username: str, archive_path: Optional[str]) -> Optional[str]:
    """Move ``<data_dir>/ledgers/<name>`` into the account's deleted/ archive.

    Companion to :func:`_archive_user_data`: the registry's drop_user hook
    clears the IN-MEMORY ledger, but the on-disk ``ledger.json`` would
    otherwise survive deletion and be reloaded — including after a process
    restart — by an account recreated under the same username, replaying the
    previous account's idempotency keys. The directory is renamed to
    ``<archive>/ledgers`` (the same ``deleted/<name>-<ts>`` directory that
    holds the archived collection, or a fresh one when the account had no
    data directory), keeping the whole account recoverable in one place while
    detaching it from the name.

    Returns the destination directory, or None when there was no ledger dir
    (skip silently — nothing to archive). Call AFTER ``drop_user``: once the
    in-memory ledger is popped, no racing sync can recreate the old path
    between the rename and the state drop. Raises on rename failure so the
    caller can log it; by then the record deletion is already committed.
    """
    src = os.path.join(data_dir, "ledgers", username)
    if not os.path.isdir(src):
        return None
    dest_root = archive_path if archive_path else _deleted_archive_dir(data_dir, username)
    os.makedirs(dest_root, exist_ok=True)
    dest = os.path.join(dest_root, "ledgers")
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

    # 9. create_deck
    @mcp.tool(
        description="Create a deck ('Parent::Child' creates subdecks); an existing name resolves to its ID."
    )
    async def create_deck(deck_name: str, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = CreateDeckInput(deck_name=deck_name)
            res = await deck_tools.create_deck(tool_sync_manager, tool_registry, username, parsed.deck_name)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 10. rename_deck
    @mcp.tool(
        description="Rename a deck (subdecks follow). DESTRUCTIVE: requires confirm=true; a "
        "pre-rename-deck snapshot backup is taken first and the rename is aborted if it fails."
    )
    async def rename_deck(deck_id: int, new_name: str, confirm: bool = False, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = RenameDeckInput(deck_id=deck_id, new_name=new_name, confirm=confirm)
            res = await deck_tools.rename_deck(
                tool_sync_manager, tool_registry, username, parsed.deck_id, parsed.new_name, parsed.confirm
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 11. delete_deck
    @mcp.tool(
        description="Delete a deck, its subdecks and all their cards (the Default deck can never be "
        "deleted). DESTRUCTIVE: requires confirm=true; a pre-delete-deck snapshot backup is taken "
        "first and the deletion is aborted if it fails."
    )
    async def delete_deck(deck_id: int, confirm: bool = False, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = DeleteDeckInput(deck_id=deck_id, confirm=confirm)
            res = await deck_tools.delete_deck(
                tool_sync_manager, tool_registry, username, parsed.deck_id, parsed.confirm
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 12. delete_notes
    @mcp.tool(
        description="Delete notes and their cards by ID; missing IDs are reported, not fatal. "
        "DESTRUCTIVE: requires confirm=true; a pre-delete-notes snapshot backup is taken first "
        "and the deletion is aborted if it fails."
    )
    async def delete_notes(
        note_ids: List[int],
        confirm: bool = False,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = DeleteNotesInput(note_ids=note_ids, confirm=confirm, idempotency_key=idempotency_key)
            res = await note_tools.delete_notes(
                tool_sync_manager, tool_registry, username,
                parsed.note_ids, parsed.confirm, parsed.idempotency_key,
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 13. remove_tags
    @mcp.tool(description="Remove tags from notes.")
    async def remove_tags(note_ids: List[int], tags: List[str], ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = RemoveTagsInput(note_ids=note_ids, tags=tags)
            res = await note_tools.remove_tags(tool_sync_manager, tool_registry, username, parsed.note_ids, parsed.tags)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 14. list_note_types
    @mcp.tool(description="List note types with their field names and card templates.")
    async def list_note_types(ctx: Context) -> Dict[str, Any]:
        username = _username_from_context(ctx)
        if username is None:
            return _unauthorized_tool_response()
        try:
            return await note_tools.list_note_types(tool_registry, username)
        except Exception as e:
            return _tool_error_response(e)

    # 15. create_note_type
    @mcp.tool(
        description="Create a note type from field names and card templates (omit templates for a "
        "default single template over the first two fields). Refuses an existing name."
    )
    async def create_note_type(
        name: str,
        fields: List[str],
        templates: Optional[List[Dict[str, str]]] = None,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed_templates = (
                [TemplateInput(**t) for t in templates] if templates is not None else None
            )
            parsed = CreateNoteTypeInput(name=name, fields=fields, templates=parsed_templates)
            res = await note_tools.create_note_type(
                tool_sync_manager, tool_registry, username, parsed.name, parsed.fields, parsed.templates
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 16. find_cards
    @mcp.tool(description="Search cards by Anki query with bounded pagination.")
    async def find_cards(query: str, limit: int = 50, offset: int = 0, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = FindCardsInput(query=query, limit=limit, offset=offset)
            return await cards_tools.find_cards(tool_registry, username, parsed.query, parsed.limit, parsed.offset)
        except Exception as e:
            return _tool_error_response(e)

    # 17. get_card
    @mcp.tool(description="Retrieve a card's fields, rendered question/answer, and scheduling state.")
    async def get_card(card_id: int, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            GetCardInput(card_id=card_id)
            return await cards_tools.get_card(tool_registry, username, card_id)
        except Exception as e:
            return _tool_error_response(e)

    # 18. suspend_cards
    @mcp.tool(description="Suspend cards so they leave the review queue until unsuspended.")
    async def suspend_cards(card_ids: List[int], ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = SuspendCardsInput(card_ids=card_ids)
            res = await cards_tools.suspend_cards(tool_sync_manager, tool_registry, username, parsed.card_ids)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 19. unsuspend_cards
    @mcp.tool(description="Unsuspend previously suspended cards.")
    async def unsuspend_cards(card_ids: List[int], ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = SuspendCardsInput(card_ids=card_ids)
            res = await cards_tools.unsuspend_cards(tool_sync_manager, tool_registry, username, parsed.card_ids)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 20. set_due_date
    @mcp.tool(
        description="Set cards' due date to N days from today ('3') or a random range ('0-2')."
    )
    async def set_due_date(card_ids: List[int], days: str, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = SetDueDateInput(card_ids=card_ids, days=days)
            res = await cards_tools.set_due_date(tool_sync_manager, tool_registry, username, parsed.card_ids, parsed.days)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 21. move_cards
    @mcp.tool(description="Move cards to another deck (target deck must exist).")
    async def move_cards(card_ids: List[int], deck_id: int, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = MoveCardsInput(card_ids=card_ids, deck_id=deck_id)
            res = await cards_tools.move_cards(tool_sync_manager, tool_registry, username, parsed.card_ids, parsed.deck_id)
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 22. get_review_queue
    @mcp.tool(description="Fetch the scheduler's current review queue with capped questions.")
    async def get_review_queue(limit: int = 10, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = ReviewQueueInput(limit=limit)
            return await cards_tools.get_review_queue(tool_registry, username, parsed.limit)
        except Exception as e:
            return _tool_error_response(e)

    # 23. answer_card
    @mcp.tool(
        description="Answer a queued card with rating 1=Again, 2=Hard, 3=Good, 4=Easy (v3 scheduler). "
        "The card must currently be in the review queue; only the first 50 queued cards are "
        "considered, so a card deeper in a long queue is refused until it reaches the front."
    )
    async def answer_card(
        card_id: int,
        rating: int,
        idempotency_key: Optional[str] = None,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = AnswerCardInput(card_id=card_id, rating=rating, idempotency_key=idempotency_key)
            res = await cards_tools.answer_card(
                tool_sync_manager, tool_registry, username,
                parsed.card_id, parsed.rating, parsed.idempotency_key,
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 24. store_media_file
    @mcp.tool(
        description="Store a media file (standard base64, max 4 MiB decoded, single-segment "
        "filename); returns the actual stored name."
    )
    async def store_media_file(filename: str, data_base64: str, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = StoreMediaInput(filename=filename, data_base64=data_base64)
            res = await media_tools.store_media_file(
                tool_sync_manager, tool_registry, username, parsed.filename, parsed.data_base64
            )
            return res.model_dump()
        except Exception as e:
            return _tool_error_response(e)

    # 25. get_media_file
    @mcp.tool(description="Read a media file back as base64 (files over 4 MiB are refused).")
    async def get_media_file(filename: str, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = GetMediaInput(filename=filename)
            return await media_tools.get_media_file(tool_registry, username, parsed.filename)
        except Exception as e:
            return _tool_error_response(e)

    # 26. list_media_files
    @mcp.tool(description="List media filenames (sorted), capped at the requested limit.")
    async def list_media_files(limit: int = 200, ctx: Context = None) -> Dict[str, Any]:
        username = _username_from_context(ctx) if ctx is not None else None
        if username is None:
            return _unauthorized_tool_response()
        try:
            parsed = ListMediaInput(limit=limit)
            return await media_tools.list_media_files(tool_registry, username, parsed.limit)
        except Exception as e:
            return _tool_error_response(e)

    # Create Starlette app for streamable HTTP with reverse-proxy support
    from mcp.server.transport_security import TransportSecuritySettings

    # Session-table posture owned in app code (audit fix): stateful mode is
    # explicit, and the per-process session cardinality + idle reaping come
    # from operator-tunable settings instead of the SDK wheel's defaults
    # (10,000 sessions / 1800s idle) — on this shared multi-user surface the
    # defaults would let any one token holder lock every other user out of
    # /mcp at trivial cost.
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=False,
        max_sessions=settings.mcp_max_sessions,
        session_idle_timeout=settings.mcp_session_idle_timeout,
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

    # Same capture pattern for the settings object: the handlers below must
    # sign/verify cookies and archive data dirs with THIS app's settings, so a
    # later create_app(custom_settings=...) rebind of the module global cannot
    # reach into already-serving apps.
    app_settings = settings

    def get_session_user(request: Request) -> Optional[User]:
        """Resolve the anki_session cookie to an account, or None.

        Signed-cookie validity alone is not enough: the named account must
        still exist (deleted users lose access immediately even with a valid
        stateless cookie; lookup goes through UserStore.get, not a list scan),
        and the cookie must postdate the account's session-revocation epoch:
        the MAC-covered issued-at stamp is compared against
        ``User.sessions_invalid_before`` (bumped by password resets), so a
        cookie captured before a reset is dead even while unexpired — closing
        the stale-session-to-token-mint chain. Cookies without a parseable
        issued-at stamp fail closed.
        """
        session_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_value:
            return None
        username = verify_session(app_settings.session_secret, session_value)
        if username is None:
            return None
        issued_at = session_issued_before(session_value)
        if issued_at is None:
            return None  # legacy/malformed cookie: reject (fail closed)
        user = store.get(username)
        if user is None:
            return None
        if issued_at < user.sessions_invalid_before:
            return None  # issued before a password-reset epoch bump
        return user

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
        return verify_flash(app_settings.session_secret, raw) if raw else None

    def _clear_flash(response: Response) -> None:
        response.delete_cookie(FLASH_COOKIE_NAME, path="/", secure=secure_cookies_enabled())

    def _redirect_with_flash(url: str, message: str, kind: str = "info") -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLASH_COOKIE_NAME,
            issue_flash(app_settings.session_secret, message, kind),
            max_age=FLASH_TTL_SECONDS,
            path="/",
            httponly=True,
            samesite="strict",
            secure=secure_cookies_enabled(),
        )
        return response

    def _bad_request(message: str, back_href: str = "/admin", back_label: str = "Back to admin") -> HTMLResponse:
        return HTMLResponse(_message_page(message, back_href, back_label), status_code=400)

    def _login_form_response(
        error: Optional[str] = None, status_code: int = 200, next_path: Optional[str] = None
    ) -> HTMLResponse:
        """Render the login form together with a FRESH prelogin CSRF pair.

        The nonce|MAC value goes into the hidden form field and into the
        anki_prelogin cookie (HttpOnly, SameSite=strict, path=/login) — the
        browser must echo both back on POST. Re-issued on every render,
        including the 401 re-render, so a mistyped password does not strand
        the user with a stale pair. ``next_path`` (already validated by
        _safe_next) rides along as a hidden field so the OAuth consent flow
        survives a failed first attempt.
        """
        token = _issue_prelogin(app_settings.session_secret)
        response = HTMLResponse(_login_page(error, token, next_path), status_code=status_code)
        response.set_cookie(
            PRELOGIN_COOKIE_NAME,
            token,
            max_age=PRELOGIN_TTL_SECONDS,
            path="/login",
            httponly=True,
            samesite="strict",
            secure=secure_cookies_enabled(),
        )
        return response

    async def login_page(request: Request) -> Response:
        nxt = _safe_next(request.query_params.get("next"))
        if get_session_user(request) is not None:
            return RedirectResponse(nxt or "/", status_code=303)
        return _login_form_response(next_path=nxt)

    async def login_submit(request: Request) -> Response:
        # Body cap BEFORE the form parse: starlette's per-field caps do not
        # bound a request's total body (and file parts spool to disk), so the
        # declared Content-Length gates all parse work on this unauthenticated
        # route. One header read, zero body bytes consumed.
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
        form = await request.form()
        nxt = _safe_next(form.get("next"))
        # Login CSRF first — ahead of the rate limiter and any password work:
        # a cross-site forged POST can neither read the prelogin cookie (Same-
        # Site=strict + HttpOnly) nor know the form field value, so it is
        # refused here without consuming lockout budget or scrypt effort.
        cookie_token = request.cookies.get(PRELOGIN_COOKIE_NAME, "")
        form_token = str(form.get(PRELOGIN_FIELD_NAME, ""))
        if (
            not form_token
            or form_token != cookie_token
            or not _verify_prelogin(app_settings.session_secret, form_token)
        ):
            return HTMLResponse(
                _message_page("Invalid or missing CSRF token."), status_code=403
            )
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        ip = request.client.host if request.client else ""
        # Brute-force gate second: a locked key is rejected before any password
        # work happens, and the page reveals nothing about the account.
        if not login_rate_limiter.check(username, ip):
            logger.warning("Login rate-limited for %r from %s", username, ip)
            return HTMLResponse(
                _message_page("Too many failed attempts. Please try again later."),
                status_code=429,
            )
        valid = False
        if username:
            # scrypt is CPU-bound; keep it off the event loop, and cap how
            # much unauthenticated crypto work the shared process hosts:
            # unauthenticated callers get at most a few concurrent burns
            # regardless of uvicorn's overall concurrency ceiling.
            async with login_verify_capacity:
                valid = await run_in_threadpool(store.verify_password, username, password)
        if valid:
            login_rate_limiter.record_success(username, ip)
            response = RedirectResponse(nxt or "/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE_NAME,
                issue_session(app_settings.session_secret, username),
                max_age=SESSION_TTL_SECONDS,
                path="/",
                httponly=True,
                # Lax (not strict): the OAuth authorize flow arrives as a
                # cross-site TOP-LEVEL GET from the connector's host (e.g.
                # claude.ai), and strict would withhold the session cookie
                # there — every connector link would force a fresh login. Lax
                # still withholds the cookie on cross-site POSTs, and every
                # state-changing form carries a session-bound CSRF token, so
                # the CSRF posture is unchanged.
                samesite="lax",
                secure=secure_cookies_enabled(),
            )
            logger.info("Web session issued for %r from %s", username, ip)
            return response
        login_rate_limiter.record_failure(username, ip)
        # Generic message: identical for unknown usernames and wrong passwords.
        # The retry form ships with a fresh prelogin pair (and the pending
        # next-path, so an OAuth flow survives a mistyped password).
        return _login_form_response(
            "Invalid username or password.", status_code=401, next_path=nxt
        )

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
        # Snapshot listing for Version history (cheap directory read); same
        # resilience posture as the stats above.
        backups: Optional[List[Dict[str, Any]]] = None
        try:
            backups = await run_in_threadpool(portal_registry.list_backups, user.username)
        except Exception:
            logger.exception("Backup listing failed for %r", user.username)
        response = HTMLResponse(
            _portal_page(user, csrf_token, flash, stats, backups, token_mask)
        )
        if request.cookies.get(FLASH_COOKIE_NAME):
            _clear_flash(response)  # consumed: render at most once
        return response

    async def logout_submit(request: Request) -> Response:
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        # Maintenance gate (audit fix): this handler calls the registry's
        # sync_user directly, so SyncManager's own gate never sees this path.
        # Refuse BEFORE any registry call — an operator who paused writes must
        # not be circumvented by the portal's sync button (no backup, no
        # retention rotation, no AnkiWeb contact).
        if tool_sync_manager.maintenance_mode:
            return _redirect_with_flash(
                "/",
                "Worker is currently in maintenance mode; writes are paused.",
                kind="error",
            )
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

    # -- Portal version history (snapshots): create / confirm / restore / ------
    #    download. Session-authenticated like every portal route; every POST is
    #    CSRF-verified, and every filename is validated by exact string
    #    membership against the acting user's OWN list_backups() output before
    #    it may address a file — unknown names, other users' snapshots and
    #    traversal strings all fail the same way (400).

    def _backup_entry(username: str, filename: str) -> Optional[Dict[str, Any]]:
        """The user's list_backups record whose filename equals ``filename``.

        The membership check IS the access control: ``filename`` never
        reaches the filesystem on its own, so it cannot traverse, and a
        snapshot belonging to a different account is simply not in this
        user's listing. Returns None for anything else.
        """
        if not isinstance(filename, str) or not filename:
            return None
        for entry in portal_registry.list_backups(username):
            if entry["filename"] == filename:
                return entry
        return None

    async def portal_backup_create(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        # Same maintenance contract as the sync button: a paused worker must
        # not write snapshots or rotate retention behind the operator's back.
        if tool_sync_manager.maintenance_mode:
            return _redirect_with_flash(
                "/",
                "Worker is currently in maintenance mode; writes are paused.",
                kind="error",
            )
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        try:
            filename = await portal_registry.backup_user(user.username, "manual")
        except Exception as e:
            logger.error("Portal manual backup failed for %r: %s", user.username, e)
            return _redirect_with_flash("/", f"Backup failed: {e}", kind="error")
        logger.info("Portal manual backup for %r: %r", user.username, filename)
        return _redirect_with_flash("/", f"Snapshot created: {filename}.", kind="success")

    async def portal_backup_confirm(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        filename = request.query_params.get("file", "")
        entry = _backup_entry(user.username, filename)
        if entry is None:
            return _bad_request(
                f"Unknown snapshot {filename!r}.", back_href="/", back_label="Back to portal"
            )
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_token = issue_csrf(session_value)
        return HTMLResponse(_backup_confirm_page(user, csrf_token, entry))

    async def portal_backup_restore(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        # The maintenance pause must cover restore above all: it REPLACES the
        # collection file (and takes a pre-restore snapshot + retention
        # rotation on the way), none of which a paused worker may do.
        if tool_sync_manager.maintenance_mode:
            return _redirect_with_flash(
                "/",
                "Worker is currently in maintenance mode; writes are paused.",
                kind="error",
            )
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        filename = str(form.get("file", ""))
        if _backup_entry(user.username, filename) is None:
            return _bad_request(
                f"Unknown snapshot {filename!r}.", back_href="/", back_label="Back to portal"
            )
        try:
            result = await portal_registry.restore_backup(user.username, filename)
        except Exception as e:
            logger.error("Portal restore failed for %r from %r: %s", user.username, filename, e)
            return _redirect_with_flash("/", f"Restore failed: {e}", kind="error")
        snapshot = str(result.get("pre_restore_snapshot", ""))
        logger.info(
            "Portal restore for %r from %r (pre-restore snapshot: %r)",
            user.username,
            filename,
            snapshot,
        )
        return _redirect_with_flash(
            "/",
            f"Collection restored from {filename}. The replaced state was kept as "
            f"snapshot {snapshot}. The next AnkiWeb sync reconciles the differences "
            "— review the changes before syncing.",
            kind="success",
        )

    async def portal_backup_download(request: Request) -> Response:
        user, err = _require_session(request)
        if err:
            return err
        filename = request.query_params.get("file", "")
        if _backup_entry(user.username, filename) is None:
            return _bad_request(
                f"Unknown snapshot {filename!r}.", back_href="/", back_label="Back to portal"
            )
        # The path is built from the validated name ONLY AFTER the exact-
        # membership check above, so the filename can never carry a path.
        # colpkg IS a zip, hence the media type.
        path = portal_registry.backup_path(user.username, filename)
        return FileResponse(path, filename=filename, media_type="application/zip")

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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
        form = await request.form()
        if not _csrf_ok(session_value, form):
            return HTMLResponse(_message_page("Invalid or missing CSRF token."), status_code=403)
        username = str(form.get("username", "")).strip()
        role = str(form.get("role", ""))
        # Last-admin guard: count admins from the store BEFORE applying, so a
        # demotion can never remove the only admin account (the service would
        # be unadministerable; recovery would need the offline useradmin CLI).
        target = store.get(username)
        if (
            target is not None
            and target.role == "admin"
            and role != "admin"
            and sum(1 for u in store.list_users() if u.role == "admin") <= 1
        ):
            return _bad_request(
                f"Cannot demote {username}: it is the last admin account. "
                "Promote another account to admin first."
            )
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
        if _body_too_large(request):
            return HTMLResponse(_message_page("Request body too large."), status_code=413)
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
        target = store.get(username)
        if target is None:
            return _bad_request(f"Unknown user: {username}")
        # Last-admin guard, counted from the store BEFORE any change: deleting
        # the only admin would leave the service unadministerable (recovery
        # would need the offline useradmin CLI). Checked before archiving so a
        # refused deletion moves no data either.
        if target.role == "admin" and sum(1 for u in store.list_users() if u.role == "admin") <= 1:
            return _bad_request(
                f"Cannot delete {username}: it is the last admin account. "
                "Promote another account to admin first."
            )
        try:
            # Archive FIRST: if the rename fails, the account record stays put
            # rather than losing a user whose data could not be moved aside.
            archive_path = _archive_user_data(app_settings.data_dir, username)
        except (OSError, ValueError) as e:
            logger.error("Failed to archive data directory for %r: %s", username, e)
            return _bad_request(f"Deletion failed while archiving data: {e}")
        try:
            await run_in_threadpool(store.delete_user, username)
        except ValueError as e:
            # The in-lock last-admin guard raced a concurrent admin change
            # after the pre-check above. The data directory was ALREADY
            # archived, so be honest about the divergence rather than 500:
            # the account record survives and its collection restarts empty
            # (recoverable by an operator from the deleted/ archive).
            logger.error(
                "Delete of %r failed the store-level last-admin guard AFTER archiving: %s",
                username, e,
            )
            return _bad_request(
                f"{e} The account's data was already archived to {archive_path}; "
                "the account still exists and will start with an empty collection. "
                "Restore it from the archive if needed."
            )
        # Detach every serving state keyed by the username (audit fix): the
        # open Collection handle, the cached registry state and the in-memory
        # idempotency ledger would otherwise survive deletion and be handed
        # to an account recreated under the same name — the recreated user's
        # reads/writes and portal stats would come from the previous person's
        # archived collection. Unknown usernames and never-opened users are a
        # harmless no-op.
        await portal_registry.drop_user(username)
        try:
            # And move the on-disk ledger aside (into the same deleted/
            # archive), or a recreate — even after a restart — would reload
            # the previous account's idempotency keys. Runs AFTER drop_user so
            # no in-flight sync can resurrect the path mid-rename; a failure
            # here cannot be rolled back (the record is already deleted), so
            # it is logged for the operator instead of aborting.
            ledger_archive = _archive_ledger_dir(app_settings.data_dir, username, archive_path)
        except (OSError, ValueError) as e:
            logger.error("Failed to archive ledger directory for %r: %s", username, e)
            ledger_archive = None
        logger.info(
            "Admin %r deleted account %r (data archived at %s, ledger archived at %s)",
            admin.username,
            username,
            archive_path or "no data directory",
            ledger_archive or "no ledger directory",
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
    app.add_route("/portal/backup/create", portal_backup_create, methods=["POST"])
    app.add_route("/portal/backup/confirm", portal_backup_confirm, methods=["GET"])
    app.add_route("/portal/backup/restore", portal_backup_restore, methods=["POST"])
    app.add_route("/portal/backup/download", portal_backup_download, methods=["GET"])
    app.add_route("/admin", admin_page, methods=["GET"])
    app.add_route("/admin/create-user", admin_create_user, methods=["POST"])
    app.add_route("/admin/reset-password", admin_reset_password, methods=["POST"])
    app.add_route("/admin/regenerate-token", admin_regenerate_token, methods=["POST"])
    app.add_route("/admin/set-role", admin_set_role, methods=["POST"])
    app.add_route("/admin/delete-user", admin_delete_user, methods=["POST"])

    # -- OAuth 2.1 authorization server (claude.ai custom connectors) ---------
    # Discovery, dynamic client registration, the consent-gated authorize
    # flow, and the token endpoint live in src/oauth.py. The store is
    # constructed HERE (never at module scope), exactly once per app and
    # bound to this app's settings/data dir — same capture discipline as
    # portal_registry/app_settings above.
    oauth_store = OAuthStore(os.path.join(app_settings.data_dir, "oauth_state.json"))
    add_oauth_routes(app, oauth_store, user_store, app_settings)

    # Per-user token auth for the MCP surface; the web routes above are in the
    # middleware's explicit exempt allowlist (they carry their own session-
    # cookie authentication) along with /health and OPTIONS.
    app.add_middleware(AuthMiddleware, store=store, settings=settings)

    # Introspection handles for tests/ops: the registry and store THIS app owns.
    app.state.anki_registry = collection_registry
    app.state.anki_store = store
    app.state.anki_oauth_store = oauth_store

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
    """First-boot admin bootstrap, operating on the module-global store.

    Credentials come from ADMIN_USERNAME/ADMIN_PASSWORD when provided; when
    they are absent (zero-touch deployments) the password is generated and
    persisted (0600, atomic) in the data volume so an operator can retrieve
    it after the fact. The service still never comes up with zero accounts.

    MUST rebind the module globals (not build locals): create_app() captures
    ``user_store`` and the tool wrappers use ``sync_manager`` when called without
    custom_settings — the production path — so bootstrapping through a local
    instance would leave the serving app authenticating against the import-time
    (empty on first boot) store, and the sync manager resolving credentials
    through that same stale store ("Unknown user" on first boot). Regression-
    tested by test_first_boot_bootstrap_visible_to_serving_app.
    """
    global user_store, sync_manager
    user_store = UserStore(app_settings.users_file)
    sync_manager = SyncManager(collection_registry, user_store, app_settings)
    if not user_store.list_users():
        username = app_settings.admin_username or "admin"
        generated = not app_settings.admin_password
        password = app_settings.admin_password or secrets.token_urlsafe(18)
        if generated:
            # Persist BEFORE creating the account: if the password cannot be
            # stored, do not create an admin nobody can log in as.
            try:
                credentials_path = _write_initial_admin_credentials(app_settings, username, password)
            except OSError as e:
                logger.error(
                    "FATAL: no bootstrap credentials set and the generated admin password "
                    "could not be persisted in the data directory (%s). Set ADMIN_PASSWORD "
                    "explicitly or make the data directory writable.",
                    e,
                )
                raise SystemExit(1)
        try:
            user_store.bootstrap_admin(username, password)
        except ValueError as e:
            logger.error("FATAL: cannot bootstrap the initial admin account: %s", e)
            raise SystemExit(1)
        if generated:
            logger.info(
                "No ADMIN_USERNAME/ADMIN_PASSWORD set: bootstrapped initial admin %r with a "
                "generated password, stored with mode 0600 at %s. Read it with "
                "`docker exec <container> cat %s`, log in at the portal, then change the "
                "password (the file can be deleted afterwards).",
                username,
                credentials_path,
                credentials_path,
            )
        else:
            logger.info("Bootstrapped initial admin account %r", username)


def _write_initial_admin_credentials(app_settings: Settings, username: str, password: str) -> str:
    """Persist the generated first-boot admin credentials (atomic, 0600); returns the path."""
    path = os.path.join(app_settings.data_dir, "initial-admin-password.txt")
    os.makedirs(app_settings.data_dir, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=app_settings.data_dir, prefix="initial-admin-", suffix=".tmp"
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            f.write(
                "Anki-WebMCP first-boot admin credentials\n"
                "(generated because ADMIN_USERNAME/ADMIN_PASSWORD were not set)\n"
                f"\nusername: {username}\npassword: {password}\n"
                "\nLog in at the portal with these, then change the password from the\n"
                "portal or manage accounts in /admin. This file is not needed after the\n"
                "first login and can be deleted.\n"
            )
            f.flush()
            os.fsync(f.fileno())
        # mkstemp already creates the file 0o600; re-assert against umask oddities.
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception:
        with suppress(OSError):
            os.remove(temp_path)
        raise
    return path


def _resolve_session_secret(app_settings: Settings) -> List[str]:
    """Zero-touch session-secret provisioning; returns fatal problems.

    When SESSION_SECRET is absent from the environment, generate one and
    persist it (0600, atomic) in the data volume; the same object handed in
    (the module-global settings in production) is updated in place so the
    serving app signs sessions with the generated secret. An explicitly set
    SESSION_SECRET always wins; a placeholder value is still rejected by
    validate_deployment_config.
    """
    if app_settings.session_secret:
        return []
    try:
        secret, path = load_or_create_session_secret(app_settings.data_dir)
    except OSError as e:
        return [
            "SESSION_SECRET is not set and could not be auto-generated inside the data "
            f"directory ({app_settings.data_dir!r}): {e}. Make the data directory writable "
            "or set SESSION_SECRET explicitly (openssl rand -hex 24)."
        ]
    object.__setattr__(app_settings, "session_secret", secret)
    logger.info(
        "SESSION_SECRET not provided: generated a deployment secret (persisted with mode "
        "0600 at %s; it is reused on every boot). Set SESSION_SECRET to override; delete "
        "the file to rotate.",
        path,
    )
    return []


def main():
    """Start uvicorn server, refusing unsafe configuration and bootstrapping the first admin."""
    problems = _resolve_session_secret(settings)
    problems.extend(validate_deployment_config(settings))
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
    uvicorn.run(app, **_uvicorn_run_kwargs(settings))


if __name__ == "__main__":
    main()
