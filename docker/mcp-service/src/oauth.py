"""OAuth 2.1 authorization server for the MCP resource (claude.ai connectors).

claude.ai "custom connectors" speak the MCP authorization spec: they discover
this server via RFC 9728 protected-resource metadata, register themselves
through RFC 7591 dynamic client registration, and obtain the account's MCP
token through an authorization-code grant with mandatory PKCE (S256), later
refreshed via rotating refresh tokens. The authorization server is
deliberately minimal and only ever hands out the account's EXISTING MCP token
(:meth:`src.users.UserStore.peek_token`) as the access token — there is no
second token universe to administer or revoke.

Security posture (invariants):

- Authorization codes are single-use, expire after 60 seconds, and are bound
  to (client_id, redirect_uri, code_challenge). They live in memory only: a
  restart merely aborts in-flight flows, which clients retry from scratch.
- PKCE S256 is mandatory and compared in constant time.
- The authorize endpoint never redirects on a bad request: an unregistered
  redirect_uri yields a plain 400 (open-redirect protection), so the error
  path can never bounce the user to an attacker-chosen URL.
- Refresh tokens are persisted only as sha256 hashes, capped per username,
  and rotated on every use (presenting a used token burns it).
- Consent rides the portal's session cookie + CSRF machinery
  (:mod:`src.webauth`); the browser-facing pages are HTML, everything else
  answers JSON per the OAuth error conventions.
- No secrets (codes, verifiers, tokens, refresh tokens) are ever logged —
  only client ids and usernames, with %r.

stdlib only: no new pip dependencies.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlparse

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .config import Settings
from .users import UserStore
from .webauth import (
    SESSION_COOKIE_NAME,
    issue_csrf,
    session_issued_before,
    verify_csrf,
    verify_session,
)

logger = logging.getLogger("anki_mcp.oauth")

# Authorization codes live 60 seconds: long enough to read a consent form,
# short enough that an intercepted code is nearly worthless (RFC 6749 4.1.2
# recommends <= 10 minutes; OAuth 2.1 draft pushes further).
CODE_TTL_SECONDS = 60

# Bound on live refresh tokens per username (oldest dropped first): a client
# that leaks refresh tokens can only ever hold a bounded slice of the store.
MAX_REFRESH_TOKENS_PER_USER = 10

# Bounds on dynamic client registration. /register is unauthenticated (the
# MCP spec's DCR path), so it must not grow persisted state without limit:
# after MAX_CLIENTS records further registrations are refused, and per-record
# fields are length-capped. Generous enough that a household never hits them.
MAX_CLIENTS = 250
MAX_CLIENT_NAME_CHARS = 128
MAX_REDIRECT_URIS_PER_CLIENT = 8
MAX_REDIRECT_URI_CHARS = 512

# Pre-auth body cap for the OAuth endpoints, mirroring main.MAX_FORM_BODY_BYTES
# (which cannot be imported here without a circular import): the OAuth routes
# are exempt from token auth, so an unauthenticated caller must not be able to
# make the process buffer an unbounded body (JSON) or spool multipart file
# parts to disk before any validation runs.
MAX_BODY_BYTES = 64 * 1024

# The one scope this server issues; anything else requested is coerced to it.
SCOPE = "mcp"


def _body_too_large(request: Request) -> bool:
    """True when the request body must be refused before parsing.

    Reads headers only (no body bytes consumed) so an oversized form or JSON
    body is refused before starlette's parsers allocate for it. Chunked bodies
    are refused outright: the parser's per-field caps do not bound multipart
    file parts, and browsers never chunk these posts.
    """
    if request.headers.get("transfer-encoding"):
        return True
    raw = request.headers.get("content-length", "")
    try:
        return int(raw) > MAX_BODY_BYTES
    except ValueError:
        # Unparseable/absent without transfer-encoding: nothing sane to read.
        return raw != ""


def _pkce_s256(verifier: str) -> str:
    """RFC 7636 S256 challenge: base64url(SHA256(verifier)), padding stripped."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _hash_refresh_token(raw_token: str) -> str:
    """Storable form of a refresh token (sha256 hex); the raw value is never persisted."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def public_base_url(request: Request) -> str:
    """Externally visible origin for this deployment, proxy headers first.

    Behind a reverse proxy (the Docker deployment) ``request.url`` reflects
    the internal hop, so ``X-Forwarded-Proto`` and the Host header win when
    present. The result NEVER carries a trailing slash, so callers can safely
    build ``f"{base}/path"`` discovery URLs from it.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded_proto.split(",")[0].strip() if forwarded_proto else request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def _redirect_uri_allowed(uri: str) -> bool:
    """Registration-time rule: https anywhere, http only for loopback hosts.

    Fragments are rejected outright (OAuth forbids them in redirect URIs —
    they never reach the server, so a registered fragment is unusable and
    only invites confusion).
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if not parsed.netloc or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        # urlparse lowercases the hostname for us.
        return parsed.hostname in ("localhost", "127.0.0.1")
    return False


def _valid_redirect_uris(uris: object) -> bool:
    """A registration's redirect_uris field: a non-empty, bounded list of allowed URIs."""
    if not isinstance(uris, list) or not uris:
        return False
    if len(uris) > MAX_REDIRECT_URIS_PER_CLIENT:
        return False
    return all(
        isinstance(uri, str) and len(uri) <= MAX_REDIRECT_URI_CHARS and _redirect_uri_allowed(uri)
        for uri in uris
    )


def _append_redirect_params(redirect_uri: str, params: Dict[str, str]) -> str:
    """Append OAuth redirect params (code/error, then state) to a redirect URI.

    Values are percent-encoded (``state`` is client-supplied and unbounded, so
    echoing it raw could corrupt the Location header); the separator is ``&``
    when the URI already carries a query string.
    """
    encoded = "&".join(f"{name}={quote(value, safe='')}" for name, value in params.items())
    separator = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{separator}{encoded}"


class OAuthStore:
    """JSON-backed store for OAuth clients and refresh-token hashes.

    Persisted shape at ``<data_dir>/oauth_state.json``::

        {"clients": {client_id: {...}}, "refresh_tokens": {sha256_hex: {...}}}

    Persistence mirrors :class:`src.users.UserStore._save_atomic`: write to a
    ``mkstemp`` file in the same directory (created 0o600 by ``mkstemp``),
    fsync, chmod 0600, then ``os.replace`` so a crash can never leave a torn
    file behind. Authorization codes are deliberately NOT persisted — they
    live 60 seconds in memory only, and a restart just aborts in-flight flows.

    Load tolerance: a missing file is a fresh deployment (empty state). A
    corrupt/unreadable file is logged and treated as empty rather than fatal —
    unlike the account store (``src/users.py``), whose corruption must be
    fatal to protect existing accounts, losing OAuth state only costs each
    client a re-registration plus one consent round-trip, and refusing to
    boot over it would take the whole MCP service down for it. Individual
    records are loaded defensively (non-dict entries skipped) so a partially
    damaged file degrades instead of poisoning every client.

    Single-instance assumption: the lock is process-internal; run exactly one
    mcp-service replica per data dir.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._clients: Dict[str, dict] = {}
        self._refresh: Dict[str, dict] = {}
        self._codes: Dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.file_path):
            logger.info("No OAuth state at %s; starting with an empty state", self.file_path)
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clients = data.get("clients", {})
            refresh = data.get("refresh_tokens", {})
            if not isinstance(clients, dict) or not isinstance(refresh, dict):
                raise ValueError("expected a JSON object with clients/refresh_tokens objects")
        except (OSError, ValueError, AttributeError) as e:
            logger.warning(
                "OAuth state at %s is unreadable or corrupt (%s); starting fresh "
                "(clients re-register; refresh tokens re-consent)",
                self.file_path,
                e,
            )
            return
        self._clients = {str(key): rec for key, rec in clients.items() if isinstance(rec, dict)}
        self._refresh = {str(key): rec for key, rec in refresh.items() if isinstance(rec, dict)}
        logger.info(
            "Loaded %d OAuth clients and %d refresh-token hashes from %s",
            len(self._clients),
            len(self._refresh),
            self.file_path,
        )

    def _save_atomic(self) -> None:
        """Persist state atomically (mkstemp in the same dir + fsync + os.replace)."""
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(
            dir=dir_name if dir_name else ".", prefix="oauth_", suffix=".tmp"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump({"clients": self._clients, "refresh_tokens": self._refresh}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # mkstemp already creates the file 0o600; re-assert in case of umask oddities.
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.file_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to atomically save OAuth state to %s: %s", self.file_path, e)
            raise

    # -- clients -------------------------------------------------------------

    def register_client(
        self, client_name: str, redirect_uris: List[str], token_endpoint_auth_method: str = "none"
    ) -> dict:
        """Register a dynamically-registered public client; returns its record."""
        client_id = secrets.token_urlsafe(16)
        record = {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "created_at": time.time(),
        }
        with self._lock:
            self._clients[client_id] = record
            self._save_atomic()
        logger.info(
            "Registered OAuth client %r (%d redirect URIs)", client_id, len(record["redirect_uris"])
        )
        # Copy semantics like UserStore: callers can never mutate store state.
        copy = dict(record)
        copy["redirect_uris"] = list(record["redirect_uris"])
        return copy

    def get_client(self, client_id: str) -> Optional[dict]:
        """One client record as a copy, or None when the client_id is unknown."""
        with self._lock:
            record = self._clients.get(client_id)
        if record is None:
            return None
        copy = dict(record)
        copy["redirect_uris"] = list(record["redirect_uris"])
        return copy

    def client_count(self) -> int:
        """Live client count, for the registration-cap guard."""
        with self._lock:
            return len(self._clients)

    # -- authorization codes (in memory only; 60s TTL; single-use) -----------

    def purge_expired_codes(self) -> None:
        """Drop expired in-memory codes; called internally on issue/redeem."""
        now = time.time()
        with self._lock:
            expired = [
                code for code, record in self._codes.items() if now >= record["expires_at"]
            ]
            for code in expired:
                del self._codes[code]

    def issue_code(
        self, client_id: str, redirect_uri: str, username: str, scope: str, code_challenge: str
    ) -> str:
        """Mint a single-use authorization code bound to the consented request."""
        with self._lock:
            self.purge_expired_codes()
            code = secrets.token_urlsafe(32)
            self._codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "username": username,
                "scope": scope,
                "code_challenge": code_challenge,
                "expires_at": time.time() + CODE_TTL_SECONDS,
            }
        logger.info("Issued authorization code for %r (client %r)", username, client_id)
        return code

    def redeem_code(
        self, code: str, client_id: str, redirect_uri: str, code_verifier: str
    ) -> Optional[dict]:
        """Consume an authorization code; ``{"username", "scope"}`` or None.

        The code is deleted on ANY redemption attempt (pop first), so an
        intercepted-and-replayed code is dead even when the first attempt
        failed PKCE. Every failure — unknown, expired, wrong client, wrong
        redirect_uri, PKCE mismatch — returns the same None: the token
        endpoint reports a constant ``invalid_grant`` with no oracle detail.
        """
        with self._lock:
            self.purge_expired_codes()
            record = self._codes.pop(code, None) if code else None
        if record is None:
            return None
        if time.time() >= record["expires_at"]:
            return None
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            return None
        if not isinstance(code_verifier, str) or not code_verifier:
            return None
        if not hmac.compare_digest(_pkce_s256(code_verifier), record["code_challenge"]):
            return None
        return {"username": record["username"], "scope": record["scope"]}

    # -- refresh tokens (persisted as sha256 hashes; rotated on use) ---------

    def issue_refresh(self, username: str, client_id: str) -> str:
        """Mint a refresh token; only its sha256 is persisted. Returns the raw value."""
        with self._lock:
            raw = self._store_refresh_locked(username, client_id)
            self._save_atomic()
        logger.info("Issued refresh token for %r (client %r)", username, client_id)
        return raw

    def rotate_refresh(
        self, raw_token: str, expected_client_id: str, min_created_at: float = 0.0
    ) -> Optional[Tuple[str, dict]]:
        """Consume ``raw_token`` and issue a fresh one; None on any rejection.

        The presented token is deleted BEFORE anything else, so replaying an
        already-rotated token fails instead of double-spending it. All
        validation happens BEFORE the replacement is minted, so a rejected
        rotation (unknown token, wrong client_id, or a record older than
        ``min_created_at`` — the account's revocation epoch) leaves nothing
        behind: no orphaned replacement token is persisted.
        """
        if not isinstance(raw_token, str) or not raw_token:
            return None
        with self._lock:
            record = self._refresh.pop(_hash_refresh_token(raw_token), None)
            if record is None:
                return None
            if record.get("client_id") != expected_client_id:
                return None
            if float(record.get("created_at", 0.0)) < min_created_at:
                return None
            new_raw = self._store_refresh_locked(record["username"], record["client_id"])
            self._save_atomic()
        return new_raw, dict(record)

    def _store_refresh_locked(self, username: str, client_id: str) -> str:
        """Caller holds the lock. Enforce the per-username cap (drop oldest)."""
        same_user = sorted(
            (
                (token_hash, record)
                for token_hash, record in self._refresh.items()
                if record.get("username") == username
            ),
            key=lambda item: float(item[1].get("created_at", 0.0)),
        )
        overflow = len(same_user) - MAX_REFRESH_TOKENS_PER_USER + 1
        for token_hash, _record in same_user[: max(overflow, 0)]:
            del self._refresh[token_hash]
        raw = secrets.token_urlsafe(32)
        self._refresh[_hash_refresh_token(raw)] = {
            "username": username,
            "client_id": client_id,
            "created_at": time.time(),
        }
        return raw


def _consent_page(client_name: str, values: Dict[str, str], csrf_token: str) -> str:
    """Minimal consent card, styled like the portal's login page (main._page).

    Every original authorize query parameter is echoed back as a hidden input
    so the POST re-validates the exact request the user consented to. The card
    shows the redirect destination's HOST (not just the attacker-chosen client
    name): with open dynamic registration the name alone is a phishing
    primitive, the host is where the access token actually goes.
    """
    redirect_host = urlparse(values.get("redirect_uri", "")).hostname or "(unknown)"
    hidden = "".join(
        f'\n  <input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
        for name, value in values.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anki MCP - Authorize client</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0;
         display: flex; justify-content: center; padding-top: 10vh; }}
  main {{ background: #fff; border: 1px solid #ddd; border-radius: 6px;
         padding: 2rem; width: 24rem; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem; }}
  p {{ word-break: break-all; }}
  form {{ margin-top: 1rem; display: flex; gap: 0.5rem; }}
  button {{ padding: 0.45rem 1rem; cursor: pointer; }}
</style>
</head>
<body>
<main>
<h1>Authorize client</h1>
<p><strong>{html.escape(client_name)}</strong> requests access to your Anki collection.</p>
<p>The access token will be delivered to: <strong>{html.escape(redirect_host)}</strong></p>
<form method="post" action="/oauth/authorize">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">{hidden}
  <button type="submit" name="decision" value="allow">Allow</button>
  <button type="submit" name="decision" value="deny">Deny</button>
</form>
</main>
</body>
</html>"""


def add_oauth_routes(app, store: OAuthStore, user_store: UserStore, settings: Settings) -> None:
    """Register the OAuth discovery/registration/authorize/token routes.

    Handlers close over THIS call's ``store``/``user_store``/``settings``
    (the same capture discipline create_app applies to its portal handlers),
    so a later create_app() rebind of the module globals cannot reach into
    an already-serving app.
    """

    def _param(params: Mapping, name: str) -> str:
        """A stringified form/query parameter, or "" when absent (never "None")."""
        value = params.get(name)
        return str(value) if value is not None else ""

    def _session_username(request: Request) -> Optional[str]:
        """The portal session's username, or None.

        Same posture as the portal's get_session_user (main.py): signed-cookie
        validity alone is not enough — the account must still exist, and the
        cookie must postdate the account's session-revocation epoch, so a
        cookie captured before a password reset cannot drive the consent flow
        (the stale-session-to-code-mint chain).
        """
        session_value = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_value:
            return None
        username = verify_session(settings.session_secret, session_value)
        if username is None:
            return None
        issued_at = session_issued_before(session_value)
        if issued_at is None:
            return None
        user = user_store.get_user(username)
        if user is None or issued_at < user.sessions_invalid_before:
            return None
        return username

    def _validate_authorize(params: Mapping):
        """Shared GET/POST authorize validation; returns (client, values) or (None, error).

        Failures are plain 400s and NEVER a redirect: echoing an unvalidated
        redirect_uri would turn the endpoint into an open redirector.
        """
        client_id = _param(params, "client_id")
        client = store.get_client(client_id)
        if client is None:
            return None, PlainTextResponse("unknown client", status_code=400)
        redirect_uri = _param(params, "redirect_uri")
        if redirect_uri not in client["redirect_uris"]:
            return None, PlainTextResponse(
                "redirect_uri is not registered for this client", status_code=400
            )
        if _param(params, "response_type") != "code":
            return None, PlainTextResponse("unsupported response_type", status_code=400)
        code_challenge = _param(params, "code_challenge")
        if not code_challenge or _param(params, "code_challenge_method") != "S256":
            return None, PlainTextResponse("PKCE S256 code_challenge is required", status_code=400)
        values = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            # Scope is coerced, not echoed: this server issues exactly one
            # scope, so a client requesting e.g. "admin openid" must not get
            # it back as a privilege claim.
            "scope": SCOPE,
            "state": _param(params, "state"),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return client, values

    # -- discovery -------------------------------------------------------------

    async def protected_resource_metadata(request: Request) -> JSONResponse:
        """RFC 9728: this is the protected resource; here is its authorization server."""
        base = public_base_url(request)
        return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})

    async def authorization_server_metadata(request: Request) -> JSONResponse:
        """The authorization server's metadata (base URL is proxy-header aware)."""
        base = public_base_url(request)
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": [SCOPE],
            }
        )

    # -- dynamic client registration (RFC 7591) ---------------------------------

    async def dynamic_register(request: Request) -> JSONResponse:
        # Pre-auth endpoint: cap the body before JSON parsing allocates for it.
        if _body_too_large(request):
            return JSONResponse({"error": "invalid_request", "error_description": "body too large"}, status_code=413)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or not _valid_redirect_uris(body.get("redirect_uris")):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        client_name = body.get("client_name")
        if not isinstance(client_name, str) or len(client_name) > MAX_CLIENT_NAME_CHARS:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        # Unauthenticated growth bound: refuse once the store is at capacity.
        if store.client_count() >= MAX_CLIENTS:
            logger.warning("OAuth client registration refused: at capacity (%d)", MAX_CLIENTS)
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        record = store.register_client(
            client_name,
            [str(uri) for uri in body["redirect_uris"]],
        )
        return JSONResponse(
            {
                "client_id": record["client_id"],
                "client_name": record["client_name"],
                "redirect_uris": record["redirect_uris"],
                "token_endpoint_auth_method": record["token_endpoint_auth_method"],
            },
            status_code=201,
        )

    # -- authorize (browser consent; portal session required) -------------------

    async def authorize_get(request: Request) -> Response:
        client, values_or_error = _validate_authorize(request.query_params)
        if client is None:
            return values_or_error
        username = _session_username(request)
        if username is None:
            # The portal login is the front door. The full authorize URL rides
            # along as a validated `next` parameter so the post-login redirect
            # lands the user back on this consent flow instead of the portal
            # home (the claude.ai-initiated navigation is cross-site, so the
            # session cookie may not even have been sent here).
            target = request.url.path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=302)
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_token = issue_csrf(session_value)
        return HTMLResponse(_consent_page(str(client.get("client_name", "")), values_or_error, csrf_token))

    async def authorize_post(request: Request) -> Response:
        # Pre-auth-reachable endpoint (the browser may post here with only a
        # stale cookie): cap the body before the form parser allocates for it.
        if _body_too_large(request):
            return PlainTextResponse("Request body too large.", status_code=413)
        session_value = request.cookies.get(SESSION_COOKIE_NAME, "")
        username = _session_username(request)
        if username is None:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not verify_csrf(session_value, _param(form, "csrf_token")):
            return PlainTextResponse("Invalid or missing CSRF token.", status_code=403)
        client, values_or_error = _validate_authorize(form)
        if client is None:
            return values_or_error
        values = values_or_error
        redirect_params: Dict[str, str] = {}
        if values["state"]:
            redirect_params["state"] = values["state"]
        if _param(form, "decision") != "allow":
            logger.info("OAuth consent denied by %r (client %r)", username, values["client_id"])
            redirect_params = {"error": "access_denied", **redirect_params}
        else:
            logger.info("OAuth consent granted by %r (client %r)", username, values["client_id"])
            code = store.issue_code(
                values["client_id"],
                values["redirect_uri"],
                username,
                values["scope"],
                values["code_challenge"],
            )
            redirect_params = {"code": code, **redirect_params}
        return RedirectResponse(
            _append_redirect_params(values["redirect_uri"], redirect_params), status_code=302
        )

    # -- token endpoint (PKCE codes and rotating refresh tokens) ----------------

    _TOKEN_NOT_VIEWABLE = (
        "MCP token not viewable; regenerate it in the portal, then reconnect"
    )

    async def token_endpoint(request: Request) -> JSONResponse:
        # Pre-auth endpoint (that is the point of OAuth): cap the body before
        # the form parser allocates or spools multipart file parts for it.
        if _body_too_large(request):
            return JSONResponse({"error": "invalid_request", "error_description": "body too large"}, status_code=413)
        form = await request.form()
        grant_type = _param(form, "grant_type")
        if grant_type == "authorization_code":
            return await _token_authorization_code(form)
        if grant_type == "refresh_token":
            return await _token_refresh(form)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _token_authorization_code(form: Mapping) -> JSONResponse:
        client_id = _param(form, "client_id")
        redeemed = store.redeem_code(
            _param(form, "code"),
            client_id,
            _param(form, "redirect_uri"),
            _param(form, "code_verifier"),
        )
        if redeemed is None:
            logger.warning("OAuth code redemption failed (client %r)", client_id)
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        username = redeemed["username"]
        raw_token = user_store.peek_token(username)
        if not raw_token:
            # Legacy record without token_plaintext: the raw token was never
            # stored, so only regeneration (portal) can unblock the connector.
            return JSONResponse(
                {"error": "server_error", "error_description": _TOKEN_NOT_VIEWABLE},
                status_code=400,
            )
        logger.info("OAuth access token issued to client %r for %r", client_id, username)
        return JSONResponse(
            {
                "access_token": raw_token,
                "token_type": "Bearer",
                "scope": redeemed["scope"],
                "refresh_token": store.issue_refresh(username, client_id),
                # Deliberately NO expires_in: the MCP token has no expiry.
            }
        )

    async def _token_refresh(form: Mapping) -> JSONResponse:
        client_id = _param(form, "client_id")
        raw_refresh = _param(form, "refresh_token")
        rotated = store.rotate_refresh(raw_refresh, client_id)
        if rotated is not None:
            # Grant eviction: the account's revocation epochs (password reset
            # via sessions_invalid_before, token regeneration via
            # tokens_invalid_before) gate the already-burned grant, so a
            # leaked connector grant dies at its next refresh instead of
            # self-renewing forever. The username lives inside the hashed
            # refresh record, so rotation runs first and this check follows;
            # rotate_refresh burned the presented token either way.
            new_refresh, record = rotated
            user = user_store.get_user(record["username"])
            if user is None or float(record.get("created_at", 0.0)) < max(
                user.sessions_invalid_before, user.tokens_invalid_before
            ):
                rotated = None
        if rotated is None:
            logger.warning("OAuth refresh token rejected (client %r)", client_id)
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        new_refresh, record = rotated
        username = record["username"]
        raw_token = user_store.peek_token(username)
        if not raw_token:
            return JSONResponse(
                {"error": "server_error", "error_description": _TOKEN_NOT_VIEWABLE},
                status_code=400,
            )
        logger.info("OAuth refresh token rotated for %r (client %r)", username, client_id)
        return JSONResponse(
            {
                "access_token": raw_token,
                "token_type": "Bearer",
                "scope": SCOPE,
                "refresh_token": new_refresh,
            }
        )

    app.add_route(
        "/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]
    )
    app.add_route(
        "/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]
    )
    app.add_route("/register", dynamic_register, methods=["POST"])
    app.add_route("/oauth/authorize", authorize_get, methods=["GET"])
    app.add_route("/oauth/authorize", authorize_post, methods=["POST"])
    app.add_route("/oauth/token", token_endpoint, methods=["POST"])
