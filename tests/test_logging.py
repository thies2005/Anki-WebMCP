"""Unit tests for secret redaction and logging utilities."""

import logging
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
