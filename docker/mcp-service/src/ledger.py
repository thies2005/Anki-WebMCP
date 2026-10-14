"""Persistent idempotency ledger for operation deduplication across restarts.

The headless service runs one ledger PER USER, at
``<data_dir>/ledgers/<username>/ledger.json`` (managed by
:class:`src.collections.CollectionRegistry.ledger`). Each write mutation
carrying an ``idempotency_key`` follows a per-user, lock-serialized cycle:

    1. Check the key in this ledger; a committed record replays the cached
       result, a failed record rejects the retry (no re-apply).
    2. Apply the mutation locally under the user's collection lock.
    3. Commit the record to this ledger (flushed atomically to disk).

AnkiWeb synchronization happens either explicitly (``sync_collection`` / the
portal's Sync button) or automatically via the pre-tool two-way sync, which
pushes pending local mutations at most one SYNC_BEFORE_TOOLS_TTL_SECONDS
window after they are made (accounts with saved credentials; disable with
SYNC_BEFORE_TOOLS=false or a sync_collection tool rule). If a sync drops
mid-transfer with an unknown outcome, the outcome is surfaced as
``outcome_unknown`` and NO record is written for the key (ambiguous-outcome
no-record semantics): a poisoned record could hide a half-applied mutation, so
the caller is told to verify collection state before retrying. Deterministic
failures (e.g. duplicate rejection) do persist a ``failed`` record.
add_notes validates note models AND field names up front, before any write, so
the remaining add_notes failures occur before any note is persisted — a
``failed`` record for add_notes implies nothing was applied.

GROWTH LIMIT:
The ledger keeps at most MAX_ENTRIES (default 1000) most-recent keys,
evicting the oldest first, and rejects keys longer than MAX_KEY_LEN
characters, so the file cannot grow without bound in either entry count or
per-entry bytes.
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

# Per-entry application-layer bound on the key (the model layer enforces the
# same limit on AddNotesInput.idempotency_key). Without it the JSON ledger
# file on the shared /data volume could grow without bound per entry (every
# keyed mutation rewrites the whole file), contradicting the growth-limit
# invariant documented above.
MAX_KEY_LEN = 256

# Keys are logged %r (repr escapes newlines/control chars) and truncated so a
# client-controlled key can never split or bloat a log record (CWE-117).
_LOG_KEY_LEN = 64


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
    (at most MAX_ENTRIES most-recent keys, keys bounded at MAX_KEY_LEN
    characters; process-internal lock).
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
        """Persist entries to disk using an atomic rename to prevent corruption on crash.

        Called via ``asyncio.to_thread`` from ``record_success``/``record_failure``,
        so the mkstemp/json.dump/fsync work runs on a worker thread while the
        ledger's asyncio lock stays held — writes remain serialized and the
        event loop is never blocked by disk I/O.
        """
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
        """Record successful operation in ledger.

        Raises ValueError for keys longer than MAX_KEY_LEN: the per-entry
        application-layer byte bound keeping the ledger file growth-limited
        (the input model enforces the same limit upstream).
        """
        if not key:
            return
        if len(key) > MAX_KEY_LEN:
            raise ValueError(f"idempotency_key longer than {MAX_KEY_LEN} characters")
        async with self._lock:
            self._entries[key] = {
                "status": "committed",
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._evict_oldest_locked()
            # Atomic rewrite (mkstemp -> fsync -> os.replace) on a worker
            # thread so disk I/O never blocks the event loop; the ledger
            # asyncio lock is held across the call, so writers stay
            # serialized and saves cannot interleave.
            await asyncio.to_thread(self._save_atomic)
            logger.info("Recorded idempotency key %r with committed status", key[:_LOG_KEY_LEN])

    async def record_failure(self, key: str, error: str) -> None:
        """Record failed operation in ledger (same MAX_KEY_LEN guard as record_success)."""
        if not key:
            return
        if len(key) > MAX_KEY_LEN:
            raise ValueError(f"idempotency_key longer than {MAX_KEY_LEN} characters")
        async with self._lock:
            self._entries[key] = {
                "status": "failed",
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._evict_oldest_locked()
            # Same worker-thread rewrite discipline as record_success (the
            # ledger asyncio lock is held across the call).
            await asyncio.to_thread(self._save_atomic)
            logger.info("Recorded idempotency key %r with failed status", key[:_LOG_KEY_LEN])

    async def clear(self) -> None:
        """Drop every recorded key and persist the empty ledger atomically.

        Used after a backup restore (audit F-11): keys committed after the
        restored snapshot was created were never applied to the restored
        collection, so their records must not replay. Persists even when the
        in-memory map was already empty — the on-disk file may still hold
        records written by a previous process. Same worker-thread rewrite
        discipline as record_success/record_failure (the ledger asyncio lock
        is held across the save).
        """
        async with self._lock:
            self._entries.clear()
            await asyncio.to_thread(self._save_atomic)
            logger.info("Cleared idempotency ledger %s", self.file_path)
