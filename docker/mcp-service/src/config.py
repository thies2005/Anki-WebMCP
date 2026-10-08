"""Application configuration for the Anki MCP Service."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    anki_connect_url: str = os.getenv("ANKI_CONNECT_URL", "http://anki-worker:8765").rstrip("/")
    anki_connect_api_key: str = os.getenv("ANKI_CONNECT_API_KEY", "").strip()
    mcp_auth_token: str = os.getenv("MCP_AUTH_TOKEN", "").strip()
    mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
    mcp_port: int = int(os.getenv("MCP_PORT", "8000"))
    mcp_path: str = os.getenv("MCP_PATH", "/mcp")
    idempotency_ledger_path: str = os.getenv("IDEMPOTENCY_LEDGER_PATH", "/data/idempotency_ledger.json")
    maintenance_mode: bool = os.getenv("MAINTENANCE_MODE", "false").lower() in ("true", "1", "yes")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30.0"))
    sync_timeout_seconds: float = float(os.getenv("SYNC_TIMEOUT_SECONDS", "60.0"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def is_auth_enabled(self) -> bool:
        return bool(self.mcp_auth_token)


def get_settings() -> Settings:
    """Return an immutable snapshot of configuration from environment variables."""
    return Settings()
