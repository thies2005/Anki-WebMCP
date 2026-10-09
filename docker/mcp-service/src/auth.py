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

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Settings, get_settings
from .users import UserStore

logger = logging.getLogger("anki_mcp.auth")

# Web routes authenticate with session cookies and run their own CSRF checks
# (Task 2/4), so the bearer-token middleware must not guard them. Kept as an
# explicit allowlist so a newly added route is protected by default.
WEB_AUTH_PATHS = frozenset(
    {
        "/",
        "/login",
        "/logout",
        "/portal/regenerate-token",
        "/portal/credentials",
        "/portal/sync",
        "/admin",
        "/admin/create-user",
        "/admin/reset-password",
        "/admin/regenerate-token",
        "/admin/set-role",
        "/admin/delete-user",
    }
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Protects the MCP endpoint with per-user token authentication.

    Authentication is mandatory and has no configuration switch: constructing
    without a :class:`UserStore` raises, so the endpoint can never serve
    unauthenticated (the legacy single-token middleware's fail-closed
    construction contract, carried over).
    """

    def __init__(self, app, store: UserStore, settings: Optional[Settings] = None):
        super().__init__(app)
        if store is None:
            raise ValueError(
                "AuthMiddleware requires a UserStore. Refusing to serve an "
                "MCP endpoint that could never authenticate anybody."
            )
        self.store = store
        self.settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next) -> Response:
        # Permit health checks, CORS preflights, and the cookie-authenticated
        # web routes without bearer-token authentication.
        if request.url.path == "/health" or request.method == "OPTIONS" or request.url.path in WEB_AUTH_PATHS:
            return await call_next(request)

        provided_token = self._extract_token(request)
        if not provided_token:
            logger.warning("Rejected unauthenticated request to %s: missing token", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Missing authentication token (Authorization: Bearer <token> or X-API-Key)"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # sha256 + in-memory scan (no I/O, no scrypt): safe to run inline.
        user = self.store.authenticate_token(provided_token)
        if user is None:
            logger.warning("Rejected request to %s: invalid token provided", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Invalid authentication token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Travel with the ASGI scope (scope["state"]) into the MCP SDK's
        # session task; tool wrappers read it via the tool Context.
        request.state.username = user.username
        request.state.user = user
        return await call_next(request)

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
