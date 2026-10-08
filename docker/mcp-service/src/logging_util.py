"""Logging utility with automatic secret and token redaction."""

import logging
import re
from typing import Any

# Sensitive patterns to redact
_PATTERNS = [
    # Authorization header: Bearer <token>
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE), r"\1[REDACTED]"),
    # Basic auth
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{6,}", re.IGNORECASE), r"\1[REDACTED]"),
    # Key-value pairs: "key": "...", key=..., token=..., apiKey=...
    (re.compile(r'(["\']?(?:api[_-]?key|key|token|auth|password|secret|hkey)["\']?\s*[:=]\s*["\']?)([^"\'\s,;&]+)(["\']?)', re.IGNORECASE), r'\1[REDACTED]\3'),
]

_SENSITIVE_KEYS = {
    "key", "apikey", "api_key", "token", "auth", "password", "secret",
    "hkey", "authorization", "x-api-key", "mcp_auth_token", "anki_connect_api_key",
}


def redact_string(message: str) -> str:
    """Redact sensitive patterns from a string."""
    if not isinstance(message, str):
        message = str(message)
    for pattern, replacement in _PATTERNS:
        message = pattern.sub(replacement, message)
    return message


def sanitize_data(data: Any) -> Any:
    """Recursively mask sensitive values in dicts, lists, and primitives."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_data(v)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    elif isinstance(data, str):
        return redact_string(data)
    return data


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts sensitive credentials from log outputs."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_string(original)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure root and package logging with redacting formatter."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    formatter = RedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Adjust external noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logger = logging.getLogger("anki_mcp")
    logger.setLevel(log_level)
    return logger
