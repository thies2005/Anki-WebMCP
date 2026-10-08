"""Application configuration for the Anki MCP Service."""

import os
from dataclasses import dataclass, field
from typing import List

PLACEHOLDER_PREFIX = "replace-with"


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got: {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


def _is_placeholder(value: str) -> bool:
    return value.strip().lower().startswith(PLACEHOLDER_PREFIX)


@dataclass(frozen=True)
class Settings:
    anki_connect_url: str = field(default_factory=lambda: _env_str("ANKI_CONNECT_URL", "http://anki-worker:8765").rstrip("/"))
    anki_connect_api_key: str = field(default_factory=lambda: _env_str("ANKI_CONNECT_API_KEY", "").strip())
    mcp_auth_token: str = field(default_factory=lambda: _env_str("MCP_AUTH_TOKEN", "").strip())
    mcp_host: str = field(default_factory=lambda: _env_str("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: _env_int("MCP_PORT", 8000))
    mcp_path: str = field(default_factory=lambda: _env_str("MCP_PATH", "/mcp"))
    idempotency_ledger_path: str = field(default_factory=lambda: _env_str("IDEMPOTENCY_LEDGER_PATH", "/data/idempotency_ledger.json"))
    maintenance_mode: bool = field(default_factory=lambda: _env_bool("MAINTENANCE_MODE", False))
    request_timeout_seconds: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT_SECONDS", 30.0))
    sync_timeout_seconds: float = field(default_factory=lambda: _env_float("SYNC_TIMEOUT_SECONDS", 60.0))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())

    @property
    def is_auth_enabled(self) -> bool:
        return bool(self.mcp_auth_token)


def validate_deployment_config(settings: Settings) -> List[str]:
    """Return fatal configuration problems. An empty list means safe to start."""
    problems: List[str] = []
    if not settings.mcp_auth_token:
        problems.append(
            "MCP_AUTH_TOKEN is not set. The MCP endpoint refuses to start unauthenticated. "
            "Generate one with: openssl rand -hex 24"
        )
    elif _is_placeholder(settings.mcp_auth_token):
        problems.append(
            "MCP_AUTH_TOKEN is still the .env.example placeholder. "
            "Generate a real secret with: openssl rand -hex 24"
        )
    if not settings.anki_connect_api_key:
        problems.append(
            "ANKI_CONNECT_API_KEY is not set. AnkiConnect requires a shared API key. "
            "Generate one with: openssl rand -hex 24"
        )
    elif _is_placeholder(settings.anki_connect_api_key):
        problems.append(
            "ANKI_CONNECT_API_KEY is still the .env.example placeholder. "
            "Generate a real secret with: openssl rand -hex 24"
        )
    return problems


def get_settings() -> Settings:
    """Return an immutable snapshot of configuration, read from the environment at call time."""
    return Settings()
