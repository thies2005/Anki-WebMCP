"""Cross-cutting regression tests for the run-1 audit fixes (fix wave A3).

Covers the seams between the individual fixes:

* the idempotency_key length bound enforced at BOTH layers (the pydantic input
  model, whose ValidationError the MCP/HTTP layer surfaces as 422, and the
  ledger's own MAX_KEY_LEN guard), and
* one-line-safe logging of client-controlled values through SyncManager's
  replay path (a newline in the key must produce exactly ONE escaped log line,
  not a forged second line).
"""

import logging

import pytest
import pytest_asyncio
from pydantic import ValidationError

from src.collections import CollectionRegistry
from src.logging_util import RedactingFormatter
from src.ledger import MAX_KEY_LEN
from src.models import AddNotesInput, NoteInput
from src.sync_manager import SyncManager


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data_dir (no collection needed)."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, None)
    yield reg, manager
    await reg.close_all()


def _valid_notes():
    return [
        NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "q", "Back": "a"})
    ]


# -- two-layer key bound (models.py max_length <-> ledger.MAX_KEY_LEN) -------------


def test_add_notes_input_idempotency_key_max_length():
    """256 characters pass; longer keys fail input validation (HTTP 422 path)."""
    AddNotesInput(notes=_valid_notes(), idempotency_key="k" * 256)
    with pytest.raises(ValidationError):
        AddNotesInput(notes=_valid_notes(), idempotency_key="k" * 257)


def test_add_notes_input_idempotency_key_optional():
    assert AddNotesInput(notes=_valid_notes()).idempotency_key is None


def _declared_max_length(field_info) -> int:
    """The declared max_length, wherever the installed pydantic keeps it."""
    bound = getattr(field_info, "max_length", None)
    if bound is not None:
        return bound
    for meta in field_info.metadata:
        bound = getattr(meta, "max_length", None)
        if bound is not None:
            return bound
    raise AssertionError("idempotency_key declares no max_length constraint")


def test_model_bound_matches_ledger_guard():
    """The two layers enforce the SAME bound (models.py documents the pairing)."""
    info = AddNotesInput.model_fields["idempotency_key"]
    assert _declared_max_length(info) == MAX_KEY_LEN == 256


# -- one-line-safe client-controlled logs through the mutation replay path --------


@pytest.mark.asyncio
async def test_execute_mutation_committed_replay_log_is_single_line(env, caplog):
    reg, manager = env
    caplog.set_level(logging.INFO)
    # Payload within the call site's 64-char log truncation.
    key = "evil\n2026-01-01 [ERROR] FORGED LINE"
    await reg.ledger("alice").record_success(key, {"count": 1})

    applied = []

    async def mutation():
        applied.append(True)
        return {}

    res = await manager.execute_mutation("alice", "add_notes", mutation, idempotency_key=key)
    assert res.success is True  # replay of the cached committed record
    assert res.data == {"count": 1}
    assert applied == []  # the mutation was NOT re-applied

    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.sync_manager" and "Idempotency hit" in r.getMessage()
    ]
    assert len(recs) == 1
    line = RedactingFormatter("%(message)s").format(recs[0])
    assert "\n" not in line  # exactly ONE log line
    assert "\\n" in line  # newline repr-escaped at the call site
    assert "FORGED LINE" in line  # content preserved inline, escaped


@pytest.mark.asyncio
async def test_execute_mutation_failed_replay_log_is_single_line(env, caplog):
    reg, manager = env
    caplog.set_level(logging.INFO)
    key = "bad\n2026-01-01 [ERROR] FORGED FAILURE LINE"
    await reg.ledger("alice").record_failure(key, "previous error")

    async def mutation():
        raise AssertionError("mutation must not run on a failed-record replay")

    res = await manager.execute_mutation("alice", "add_notes", mutation, idempotency_key=key)
    assert res.success is False
    assert res.error == "previous error"

    recs = [
        r for r in caplog.records
        if r.name == "anki_mcp.sync_manager" and "Idempotency hit" in r.getMessage()
    ]
    assert len(recs) == 1
    line = RedactingFormatter("%(message)s").format(recs[0])
    assert "\n" not in line
    assert "\\n" in line


# -- production pipeline end-to-end: escaped record, single line -------------------


def test_production_formatter_single_line_for_injected_key():
    """The production StreamHandler + RedactingFormatter pipeline emits a single
    line for a log call whose client-controlled value carried a newline AND an
    ANSI escape: the %r call site repr-escapes the newline, the formatter
    escapes any remaining control characters."""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        RedactingFormatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    test_logger = logging.getLogger("anki_mcp.test_audit")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False  # keep the root handlers out of this check
    try:
        # Same shape as the real call sites: "for key %r" (no key=value form,
        # which the credential redactor would rewrite).
        test_logger.info("Idempotency hit for key %r", "a\n\x1b[31mforged\x07")
    finally:
        test_logger.removeHandler(handler)
        test_logger.propagate = True

    emitted = stream.getvalue()
    assert emitted.endswith("\n")  # handler's one trailing line break
    assert "\n" not in emitted[:-1]  # nothing else split the record
    assert "\\n" in emitted
    assert "\\x1b" in emitted and "\\x07" in emitted
    assert "\x1b" not in emitted and "\x07" not in emitted
    assert "forged" in emitted
