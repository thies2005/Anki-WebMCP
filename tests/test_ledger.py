"""Unit tests for persistent IdempotencyLedger."""

import json
import os
import pytest
from src.ledger import IdempotencyLedger


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
