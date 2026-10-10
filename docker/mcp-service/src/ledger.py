"""Persistent idempotency ledger for operation deduplication across restarts.

The headless service runs one ledger PER USER, at
``<data_dir>/ledgers/<username>/ledger.json`` (managed by
:class:`src.collections.CollectionRegistry.ledger`). Each write mutation
carrying an ``idempotency_key`` follows a per-user, lock-serialized cycle:

    1. Check the key in this ledger; a committed record replays the cached
       result, a failed record rejects the retry (no re-apply).
    2. Apply the mutation locally under the user's collection lock.
    3. Commit the record to this ledger (flushed atomically to disk).

AnkiWeb synchronization is EXPLICIT and separate (``sync_collection`` / the
portal's Sync button) — mutations never auto-sync. If an explicit sync drops
mid-transfer with an unknown outcome, the outcome is surfaced as
``outcome_unknown`` and NO record is written for the key (ambiguous-outcome
no-record semantics): a poisoned record could hide a half-applied mutation, so
the caller is told to verify collection state before retrying. Deterministic
failures (e.g. duplicate rejection) do persist a ``failed`` record.

GROWTH LIMIT:
The ledger keeps at most MAX_ENTRIES (default 1000) most-recent keys,
evicting the oldest first, so the file cannot grow without bound.
Single-instance assumption: the lock is process-internal; run exactly
one mcp-service replica per ledger file.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger("anki_mcp.ledger")

MAX_ENTRIES = 1000


class IdempotencyLedger:
    """Thread- and coroutine-safe persistent store for idempotency keys.

    One instance per user, persisted at ``<data_dir>/ledgers/<username>/ledger.json``
    (see the module docstring for the check -> mutate -> record cycle and the
    ambiguous-outcome no-record semantics for explicit syncs).

    CRASH-WINDOW LIMITATION DOCUMENTATION:
    The record is committed only AFTER the collection write completes (step 3
    above), so a crash or power loss between the collection write and the
    ledger commit leaves no record. On retry the mutation re-applies:
      - For notes, Anki's built-in first-field duplicate checking ('canAddNotes') will
        detect and reject accidental duplicate creation if allowDuplicate is False.
      - If duplicates were explicitly allowed, a duplicated note could be created in this
        narrow crash window.

    GROWTH LIMIT and the single-instance assumption: see the module docstring
    (at most MAX_ENTRIES most-recent keys; process-internal lock).
    """

    def __init__(self, file_path: str, max_entries: int = MAX_ENTRIES):
        self.file_path = file_path
        self.max_entries = max_entries
        self._lock = asyncio.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _evict_oldest_locked(self) -> None:
        """Bound ledger growth: drop the oldest entries beyond max_entries."""
        while len(self._entries) > self.max_entries:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]
            logger.debug("Evicted idempotency entry '%s' (ledger cap reached)", oldest_key)

    def _load(self) -> None:
        """Load ledger entries from disk if file exists."""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._entries = data
                        logger.info("Loaded %d idempotency records from %s", len(self._entries), self.file_path)
                    else:
                        logger.warning("Corrupted ledger format in %s, starting fresh", self.file_path)
            except Exception as e:
                logger.error("Failed to read idempotency ledger from %s: %s", self.file_path, e)

    def _save_atomic(self) -> None:
        """Persist entries to disk using an atomic rename to prevent corruption on crash."""
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name if dir_name else ".", prefix="ledger_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.file_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to atomically save idempotency ledger to %s: %s", self.file_path, e)
            raise

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve existing record for key if it exists."""
        if not key:
            return None
        async with self._lock:
            return self._entries.get(key)

    async def record_success(self, key: str, data: Any) -> None:
        """Record successful operation in ledger."""
        if not key:
            return
        async with self._lock:
            self._entries[key] = {
                "status": "committed",
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._evict_oldest_locked()
            self._save_atomic()
            logger.info("Recorded idempotency key '%s' with committed status", key)

    async def record_failure(self, key: str, error: str) -> None:
        """Record failed operation in ledger."""
        if not key:
            return
        async with self._lock:
            self._entries[key] = {
                "status": "failed",
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._evict_oldest_locked()
            self._save_atomic()
            logger.info("Recorded idempotency key '%s' with failed status", key)
