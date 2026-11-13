"""Per-user token authentication middleware for the MCP endpoints.

Every MCP account carries its own randomly-generated token (managed by
:class:`src.users.UserStore`); the middleware resolves a presented token to
exactly one account via ``UserStore.authenticate_token`` and stashes the
authenticated username on ``request.state.username``. The MCP tool wrappers
read it back through the SDK's tool ``Context``
(``ctx.request_context.request.state``) — Starlette backs ``request.state``
with the shared ASGI ``scope["state"]`` dict, so the value survives into the
SDK's session task even though tool handlers execute outside the HTTP
request's task. See the Task 5 report for the verification trail.
"""

import logging
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings, get_settings
from .oauth import OAuthStore, public_base_url
from .users import UserStore

logger = logging.getLogger("anki_mcp.auth")

# OAuth 2.1 endpoints (src/oauth.py) are exempt as well: discovery metadata is
# public by design, /register mints only a client record, /oauth/authorize
# enforces the portal session cookie itself, and /oauth/token authenticates
# through PKCE-proven codes and rotating refresh tokens instead of bearer
# tokens. Kept as a separate named set so the route/exempt parity test can
# pin both groups against the registered route table.
OAUTH_AUTH_PATHS = frozenset(
    {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/register",
        "/oauth/authorize",
        "/oauth/token",
    }
)

# Web routes authenticate with session cookies and run their own CSRF checks
# (Task 2/4), so the bearer-token middleware must not guard them. Kept as an
# explicit allowlist so a newly added route is protected by default.
WEB_AUTH_PATHS = frozenset(
    {
        "/",
        "/login",
        "/logout",
        "/static/app.css",
        "/static/app.js",
        "/portal/password",
        "/portal/regenerate-token",
        "/portal/credentials",
        "/portal/credentials/disconnect",
        "/portal/credentials/test",
        "/portal/oauth/revoke",
        "/portal/sync",
        "/portal/backup/create",
        "/portal/backup/confirm",
        "/portal/backup/restore",
        "/portal/backup/download",
        "/portal/tools",
        "/portal/tools/disabled",
        "/admin",
        "/admin/create-user",
        "/admin/reset-password",
        "/admin/regenerate-token",
        "/admin/set-role",
        "/admin/delete-user",
        "/admin/tool-rules",
        "/admin/maintenance",
        *OAUTH_AUTH_PATHS,
    }
)


def _bearer_challenge(request: Request) -> str:
    """RFC 9728 WWW-Authenticate challenge for a rejected MCP request.

    OAuth-capable clients (claude.ai connectors) read ``resource_metadata``
    from this header to discover the authorization server that can mint them
    a token; plain API-key clients ignore the extra parameter harmlessly.
    """
    return (
        "Bearer resource_metadata="
        f'"{public_base_url(request)}/.well-known/oauth-protected-resource"'
    )


class AuthMiddleware:
    """Pure-ASGI authentication middleware for the MCP endpoint.

    Authentication is mandatory and has no configuration switch: constructing
    without a :class:`UserStore` raises, so the endpoint can never serve
    unauthenticated. Pure ASGI avoids ``BaseHTTPMiddleware`` stream buffering
    and per-request task overhead on SSE/streamable-HTTP traffic.
    """

    def __init__(
        self,
        app,
        store: UserStore,
        settings: Optional[Settings] = None,
        session_guard=None,
        oauth_store: Optional[OAuthStore] = None,
    ):
        if store is None:
            raise ValueError(
                "AuthMiddleware requires a UserStore. Refusing to serve an "
                "MCP endpoint that could never authenticate anybody."
            )
        self.app = app
        self.store = store
        self.settings = settings or get_settings()
        self.session_guard = session_guard
        self.oauth_store = oauth_store

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        if path == "/health" or request.method == "OPTIONS" or path in WEB_AUTH_PATHS:
            await self.app(scope, receive, send)
            return

        provided_token = self._extract_token(request)
        if not provided_token:
            logger.warning("Rejected unauthenticated request to %r: missing token", path)
            res = JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: Missing authentication token (Authorization: Bearer <token> or X-API-Key)"
                },
                headers={"WWW-Authenticate": _bearer_challenge(request)},
            )
            await res(scope, receive, send)
            return

        # 1. Personal static MCP token (constant-time sha256 scan).
        user = self.store.authenticate_token(provided_token)
        auth_binding = user.token_hash if user is not None else ""

        # 2. Expiring, per-grant revocable OAuth access token (when OAuth enabled).
        if user is None and self.oauth_store is not None:
            grant = self.oauth_store.authenticate_access_token(provided_token)
            if grant is not None:
                candidate = self.store.get_user(grant["username"])
                if candidate is not None:
                    epoch = max(
                        float(candidate.created_at or 0.0),
                        float(candidate.tokens_invalid_before or 0.0),
                        float(candidate.sessions_invalid_before or 0.0),
                    )
                    if float(grant.get("created_at") or 0.0) >= epoch:
                        user = candidate
                        auth_binding = f"oauth:{grant.get('grant_id', '')}"

        if user is None:
            logger.warning("Rejected request to %r: invalid token provided", path)
            res = JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Invalid authentication token"},
                headers={"WWW-Authenticate": _bearer_challenge(request)},
            )
            await res(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["username"] = user.username
        state["user"] = user
        state["auth_binding"] = auth_binding

        if self.session_guard is not None and path == self.session_guard.mcp_path:
            await self.session_guard.handle_asgi(scope, receive, send, self.app)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        # 1. Authorization: Bearer <token>
        auth_header = request.headers.get("Authorization")
        if auth_header:
            parts = auth_header.strip().split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1]

        # 2. X-API-Key: <token>
        api_key_header = request.headers.get("X-API-Key")
        if api_key_header:
            return api_key_header.strip()

        return None
