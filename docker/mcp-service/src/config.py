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


def _env_int_min(name: str, default: int, minimum: int) -> int:
    value = _env_int(name, default)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got: {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


def _is_placeholder(value: str) -> bool:
    return value.strip().lower().startswith(PLACEHOLDER_PREFIX)


@dataclass(frozen=True)
class Settings:
    mcp_host: str = field(default_factory=lambda: _env_str("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: _env_int("MCP_PORT", 8000))
    mcp_path: str = field(default_factory=lambda: _env_str("MCP_PATH", "/mcp"))
    maintenance_mode: bool = field(default_factory=lambda: _env_bool("MAINTENANCE_MODE", False))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())
    # Multi-user settings (Task 1 auth model):
    session_secret: str = field(default_factory=lambda: _env_str("SESSION_SECRET", "").strip())
    data_dir: str = field(default_factory=lambda: _env_str("DATA_DIR", "/data") or "/data")
    users_file: str = field(default_factory=lambda: _env_str("USERS_FILE", ""))
    admin_username: str = field(default_factory=lambda: _env_str("ADMIN_USERNAME", "").strip())
    admin_password: str = field(default_factory=lambda: _env_str("ADMIN_PASSWORD", ""))
    idle_timeout_minutes: int = field(default_factory=lambda: _env_int_min("IDLE_TIMEOUT_MINUTES", 15, 1))
    max_open_collections: int = field(default_factory=lambda: _env_int_min("MAX_OPEN_COLLECTIONS", 5, 1))
    max_concurrent_syncs: int = field(default_factory=lambda: _env_int_min("MAX_CONCURRENT_SYNCS", 2, 1))
    backup_retention: int = field(default_factory=lambda: _env_int_min("BACKUP_RETENTION", 5, 1))

    def __post_init__(self):
        # Derive users_file from data_dir instead of double-reading the
        # environment; an explicit USERS_FILE always wins.
        if not self.users_file:
            object.__setattr__(self, "users_file", os.path.join(self.data_dir, "users.json"))


def validate_deployment_config(settings: Settings) -> List[str]:
    """Return fatal configuration problems. An empty list means safe to start.

    Auth is per-user (MCP tokens live in the user store) and needs no legacy
    configuration. SESSION_SECRET is fail-closed — without it the service must
    not start.
    """
    problems: List[str] = []
    if not settings.session_secret:
        problems.append(
            "SESSION_SECRET is not set. The service refuses to start without a session "
            "secret (per-user sessions cannot be signed). Generate one with: openssl rand -hex 24"
        )
    elif _is_placeholder(settings.session_secret):
        problems.append(
            "SESSION_SECRET is still the .env.example placeholder. "
            "Generate a real secret with: openssl rand -hex 24"
        )
    return problems


def get_settings() -> Settings:
    """Return an immutable snapshot of configuration, read from the environment at call time."""
    return Settings()
