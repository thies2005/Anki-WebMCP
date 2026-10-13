"""Per-user Anki collection registry for the headless multi-user service.

The registry owns every account's on-disk Anki collection and is the ONLY
component that opens them (Task 5 tools call registry methods instead of an
AnkiConnect client). Data layout under ``settings.data_dir``::

    <data_dir>/users/<username>/collection.anki2
    <data_dir>/users/<username>/collection.media/
    <data_dir>/users/<username>/backups/
    <data_dir>/ledgers/<username>/ledger.json

Design invariants (verified against the anki==26.9.3 wheel):

- ``anki.collection.Collection`` is NOT thread-safe: all access to one user's
  collection is serialized behind that user's ``asyncio.Lock`` and executed on
  worker threads via ``asyncio.to_thread``.
- ``Collection(path)`` opens OR creates the collection; ``col.close()`` sets
  ``col.db = None`` and later use raises ``InvalidInput`` — a backend error
  with EMPTY ``args`` and ``str() == "CollectionNotOpen"`` (pinned wheel
  26.9.3).
- Tags are space-separated STRINGS in this API generation (relevant for
  callers of ``run()``; the registry itself is tag-agnostic).
- ``create_backup`` writes .colpkg files in the NEW package layout: the real
  database is the zstd-compressed ``collection.anki21b`` zip member, while
  ``collection.anki2`` is only a downgrade STUB (a SQLite file whose single
  note tells users to re-import in a newer client — restoring it would wipe
  the user's data). Restoring therefore reads ``collection.anki21b`` via the
  stdlib ``compression.zstd`` (Python 3.14+, PEP 784), falling back to the
  ``zstandard`` package on older Pythons, and falls back to a raw
  ``collection.anki2`` member only for legacy packages that lack 21b
  (all verified against the pinned wheel 26.9.3).

Locking model:

- Per-user ``asyncio.Lock``: guards the user's Collection handle and all
  file/thread state mutations for that user.
- Registry-level ``asyncio.Lock`` (``_registry_lock``): guards the
  open-collection dict (state creation, LRU eviction snapshots, idle sweep).
- Eviction/idle-close snapshot BOTH conditions (victim lock unlocked) and the
  detach (``victim.col = None``) inside ONE no-await critical section under
  ``_registry_lock``. Because ``run()`` reads ``state.col`` only while holding
  the user lock, a coroutine that acquires the victim's lock afterwards sees
  ``None`` and opens a fresh handle — it can never touch the detached one, so
  the detached handle is closed without holding any user lock and without a
  lock-ordering deadlock (a user lock is never awaited while holding
  ``_registry_lock``).
- ``drop_user`` (user-deletion hook) follows close_all's pattern: it pops the
  ``_UserState`` and detaches the handle in one critical section under
  ``_registry_lock``, then closes the detached handle while holding the
  popped state's user lock. An in-flight ``run()``/``sync_user`` therefore
  finishes before its handle is closed, while any call arriving after the pop
  creates fresh state and a fresh collection — a recreated username can never
  inherit the deleted account's serving state.
"""

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from anki.collection import Collection
from anki.errors import InvalidInput, NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth, SyncOutput
from anki.sync_pb2 import SyncCollectionResponse

from .ledger import MAX_ENTRIES as LEDGER_MAX_ENTRIES
from .ledger import IdempotencyLedger
from .users import USERNAME_RE

def _load_zstd_decompress():
    """One-shot zstd decompression for the wheel's backup member, or None.

    Prefers the stdlib ``compression.zstd`` (Python 3.14+, PEP 784) — what the
    Docker image runs. On older Pythons falls back to the ``zstandard``
    reference backport behind a streaming adapter: the wheel writes frames
    WITHOUT an embedded content size, which the backport's one-shot
    ``decompress()`` refuses ("could not determine content size in frame
    header") while ``decompressobj`` streams them fine. Restore must never be
    the broken half of the backup/restore pair just because the runtime
    predates the stdlib module.
    """
    try:
        from compression import zstd

        return zstd.decompress
    except ImportError:
        pass
    try:
        import zstandard

        def _backport_decompress(data: bytes) -> bytes:
            dobj = zstandard.ZstdDecompressor().decompressobj()
            return dobj.decompress(data) + dobj.flush()

        return _backport_decompress
    except ImportError:
        return None


_zstd_decompress = _load_zstd_decompress()

logger = logging.getLogger("anki_mcp.collections")

# Reasons embedded into backup filenames as backup-<timestamp>-<reason>.colpkg.
# The reason travels in the NAME so a backup's provenance survives on disk and
# list_backups() can report it without a sidecar index.
BACKUP_REASONS = (
    "pre-sync",
    "pre-delete-notes",
    "pre-change-note-type",
    "pre-delete-deck",
    "pre-rename-deck",
    "pre-restore",
    "pre-remove-note-type-field",
    "pre-remove-note-type-template",
    "pre-delete-note-type",
    "pre-restore-note-type-stock",
    "manual",
)

# backup-<timestamp>[-<reason>].colpkg. The timestamp is second-resolution and
# zero-padded, so lexicographic order IS chronological order (prune_backups
# relies on that); reason words are lowercase and hyphen-joined, which is why
# an uppercase or digit-bearing suffix cannot masquerade as a reason.
BACKUP_FILENAME_RE = re.compile(r"^backup-(\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2})(?:-([a-z0-9]+(?:-[a-z0-9]+)*))?\.colpkg$")

# Restore reads one zip member into memory; the bound rejects a bomb-sized
# member BEFORE the read allocates for it (chunked read stops at cap+1). Real
# collections are far below this; .colpkg files are only ever written by the
# wheel's create_backup, so this guards against a planted file, not traffic.
_MAX_RESTORE_MEMBER_BYTES = 1024 * 1024 * 1024

# Follow-up actions for a sync_collection handshake result. The wheel's
# sync_collection() performs login plus the regular delta sync and reports
# what more the server requires; the bulk transfers are separate calls.
_CHANGES_REQUIRED = SyncCollectionResponse.ChangesRequired


def _plan_sync_followup(out: SyncOutput) -> Tuple[str, Optional[int]]:
    """Map a ``sync_collection`` result to the follow-up the caller must run.

    Returns ``(action, server_media_usn)`` with action one of:

    - ``"none"``     — NO_CHANGES/NORMAL_SYNC: the handshake already settled
      everything (the regular delta runs inside ``sync_collection``).
    - ``"download"`` — the server holds a collection this client must pull
      wholesale (fresh/empty local collection, e.g. first sync of an account
      with existing data). Run ``full_upload_or_download(upload=False)`` with
      the response's ``server_media_usn``.
    - ``"upload"``   — the local collection must replace the server's. Run
      ``full_upload_or_download(upload=True)`` (no media USN).
    - ``"error"``    — FULL_SYNC asks for a user-chosen direction (desktop
      "on next sync, force full sync" option); the caller surfaces it as a
      failure unless the caller-supplied ``full_sync_direction`` settles it
      (see :meth:`sync_user`).

    Regression context: the first sync of a real account returns
    ``required: FULL_DOWNLOAD`` and, until this is acted on, the collection
    stays empty while the portal reports success.
    """
    required = out.required
    if required in (_CHANGES_REQUIRED.NO_CHANGES, _CHANGES_REQUIRED.NORMAL_SYNC):
        return "none", None
    if required == _CHANGES_REQUIRED.FULL_DOWNLOAD:
        return "download", int(out.server_media_usn)
    if required == _CHANGES_REQUIRED.FULL_UPLOAD:
        return "upload", None
    return "error", None


def _check_username(username: str) -> str:
    """Path-traversal guard: usernames become directory names under data_dir.

    Reuses Task 1's USERNAME_RE (same policy as src/users.py, matched with
    fullmatch so trailing-newline quirks cannot smuggle a path through).
    """
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError(
            f"Invalid username {username!r}: must match ^[a-z0-9_-]{{1,32}}$ "
            "(usernames are used as directory names and must be path-safe)"
        )
    return username


def _check_backup_reason(reason: str) -> str:
    """Return ``reason`` if it is one of BACKUP_REASONS, else raise ValueError."""
    if reason not in BACKUP_REASONS:
        raise ValueError(f"Invalid backup reason {reason!r}: must be one of {', '.join(BACKUP_REASONS)}")
    return reason


def _read_collection_member(path: str) -> bytes:
    """Return the SQLite collection bytes stored inside a .colpkg backup.

    Verified against the pinned wheel (26.9.3): ``create_backup`` writes the
    NEW colpkg layout — the real database is the zstd-compressed
    ``collection.anki21b`` member, while ``collection.anki2`` is only a
    downgrade STUB whose single note says "Please update to the latest Anki
    version, then import the .colpkg/.apkg file again"; restoring the stub
    would wipe the user's data, so it is NEVER used while the real member
    exists. Legacy packages (older wheels) carry the raw database in
    ``collection.anki2`` instead. Exactly one member is read directly —
    never ``extractall`` — so a crafted zip cannot write outside the target
    directory (zip-slip), and the read itself is byte-bounded so a planted
    bomb-sized member is rejected instead of buffered.
    """

    def _bounded_read(archive: "zipfile.ZipFile", name: str) -> bytes:
        info = archive.getinfo(name)
        if info.file_size > _MAX_RESTORE_MEMBER_BYTES:
            raise ValueError(
                f"Backup {os.path.basename(path)!r} member {name!r} declares "
                f"{info.file_size} bytes, over the restore cap"
            )
        with archive.open(name) as fh:
            data = fh.read(_MAX_RESTORE_MEMBER_BYTES + 1)
        if len(data) > _MAX_RESTORE_MEMBER_BYTES:
            raise ValueError(
                f"Backup {os.path.basename(path)!r} member {name!r} exceeds the restore cap"
            )
        return data

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "collection.anki21b" in names:
            if _zstd_decompress is None:
                raise ValueError(
                    f"Backup {os.path.basename(path)!r} stores its database in the zstd-compressed "
                    "'collection.anki21b' member but no zstd implementation is available; "
                    "restore requires Python 3.14+ (stdlib compression.zstd) or the "
                    "'zstandard' package (pip install zstandard)"
                )
            return _zstd_decompress(_bounded_read(archive, "collection.anki21b"))
        if "collection.anki2" in names:
            return _bounded_read(archive, "collection.anki2")
        raise ValueError(
            f"Backup {os.path.basename(path)!r} contains no collection database "
            "(expected a 'collection.anki21b' or 'collection.anki2' member)"
        )


@dataclass
class _UserState:
    """Per-user registry entry. ``col`` is None while the user has no open collection."""

    username: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    col: Optional[Collection] = None
    last_used: float = 0.0


class CollectionRegistry:
    """Lazily-opened, LRU-capped pool of per-user Anki collections.

    Constructor parameters mirror the Task 1 settings (``data_dir``,
    ``idle_timeout_minutes``, ``max_open_collections``,
    ``max_concurrent_syncs``, ``backup_retention``); construction performs no
    I/O — collections, media/backup directories and ledgers are created
    lazily per user.
    """

    def __init__(
        self,
        data_dir: str,
        idle_timeout_minutes: int = 15,
        max_open_collections: int = 5,
        max_concurrent_syncs: int = 2,
        backup_retention: int = 20,
    ):
        self.data_dir = str(data_dir)
        self.idle_timeout_minutes = int(idle_timeout_minutes)
        self.max_open_collections = int(max_open_collections)
        self.max_concurrent_syncs = int(max_concurrent_syncs)
        self.backup_retention = int(backup_retention)
        # Retention < 1 would mean "delete every backup", including the one
        # sync just wrote; fail loud like Settings does for the same env var.
        if self.backup_retention < 1:
            raise ValueError(f"backup_retention must be >= 1, got {self.backup_retention}")
        if self.max_open_collections < 1:
            raise ValueError(f"max_open_collections must be >= 1, got {self.max_open_collections}")
        if self.max_concurrent_syncs < 1:
            raise ValueError(f"max_concurrent_syncs must be >= 1, got {self.max_concurrent_syncs}")
        # Test seam: the idle sweep compares against this many seconds.
        self._idle_seconds = self.idle_timeout_minutes * 60.0
        self._users: Dict[str, _UserState] = {}
        self._registry_lock = asyncio.Lock()
        self._sync_semaphore = asyncio.Semaphore(self.max_concurrent_syncs)
        self._ledgers: Dict[str, IdempotencyLedger] = {}
        # IdempotencyLedger's constructor does a small file read; guard its
        # one-time creation with a threading lock so concurrent callers cannot
        # build two live objects for the same file.
        self._ledgers_lock = threading.Lock()

    # -- paths (each entry point validates the username first) ---------------

    def _user_root(self, username: str) -> str:
        return os.path.join(self.data_dir, "users", username)

    def _collection_path(self, username: str) -> str:
        _check_username(username)
        return os.path.join(self._user_root(username), "collection.anki2")

    def _media_dir(self, username: str) -> str:
        _check_username(username)
        return os.path.join(self._user_root(username), "collection.media")

    def _backups_dir(self, username: str) -> str:
        _check_username(username)
        return os.path.join(self._user_root(username), "backups")

    def _ledger_path(self, username: str) -> str:
        _check_username(username)
        return os.path.join(self.data_dir, "ledgers", username, "ledger.json")

    # -- the access primitive -------------------------------------------------

    async def run(self, username: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(col, *args, **kwargs)`` with the user's open collection.

        Acquires the user's lock, opens (or creates) the collection, runs
        ``fn`` on a worker thread (the Collection is not thread-safe), and
        returns its result. Errors from ``fn`` propagate unchanged to the
        caller (tools translate them into structured output). If the failure
        is a stale/closed handle, the collection is reopened ONCE and the
        call retried ONCE.
        """
        _check_username(username)
        state = await self._get_state(username)
        async with state.lock:
            col = await self._ensure_open(state)
            try:
                return await asyncio.to_thread(fn, col, *args, **kwargs)
            except Exception as exc:
                if state.col is col and self._is_stale(col, exc):
                    # %r over the MESSAGE (not %s over exc): backend exception
                    # text embeds client-controlled content VERBATIM (e.g. a
                    # find_and_replace regex=true pattern inside a Rust regex
                    # parse error), and %s would let one tool call author
                    # multi-line log records (CWE-117). The wheel's
                    # BackendError keeps args empty (message in _message), so
                    # %r of the exception object would drop the diagnostic.
                    logger.warning(
                        "Stale collection handle for %r (%s: %r); reopening once and retrying",
                        username,
                        type(exc).__name__,
                        str(exc),
                    )
                    col = await self._recover_stale(state, col)
                    return await asyncio.to_thread(fn, col, *args, **kwargs)
                raise
            finally:
                state.last_used = time.time()

    @staticmethod
    def _is_stale(col: Collection, exc: Exception) -> bool:
        """True if ``exc`` indicates the handle was closed underneath us.

        Contract: the retry may fire ONLY when the handle was genuinely
        closed. The handle check is authoritative — ``col.close()`` sets
        ``db = None``, and every close path in this registry goes through
        ``col.close()``. The single fallback is exact backend TYPE identity
        (``isinstance(exc, InvalidInput)``): the pinned wheel raises
        ``InvalidInput`` with EMPTY ``args`` for a closed handle, so there is
        nothing to match in the text and, more importantly, exception TEXT is
        NEVER matched — tool-layer messages can carry user-controlled strings
        (model/field names), and an application exception that merely mentions
        "CollectionNotOpen" on a healthy handle must propagate unchanged
        without closing the handle and without re-executing ``fn`` (the retry
        re-runs it, so non-idempotent mutations must not be re-run).
        """
        if getattr(col, "db", None) is None:
            return True
        return isinstance(exc, InvalidInput)

    async def _recover_stale(self, state: _UserState, col: Collection) -> Collection:
        """Drop a stale handle and reopen. Caller holds state.lock."""
        try:
            await asyncio.to_thread(col.close)  # no-op when db is already None
        except Exception:
            logger.debug("Ignoring error while closing stale handle for %r", state.username, exc_info=True)
        state.col = None
        return await self._ensure_open(state)

    # -- open / evict / close --------------------------------------------------

    async def _get_state(self, username: str) -> _UserState:
        async with self._registry_lock:
            state = self._users.get(username)
            if state is None:
                state = _UserState(username=username)
                self._users[username] = state
            return state

    async def _ensure_open(self, state: _UserState) -> Collection:
        """Return the user's open collection, opening/creating it if needed.

        Caller must hold state.lock. Creates the directory layout eagerly on
        first open (Collection itself creates collection.anki2 + the media
        directory; media/ and backups/ are asserted here so sync backups have
        a home even before the first write).
        """
        col = state.col
        if col is not None and col.db is not None:
            return col
        if col is not None:
            # Handle exists but is closed (e.g. closed behind our back); replace it.
            state.col = None
            try:
                await asyncio.to_thread(col.close)
            except Exception:
                logger.debug("Ignoring error closing detached handle for %r", state.username, exc_info=True)

        username = state.username
        os.makedirs(self._user_root(username), exist_ok=True)
        os.makedirs(self._media_dir(username), exist_ok=True)
        os.makedirs(self._backups_dir(username), exist_ok=True)

        # LRU eviction BEFORE opening a new collection: pick victims under the
        # registry lock (locked-unlocked check + detach is one atomic
        # critical section), close the detached handles afterwards without
        # holding _registry_lock (no user lock is awaited while it is held).
        async with self._registry_lock:
            evicted = self._evict_lru_locked(exclude=state)

        new_col = await asyncio.to_thread(Collection, self._collection_path(username))

        async with self._registry_lock:
            state.col = new_col
            state.last_used = time.time()

        for victim, old_col in evicted:
            logger.info(
                "Evicted least-recently-used collection for %r (max_open_collections=%d)",
                victim.username,
                self.max_open_collections,
            )
            try:
                await asyncio.to_thread(old_col.close)
            except Exception:
                logger.warning("Failed to close evicted collection for %r", victim.username, exc_info=True)
        return new_col

    def _evict_lru_locked(self, exclude: _UserState) -> List[Tuple[_UserState, Collection]]:
        """Detach LRU victims until there is room for one more open collection.

        Caller holds _registry_lock; contains no await points (the unlocked
        check and the detach are atomic with respect to the event loop).
        Never evicts the caller's own user. If every open collection is busy,
        proceeds without eviction (transiently over cap) rather than
        deadlocking.
        """
        evicted: List[Tuple[_UserState, Collection]] = []
        while self._open_count_locked() >= self.max_open_collections:
            victim: Optional[_UserState] = None
            for candidate in sorted(self._users.values(), key=lambda s: s.last_used):
                if candidate is exclude or candidate.col is None:
                    continue
                if not candidate.lock.locked():
                    victim = candidate
                    break
            if victim is None:
                logger.warning(
                    "All %d open collections are busy; temporarily exceeding max_open_collections=%d",
                    self._open_count_locked(),
                    self.max_open_collections,
                )
                break
            old_col = victim.col
            assert old_col is not None
            victim.col = None
            evicted.append((victim, old_col))
        return evicted

    def _open_count_locked(self) -> int:
        return sum(1 for s in self._users.values() if s.col is not None)

    async def close_idle(self) -> List[str]:
        """Close collections idle beyond idle_timeout_minutes; returns usernames closed.

        Loop-friendly: main() schedules this once a minute. Busy users are
        never touched; a user closed here reopens transparently on next use.
        """
        now = time.time()
        victims: List[Tuple[_UserState, Collection]] = []
        async with self._registry_lock:
            for state in self._users.values():
                if state.col is None or state.lock.locked():
                    continue
                if now - state.last_used >= self._idle_seconds:
                    old_col = state.col
                    state.col = None
                    victims.append((state, old_col))
        closed: List[str] = []
        for state, old_col in victims:
            try:
                await asyncio.to_thread(old_col.close)
                closed.append(state.username)
                logger.info(
                    "Closed idle collection for %r (idle >= %d minute(s))",
                    state.username,
                    self.idle_timeout_minutes,
                )
            except Exception:
                logger.warning("Failed to close idle collection for %r", state.username, exc_info=True)
        return closed

    async def close_all(self) -> None:
        """Close every open collection (shutdown hook).

        Waits for in-flight operations by acquiring each user's lock before
        closing that user's (already detached) handle.
        """
        async with self._registry_lock:
            pending: List[Tuple[_UserState, Collection]] = []
            for state in self._users.values():
                if state.col is not None:
                    old_col = state.col
                    state.col = None
                    pending.append((state, old_col))
        for state, old_col in pending:
            try:
                async with state.lock:
                    await asyncio.to_thread(old_col.close)
            except Exception:
                logger.warning("Failed to close collection for %r during shutdown", state.username, exc_info=True)
        if pending:
            logger.info("Closed %d open collection(s) at shutdown", len(pending))

    async def drop_user(self, username: str) -> None:
        """Detach ALL serving state for ``username`` (user-deletion hook).

        Closes and drops the open Collection handle, pops the cached
        ``_UserState`` and pops the in-memory per-user ledger, so a recreated
        username starts from a fresh collection and an empty ledger. The
        caller must also archive/remove ``users/<username>/`` and
        ``ledgers/<username>/`` on disk, or a registry restart reloads them.

        Race-safety (close_all's pattern): the state pop + handle detach are
        one critical section under ``_registry_lock``; the detached handle is
        then closed while holding the popped state's user lock, so an
        in-flight ``run()``/``sync_user`` completes before its handle is
        closed, and any call arriving after the pop creates fresh state and a
        fresh collection. Close errors are logged, never raised — deletion
        must not fail because a broken handle could not be closed.
        """
        _check_username(username)
        async with self._registry_lock:
            state = self._users.pop(username, None)
            col = state.col if state is not None else None
            if state is not None:
                state.col = None
        if state is not None:
            try:
                async with state.lock:
                    if col is not None:
                        await asyncio.to_thread(col.close)
            except Exception:
                logger.warning("Failed to close collection for deleted user %r", username, exc_info=True)
        with self._ledgers_lock:
            self._ledgers.pop(username, None)

    # -- introspection (tests / health endpoints) ------------------------------

    def open_usernames(self) -> List[str]:
        """Usernames with a currently-open collection, least-recently-used first."""
        return [
            s.username
            for s in sorted(self._users.values(), key=lambda s: s.last_used)
            if s.col is not None
        ]

    def is_open(self, username: str) -> bool:
        _check_username(username)
        state = self._users.get(username)
        return state is not None and state.col is not None and state.col.db is not None

    # -- per-user idempotency ledger -------------------------------------------

    def ledger(self, username: str) -> IdempotencyLedger:
        """The user's persistent idempotency ledger (created on first use)."""
        _check_username(username)
        with self._ledgers_lock:
            led = self._ledgers.get(username)
            if led is None:
                led = IdempotencyLedger(self._ledger_path(username), max_entries=LEDGER_MAX_ENTRIES)
                self._ledgers[username] = led
            return led

    # -- backup pruning ----------------------------------------------------------

    def prune_backups(self, username: str) -> List[str]:
        """Keep only the backup_retention newest files in backups/; returns kept names.

        Real Anki backups are named ``backup-YYYY-MM-DD-HH.MM.SS.colpkg``
        (verified against the wheel), so lexicographic order IS chronological
        order. Exposed as a method for testability and reuse by sync_user.
        """
        _check_username(username)
        backup_dir = self._backups_dir(username)
        try:
            names = sorted(n for n in os.listdir(backup_dir) if os.path.isfile(os.path.join(backup_dir, n)))
        except FileNotFoundError:
            return []
        keep = set(names[-self.backup_retention :]) if self.backup_retention > 0 else set()
        for name in names:
            if name not in keep:
                try:
                    os.remove(os.path.join(backup_dir, name))
                except OSError:
                    logger.warning("Failed to prune backup %s for %r", name, username, exc_info=True)
        return sorted(keep)

    # -- backups (snapshot engine) -----------------------------------------------

    def _backup_locked(self, col: Collection, username: str, reason: str) -> str:
        """Create a reason-tagged backup of the user's collection; returns its filename.

        The caller holds the user's lock (inside a ``run()`` closure, or under
        the lock in sync_user/restore_backup): ``create_backup`` must not race
        a concurrent collection operation. The backend writes a plain
        ``backup-<timestamp>.colpkg`` — verified against the wheel, it writes
        NO file when nothing changed since the last backup (returning False,
        or True while overwriting a same-second name), so the before/after
        directory diff is authoritative, not the return value. When no new
        file appears, the newest existing backup already IS the current state
        and its name is returned. The reason is inserted AFTER the unchanged
        timestamp prefix, so the lexicographic order prune_backups relies on
        stays chronological. Prunes to ``backup_retention`` on the way out.
        """
        _check_backup_reason(reason)
        backup_dir = self._backups_dir(username)
        try:
            before = set(os.listdir(backup_dir))
        except FileNotFoundError:
            before = set()
        col.create_backup(backup_folder=backup_dir, force=True, wait_for_completion=True)
        try:
            after = set(os.listdir(backup_dir))
        except FileNotFoundError:
            after = set()
        new_files = sorted(after - before)
        if not new_files:
            # Nothing changed since the last backup (or a same-second backup
            # overwrote an existing plain name): the newest surviving backup
            # reflects the current state. Only genuine backup names qualify —
            # a foreign file must never be returned as a backup handle.
            existing = sorted(name for name in after if BACKUP_FILENAME_RE.fullmatch(name))
            if not existing:
                raise RuntimeError(f"No backup exists for user {username!r} and none was created")
            return existing[-1]
        newest = new_files[-1]
        match = BACKUP_FILENAME_RE.fullmatch(newest)
        if match is None:
            # Unreachable for wheel-written names; fail loud rather than
            # rename an unexpected file into a mangled reason-tagged name.
            raise RuntimeError(f"Backup backend produced unexpected filename {newest!r}")
        final_name = f"backup-{match.group(1)}-{reason}.colpkg"
        # Same-second, same-reason snapshots must never overwrite each other:
        # os.rename silently REPLACES on POSIX, which would destroy the
        # pre-delete state a destructive op just snapshotted. Disambiguate
        # with a numeric suffix instead (parseable: the reason group accepts
        # digits). Ordering note: within one second the suffixed name sorts
        # before the base name, so retention prunes the suffixed (newer)
        # snapshot first — the safe direction, keeping older states longer.
        candidate = final_name
        attempt = 2
        while os.path.exists(os.path.join(backup_dir, candidate)):
            candidate = f"backup-{match.group(1)}-{reason}-{attempt}.colpkg"
            attempt += 1
        os.rename(os.path.join(backup_dir, newest), os.path.join(backup_dir, candidate))
        self.prune_backups(username)
        return candidate

    def list_backups(self, username: str) -> List[Dict[str, Any]]:
        """Parse the user's backup files into display records, newest first.

        Only the backups directory is ever listed (never a caller-supplied
        path), and only files matching BACKUP_FILENAME_RE are returned —
        filenames are the only handles restore_backup accepts, so a crafted
        name can neither escape the directory nor reach the restore path.
        A missing directory (user never opened) yields an empty list.
        """
        _check_username(username)
        backup_dir = self._backups_dir(username)
        try:
            names = os.listdir(backup_dir)
        except FileNotFoundError:
            return []
        entries: List[Dict[str, Any]] = []
        for name in names:
            match = BACKUP_FILENAME_RE.fullmatch(name)
            if match is None:
                continue
            try:
                size = os.path.getsize(os.path.join(backup_dir, name))
            except OSError:
                continue  # raced with prune_backups; never list a dead file
            entries.append(
                {
                    "filename": name,
                    "timestamp": match.group(1),
                    "reason": match.group(2) or "unknown",
                    "size_bytes": size,
                }
            )
        # Zero-padded timestamps sort chronologically; the filename breaks
        # ties (same-second plain vs reason-tagged) deterministically.
        entries.sort(key=lambda entry: (entry["timestamp"], entry["filename"]), reverse=True)
        return entries

    async def backup_user(self, username: str, reason: str) -> str:
        """Create a backup tagged with ``reason`` and return its filename.

        The public entry point for on-demand backups (portal, tests): the
        snapshot runs inside ``run()``'s user-lock + worker-thread discipline,
        exactly like every other collection access. An invalid reason is
        rejected BEFORE any collection is opened or directory created.
        """
        _check_username(username)
        _check_backup_reason(reason)

        def _backup(col: Collection) -> str:
            return self._backup_locked(col, username, reason)

        return await self.run(username, _backup)

    def snapshot_locked(self, col: Collection, username: str, reason: str) -> str:
        """Public face of ``_backup_locked`` for callers already inside ``run()``.

        The tools layer takes fail-closed snapshots inside its mutation
        closures (the per-user lock is already held there); reaching for the
        underscore-private method across the module boundary would freeze it
        as de-facto API. Tests keep monkeypatching ``_backup_locked`` — this
        alias deliberately delegates so the seam stays intact.
        """
        return self._backup_locked(col, username, reason)

    def backup_path(self, username: str, filename: str) -> str:
        """Filesystem path of a backup the caller has ALREADY validated.

        Companion to the portal's download route: callers must have matched
        ``filename`` against :meth:`list_backups` first (exact string
        membership), so the name is a genuine snapshot of THIS user. The
        method deliberately performs no filename validation of its own —
        only the username path guard — because a traversal name passed
        here without that prior check would escape the backups directory.
        """
        _check_username(username)
        return os.path.join(self._backups_dir(username), filename)

    # -- sync ------------------------------------------------------------------

    async def sync_user(
        self,
        username: str,
        ankiweb_email: str,
        ankiweb_password: str,
        endpoint: Optional[str] = None,
        on_step: Optional[Callable[[str], None]] = None,
        full_sync_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Back up, then synchronize the user's collection with AnkiWeb.

        Contract (mirrors the legacy ambiguous-outcome behaviour that Task 5's
        ledger logic keys off):
        - ``{"success": True, "output": str(out)}`` on success;
        - ``{"success": False, "error": ..., "error_type": ...}`` on failure;
        - additionally ``"outcome_unknown": True`` when a network-layer error
          hit DURING ``sync_collection`` (the server may or may not have
          applied the transfer — the idempotency ledger must not poison the
          key). Login-stage failures are NOT ambiguous: nothing was synced.
        - additionally ``"full_sync_required": True`` when the server demands
          a full sync with a USER-CHOSEN direction and no ``full_sync_direction``
          was supplied; the failure is definitive (nothing was transferred).

        ``full_sync_direction`` settles that demand on this service instead of
        the desktop client: ``"upload"`` replaces AnkiWeb's copy with this
        service's collection, ``"download"`` replaces this service's
        collection with AnkiWeb's (a pre-sync backup already protects the
        losing side). It is ignored for every other handshake result, and any
        other value is rejected before any network call.
        """
        _check_username(username)
        # Fail BEFORE any network call or directory creation when credentials
        # are missing — sync is opt-in per user.
        if not ankiweb_email or not ankiweb_password:
            return {
                "success": False,
                "error": "AnkiWeb credentials not set for this user; configure them before syncing",
                "error_type": "ValueError",
            }
        if full_sync_direction not in (None, "upload", "download"):
            return {
                "success": False,
                "error": (
                    f"Invalid full_sync_direction {full_sync_direction!r}: "
                    "must be 'upload' or 'download' (or omitted)"
                ),
                "error_type": "ValueError",
            }

        state = await self._get_state(username)
        # Global concurrency gate first, then the user lock (fixed order).
        async with self._sync_semaphore:
            async with state.lock:
                col = await self._ensure_open(state)
                state.last_used = time.time()
                step = "backup"
                try:
                    if on_step is not None:
                        on_step("backup")
                    # Shared snapshot path (reason-tagged file + prune); runs on
                    # a worker thread because create_backup blocks until the
                    # backup lands, and it is safe here: we already hold the
                    # user lock, so no other collection access can interleave.
                    await asyncio.to_thread(self._backup_locked, col, username, "pre-sync")

                    step = "login"
                    if on_step is not None:
                        on_step("login")
                    auth = await asyncio.to_thread(col.sync_login, ankiweb_email, ankiweb_password, endpoint)

                    step = "sync"
                    if on_step is not None:
                        on_step("sync")
                    out = await asyncio.to_thread(col.sync_collection, auth, True)

                    # Follow the server's endpoint redirect (syncNN.ankiweb.net)
                    # before any follow-up transfer, mirroring the desktop client.
                    if out.new_endpoint:
                        auth.endpoint = out.new_endpoint

                    followup, server_media_usn = _plan_sync_followup(out)
                    user_chosen = False
                    if followup == "error":
                        # FULL_SYNC: the server demands a direction only the
                        # account owner can pick (wrong choice = data loss on
                        # one side). Without an explicit choice this is a
                        # definitive refusal (ValueError, NOT SyncError):
                        # nothing was transferred, so the handler below must
                        # not classify it as ambiguous. With a choice, fall
                        # through to the matching bulk transfer — mirroring
                        # the desktop client, the download case carries the
                        # handshake's server_media_usn.
                        if full_sync_direction is None:
                            return {
                                "success": False,
                                "error": (
                                    "AnkiWeb requires a full sync with a user-chosen direction "
                                    "(the desktop client's 'force full sync' option was used on this "
                                    "account). Open the desktop client, run a full sync there to settle "
                                    "the direction, then sync this service again — or retry with "
                                    "full_sync_direction='upload' (replace AnkiWeb's copy with this "
                                    "service's collection) or 'download' (replace this service's "
                                    "collection with AnkiWeb's)."
                                ),
                                "error_type": "ValueError",
                                "full_sync_required": True,
                            }
                        user_chosen = True
                        followup = full_sync_direction
                        server_media_usn = int(out.server_media_usn) if full_sync_direction == "download" else None
                    if followup == "download":
                        if on_step is not None:
                            on_step("full-download")
                        await asyncio.to_thread(
                            col.full_upload_or_download,
                            auth=auth,
                            server_usn=server_media_usn,
                            upload=False,
                        )
                        output = (
                            "Full download from AnkiWeb completed "
                            f"(host {out.host_number})"
                            + (" with user-chosen direction" if user_chosen else "")
                            + "; collection and media pulled to this service."
                        )
                    elif followup == "upload":
                        if on_step is not None:
                            on_step("full-upload")
                        await asyncio.to_thread(
                            col.full_upload_or_download,
                            auth=auth,
                            server_usn=None,
                            upload=True,
                        )
                        output = (
                            "Full upload to AnkiWeb completed "
                            f"(host {out.host_number})"
                            + (" with user-chosen direction" if user_chosen else "")
                            + "; collection and media pushed from this service."
                        )
                    else:
                        output = str(out)
                    logger.info(
                        "Sync completed for %r (follow-up: %s%s)",
                        username,
                        followup,
                        ", user-chosen" if user_chosen else "",
                    )
                    return {"success": True, "output": output}
                except Exception as exc:
                    result: Dict[str, Any] = {
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                    if step == "sync" and self._is_ambiguous_sync_failure(exc):
                        result["outcome_unknown"] = True
                        logger.error(
                            "Network failure during sync_collection for %r; outcome unknown", username, exc_info=True
                        )
                    else:
                        logger.error("Sync failed for %r at step %r: %s", username, step, exc)
                    return result

    @staticmethod
    def _is_ambiguous_sync_failure(exc: Exception) -> bool:
        """Network-layer failure mid-transfer: the true outcome is unknowable.

        Auth failures (SyncErrorKind.AUTH) are definitive — nothing changed.
        Everything else the backend reports as NetworkError / non-auth
        SyncError happened while the transfer was in flight.
        """
        if isinstance(exc, NetworkError):
            return True
        return isinstance(exc, SyncError) and getattr(exc, "kind", None) is not SyncErrorKind.AUTH

    # -- restore ------------------------------------------------------------------

    async def restore_backup(self, username: str, filename: str) -> Dict[str, Any]:
        """Replace the user's collection with the database inside ``filename``.

        Safety chain: the username is path-checked, and the filename must
        match BACKUP_FILENAME_RE AND be one of ``list_backups(username)``'s
        exact strings, so it can never carry a path component (traversal
        guard). Before anything is replaced a ``pre-restore`` snapshot is
        taken — a restore is itself reversible. The database is read out of
        the .colpkg zip as a single member (never ``extractall``), the open
        collection handle is detached and closed with drop_user's discipline
        (detach under the registry lock, close while holding the user lock,
        so no other coroutine can touch the closing handle), and the bytes
        replace ``collection.anki2`` atomically (mkstemp + fsync + chmod 0644
        + ``os.replace``, mirroring UserStore's persistence pattern).
        ``state.col`` stays ``None``, so the next ``run()`` reopens the
        restored file fresh. Returns the restore record for the caller.
        """
        _check_username(username)
        if not BACKUP_FILENAME_RE.fullmatch(filename) or filename not in {
            entry["filename"] for entry in self.list_backups(username)
        }:
            raise ValueError(f"Unknown backup {filename!r} for user {username!r}")

        state = await self._get_state(username)
        async with state.lock:
            col = await self._ensure_open(state)
            state.last_used = time.time()
            # (a) Snapshot the CURRENT state first: a restore must be reversible.
            snapshot = await asyncio.to_thread(self._backup_locked, col, username, "pre-restore")
            # (b) Read the database bytes out of the backup zip (no extractall).
            backup_path = os.path.join(self._backups_dir(username), filename)
            payload = await asyncio.to_thread(_read_collection_member, backup_path)
            # (c) Detach + close the open handle (drop_user's pattern): the
            # detach under the registry lock contains no await points, and we
            # hold the user lock, so no other coroutine can observe or use the
            # handle between the detach and the close.
            async with self._registry_lock:
                old_col = state.col
                state.col = None
            if old_col is not None:
                try:
                    await asyncio.to_thread(old_col.close)
                except Exception:
                    logger.warning("Failed to close collection for %r before restore", username, exc_info=True)
            # (d) Swap the restored database into place atomically; mode 0644
            # matches what the wheel creates for collection.anki2.
            collection_path = self._collection_path(username)
            temp_fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(collection_path), prefix=".restore-", suffix=".tmp"
            )
            try:
                with os.fdopen(temp_fd, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temp_path, 0o644)
                os.replace(temp_path, collection_path)
            except Exception:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise
            logger.info(
                "Restored collection for %r from %r (pre-restore snapshot: %r)",
                username,
                filename,
                snapshot,
            )
            return {"restored_from": filename, "pre_restore_snapshot": snapshot}
