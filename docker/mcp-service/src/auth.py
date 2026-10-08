"""Authentication middleware for HTTP / Streamable HTTP MCP endpoints."""

import hmac
import logging
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Settings, get_settings

logger = logging.getLogger("anki_mcp.auth")


class AuthMiddleware(BaseHTTPMiddleware):
    """Protects MCP endpoints with Bearer token / API key authentication.

    Authentication is mandatory: constructing this middleware without a
    configured token raises, so the endpoint can never serve unauthenticated.
    """

    def __init__(self, app, settings: Optional[Settings] = None):
        super().__init__(app)
        self.settings = settings or get_settings()
        if not self.settings.mcp_auth_token:
            raise ValueError(
                "AuthMiddleware requires a non-empty MCP_AUTH_TOKEN. "
                "Refusing to serve an unauthenticated MCP endpoint."
            )

    async def dispatch(self, request: Request, call_next) -> Response:
        # Permit health checks and CORS preflights without authentication
        if request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)

        provided_token = self._extract_token(request)
        if not provided_token:
            logger.warning("Rejected unauthenticated request to %s: missing token", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Missing authentication token (Authorization: Bearer <token> or X-API-Key)"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not self._tokens_match(provided_token, self.settings.mcp_auth_token):
            logger.warning("Rejected request to %s: invalid token provided", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Invalid authentication token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)

    @staticmethod
    def _tokens_match(provided: str, expected: str) -> bool:
        """Constant-time comparison on UTF-8 bytes; never raises on odd input."""
        try:
            return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
        except UnicodeEncodeError:
            return False

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
