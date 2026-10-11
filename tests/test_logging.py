"""Unit tests for secret redaction and logging utilities."""

import logging
import sys

from src.logging_util import RedactingFormatter, redact_string, sanitize_data


def test_redact_bearer_token():
    raw = "Header Authorization: Bearer my-secret-jwt-token-value-12345"
    redacted = redact_string(raw)
    assert "my-secret-jwt-token-value-12345" not in redacted
    assert "Bearer [REDACTED]" in redacted


def test_redact_api_key_json():
    raw = '{"action": "sync", "key": "supersecretkey999"}'
    redacted = redact_string(raw)
    assert "supersecretkey999" not in redacted
    assert '[REDACTED]' in redacted


def test_redact_url_query_token():
    raw = "http://localhost:8000/mcp?token=abc123xyz789"
    redacted = redact_string(raw)
    assert "abc123xyz789" not in redacted
    assert '[REDACTED]' in redacted


def test_sanitize_dict():
    data = {
        "deck": "Default",
        "api_key": "private_key",
        "nested": {
            "password": "secret_password",
            "safe_field": "hello world",
        },
        "tags": ["vocab", "spanish"],
    }
    sanitized = sanitize_data(data)
    assert sanitized["deck"] == "Default"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["password"] == "[REDACTED]"
    assert sanitized["nested"]["safe_field"] == "hello world"
    assert sanitized["tags"] == ["vocab", "spanish"]


def test_formatter():
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Connecting with key: secret12345 and Bearer mytoken8888",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert "secret12345" not in formatted
    assert "mytoken8888" not in formatted
    assert "[REDACTED]" in formatted


def _record(msg, exc_info=None):
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


# -- control-character neutralization (CWE-117 / ANSI log forging) --------------


def test_formatter_escapes_c0_c1_control_chars_except_newline():
    """ESC/BEL/CR/TAB and other C0/C1 bytes are neutralized to \\xNN text."""
    formatter = RedactingFormatter("%(message)s")
    raw = "a\x1bb\x07c\x0dd\x09e\x02f\x9bg"
    formatted = formatter.format(_record(f"path {raw}"))
    # No active control byte reaches the emitted record ...
    for ch in ("\x1b", "\x07", "\x0d", "\x09", "\x02", "\x9b"):
        assert ch not in formatted
    assert "\n" not in formatted  # record stays a single line
    # ... but the content survives in visible escaped form.
    assert "path a\\x1bb\\x07c\\x0dd\\x09e\\x02f\\x9bg" in formatted


def test_formatter_neutralizes_ansi_window_title_payload():
    """The audit's window-title-overwrite payload cannot render as a sequence."""
    formatter = RedactingFormatter("%(message)s")
    payload = "\x1b]0;SERVER-OWNED-WINDOW\x07\x1b[31m[ERROR] forged\x1b[0m"
    formatted = formatter.format(_record(f"unauthenticated request to /{payload} probe"))
    assert "\x1b" not in formatted
    assert "\x07" not in formatted
    assert "\\x1b" in formatted and "\\x07" in formatted


def test_formatter_preserves_newlines_for_tracebacks():
    """\\n survives (traceback structure) even though other control chars do not."""
    formatter = RedactingFormatter("%(message)s")
    try:
        raise ValueError("boom\tvalue")
    except ValueError:
        exc_info = sys.exc_info()
    formatted = formatter.format(_record("mutation failed", exc_info=exc_info))
    # Traceback keeps its line structure...
    assert "\n" in formatted
    assert "ValueError: boom" in formatted
    # ...while the TAB inside the exception text is escaped.
    assert "\\x09" in formatted
    assert "\t" not in formatted


def test_formatter_escapes_before_redaction():
    """Both layers apply to one record: control chars escaped AND creds redacted."""
    formatter = RedactingFormatter("%(message)s")
    formatted = formatter.format(
        _record(f"Bearer abcdef123456 path /\x1b[31mx\x07")
    )
    assert "abcdef123456" not in formatted
    assert "[REDACTED]" in formatted
    assert "\x1b" not in formatted and "\x07" not in formatted
    assert "\\x1b" in formatted
