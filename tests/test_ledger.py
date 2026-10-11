"""Unit tests for persistent IdempotencyLedger."""

import json
import logging
import os

import pytest

from src.ledger import MAX_KEY_LEN, IdempotencyLedger
from src.logging_util import RedactingFormatter


@pytest.mark.asyncio
async def test_ledger_record_and_get(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file)

    # Empty key check
    assert await ledger.get("non-existent") is None

    # Record success
    await ledger.record_success("op-123", {"note_id": 42})
    entry = await ledger.get("op-123")
    assert entry is not None
    assert entry["status"] == "committed"
    assert entry["data"] == {"note_id": 42}
    assert "timestamp" in entry


@pytest.mark.asyncio
async def test_ledger_record_failure(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file)

    await ledger.record_failure("op-fail", "Anki rejected note")
    entry = await ledger.get("op-fail")
    assert entry is not None
    assert entry["status"] == "failed"
    assert entry["error"] == "Anki rejected note"


@pytest.mark.asyncio
async def test_ledger_persistence_across_restarts(temp_ledger_file):
    ledger1 = IdempotencyLedger(temp_ledger_file)
    await ledger1.record_success("op-persist", {"saved": True})

    # Read raw JSON file to verify it was flushed
    assert os.path.exists(temp_ledger_file)
    with open(temp_ledger_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "op-persist" in data
    assert data["op-persist"]["status"] == "committed"

    # Instantiate new ledger object pointing to same file (simulating service restart)
    ledger2 = IdempotencyLedger(temp_ledger_file)
    entry = await ledger2.get("op-persist")
    assert entry is not None
    assert entry["data"] == {"saved": True}


@pytest.mark.asyncio
async def test_ledger_evicts_oldest_entries_beyond_cap(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file, max_entries=3)
    for i in range(5):
        await ledger.record_success(f"op-{i}", {"i": i})

    assert await ledger.get("op-0") is None
    assert await ledger.get("op-1") is None
    assert await ledger.get("op-2") is not None
    assert await ledger.get("op-4") is not None

    with open(temp_ledger_file, "r", encoding="utf-8") as f:
        assert len(json.load(f)) == 3


# -- per-entry key-length bound (MAX_KEY_LEN) -------------------------------------


@pytest.mark.asyncio
async def test_record_success_rejects_overlong_key(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file)
    # Boundary: exactly MAX_KEY_LEN characters is accepted.
    await ledger.record_success("k" * MAX_KEY_LEN, {"ok": True})
    assert await ledger.get("k" * MAX_KEY_LEN) is not None
    # Anything longer is rejected before any ledger write.
    with pytest.raises(ValueError, match=str(MAX_KEY_LEN)):
        await ledger.record_success("k" * (MAX_KEY_LEN + 1), {})
    assert await ledger.get("k" * (MAX_KEY_LEN + 1)) is None


@pytest.mark.asyncio
async def test_record_failure_rejects_overlong_key(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file)
    await ledger.record_failure("k" * MAX_KEY_LEN, "err")
    assert await ledger.get("k" * MAX_KEY_LEN) is not None
    with pytest.raises(ValueError, match=str(MAX_KEY_LEN)):
        await ledger.record_failure("k" * (MAX_KEY_LEN + 1), "err")
    assert await ledger.get("k" * (MAX_KEY_LEN + 1)) is None


@pytest.mark.asyncio
async def test_overlong_key_never_persisted_to_disk(temp_ledger_file):
    ledger = IdempotencyLedger(temp_ledger_file)
    with pytest.raises(ValueError):
        await ledger.record_success("k" * (MAX_KEY_LEN + 1), {})
    # The rejected write never created/persisted a ledger file: it is still
    # the fixture's empty placeholder.
    assert os.path.getsize(temp_ledger_file) == 0


# -- log hygiene for client-controlled keys (CWE-117) -----------------------------


@pytest.mark.asyncio
async def test_ledger_key_log_is_single_line(temp_ledger_file, caplog):
    """A newline inside the key cannot split the record: %r escapes it.

    The key payload stays within the call site's 64-char log truncation (the
    truncation itself is pinned by test_ledger_key_log_is_truncated).
    """
    caplog.set_level(logging.INFO)
    ledger = IdempotencyLedger(temp_ledger_file)
    key = "evil\n2026-01-01 [ERROR] FORGED LINE"

    await ledger.record_success(key, {"count": 1})
    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.ledger" and "Recorded idempotency key" in r.getMessage()
    ]
    assert len(recs) == 1
    line = RedactingFormatter("%(message)s").format(recs[0])
    assert "\n" not in line  # exactly ONE log line
    assert "\\n" in line  # newline repr-escaped at the call site
    assert "FORGED LINE" in line  # content preserved inline, escaped

    caplog.clear()
    await ledger.record_failure(key, "err")
    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.ledger" and "Recorded idempotency key" in r.getMessage()
    ]
    assert len(recs) == 1
    line = RedactingFormatter("%(message)s").format(recs[0])
    assert "\n" not in line
    assert "\\n" in line


@pytest.mark.asyncio
async def test_ledger_key_log_is_truncated(temp_ledger_file, caplog):
    """Long (but legal) keys are truncated in the log record, stored in full."""
    caplog.set_level(logging.INFO)
    ledger = IdempotencyLedger(temp_ledger_file)
    key = "k" * MAX_KEY_LEN
    await ledger.record_success(key, {})
    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.ledger" and "Recorded idempotency key" in r.getMessage()
    ]
    assert len(recs) == 1
    assert len(recs[0].args[0]) == 64  # truncated for the log line...
    assert await ledger.get(key) is not None  # ...but stored in full
