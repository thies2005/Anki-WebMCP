"""Per-user MCP tool-usage log rendered on the portal's Tools page.

One JSON file per account at ``<data_dir>/users/<username>/tool_usage.json``,
appended by the ``_tool_guard`` wrapper in :mod:`src.main` on EVERY tool
call — reads included, and refused (disabled-tool) calls too — so the portal
shows what connectors actually did. When an account is deleted the whole
``users/<name>`` directory is archived to ``deleted/`` by the existing
deletion flow, so the log rides along with the collection.

GROWTH LIMIT: a ring buffer capped at MAX_ENTRIES most-recent entries;
persistence mirrors :class:`src.ledger.IdempotencyLedger` (atomic
mkstemp -> fsync -> os.replace under a per-user asyncio lock).

Client-controlled text (the error snippet) is control-char stripped and
length-capped BEFORE storage (CWE-117 posture): a hostile tool argument can
neither bloat the file per entry nor smuggle markup into the portal table —
the page additionally HTML-escapes everything it renders.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger("anki_mcp.toollog")

MAX_ENTRIES = 1000
MAX_ERROR_CHARS = 300


def sanitize_error(message: Any) -> str:
    """Control-char-stripped, length-capped error snippet for storage."""
    text = str(message)
    # Keep printable text only: map control chars (and DEL) to '?' so a
    # multi-line/injection payload collapses into one inert line.
    cleaned = "".join(ch if (ch >= " " and ch != "\x7f") else "?" for ch in text)
    return cleaned[:MAX_ERROR_CHARS]


class ToolUsageLogger:
    """Per-user usage-log ring buffers, one JSON file per account.

    Writes hold the per-user asyncio lock while the whole file is rewritten
    (same trade-off as the idempotency ledger: entry counts are capped, so
    the rewrite is bounded). Reads return copies, newest first.
    """

    def __init__(self, data_dir: str, max_entries: int = MAX_ENTRIES):
        self._data_dir = data_dir
        self.max_entries = max_entries
        self._logs: Dict[str, List[Dict[str, Any]]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _file_path(self, username: str) -> str:
        return os.path.join(self._data_dir, "users", username, "tool_usage.json")

    def _lock_for(self, username: str) -> asyncio.Lock:
        lock = self._locks.get(username)
        if lock is None:
            # benign race: a duplicate Lock is discarded, the first one wins
            lock = asyncio.Lock()
            self._locks.setdefault(username, lock)
            lock = self._locks[username]
        return lock

    def _load(self, username: str) -> List[Dict[str, Any]]:
        path = self._file_path(username)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                entries = [e for e in data if isinstance(e, dict)]
                logger.info(
                    "Loaded %d tool-usage entries for %r", len(entries), username
                )
                return entries
            logger.warning("Corrupted tool-usage log for %r; starting fresh", username)
        except (OSError, ValueError) as e:
            logger.error("Failed to read tool-usage log for %r: %s", username, e)
        return []

    def _entries_for(self, username: str) -> List[Dict[str, Any]]:
        entries = self._logs.get(username)
        if entries is None:
            entries = self._load(username)
            self._logs[username] = entries
        return entries

    def _save_atomic(self, username: str, entries: List[Dict[str, Any]]) -> None:
        path = self._file_path(username)
        dir_name = os.path.dirname(path)
        os.makedirs(dir_name, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="toolusage_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to atomically save tool-usage log for %r: %s", username, e)
            raise

    async def record(
        self,
        username: str,
        tool: str,
        ok: bool,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """Append one entry, evicting beyond the cap. Never raises into a
        tool call path on ordinary failures: storage errors are logged and
        the in-memory buffer still updates (the log is best-effort)."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "ok": bool(ok),
            "duration_ms": int(duration_ms),
            "error": sanitize_error(error) if error else None,
        }
        async with self._lock_for(username):
            entries = self._entries_for(username)
            entries.append(entry)
            while len(entries) > self.max_entries:
                entries.pop(0)
            try:
                self._save_atomic(username, entries)
            except Exception:
                logger.exception("Tool-usage log write failed for %r (in-memory kept)", username)

    async def recent(self, username: str, limit: int = 50) -> List[Dict[str, Any]]:
        """The most recent entries, newest first, as copies."""
        async with self._lock_for(username):
            entries = self._entries_for(username)
            return [dict(e) for e in reversed(entries[-limit:])]
