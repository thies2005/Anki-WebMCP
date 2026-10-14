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
    # Key-value pairs with a QUOTED value — "password": "my secret pass".
    # Audit F-13: these run BEFORE the bare-token pattern below, which only
    # matches up to the first whitespace and used to leave the tail of a
    # quoted secret ("[REDACTED] secret pass") in the log. Order matters: the
    # quoted patterns consume the whole value, so the bare pattern only ever
    # sees what they left (an already-redacted marker, idempotently).
    (re.compile(r'(["\']?(?:api[_-]?key|key|token|auth|password|secret|hkey)["\']?\s*[:=]\s*")([^"]*)(")', re.IGNORECASE), r'\1[REDACTED]\3'),
    (re.compile(r'(["\']?(?:api[_-]?key|key|token|auth|password|secret|hkey)["\']?\s*[:=]\s*\')([^\']*)(\')', re.IGNORECASE), r'\1[REDACTED]\3'),
    # Key-value pairs, bare unquoted value: key=..., token=..., apiKey=...
    (re.compile(r'(["\']?(?:api[_-]?key|key|token|auth|password|secret|hkey)["\']?\s*[:=]\s*["\']?)([^"\'\s,;&]+)(["\']?)', re.IGNORECASE), r'\1[REDACTED]\3'),
]

# C0/C1 control characters EXCEPT the structural newline (\n): NUL, BEL, ESC
# and CSI openers, CR, TAB, and the C1 range cannot be distinguished from
# terminal control sequences by log viewers, so they are escaped to visible
# \xNN form before redaction. Attacker-influenced strings must never reach the
# log stream with active escape bytes (ANSI/terminal log-forging, CWE-117);
# CR in particular rewrites the current terminal line. \n is preserved so
# traceback blocks keep their line structure; call sites logging client-
# controlled single-line values use %r so their newlines are repr-escaped and
# cannot split a record.
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

# Secret-bearing dict keys. Generic names cover every real secret this service
# handles (session_secret, admin_password, per-user MCP tokens, AnkiWeb
# credentials); the deleted legacy env-var names are NOT listed. Audit F-13
# adds the compound names the exact-match lookup used to miss: the OAuth
# grant material and the stored credential fields.
_SENSITIVE_KEYS = {
    "key", "apikey", "api_key", "token", "auth", "password", "secret",
    "hkey", "authorization", "x-api-key",
    "refresh_token", "access_token", "code_verifier", "ankiweb_password",
    "token_plaintext", "client_secret",
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
    """Redact credentials and neutralize active control characters.

    All C0/C1 control characters except ``\\n`` are escaped to ``\\xNN`` form
    (before redaction) so attacker-influenced strings cannot alter how log
    records render or parse (ANSI/terminal log forging). ``\\n`` survives so
    exception tracebacks keep their structure; single-line client values are
    logged via ``%r`` at the call sites, which repr-escapes their newlines.
    """

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        original = _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group(0)):02x}", original)
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
