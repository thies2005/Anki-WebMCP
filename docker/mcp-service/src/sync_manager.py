"""SyncManager: per-user idempotent write-safety and explicit AnkiWeb sync.

Two responsibilities survive the AnkiConnect removal (Task 5):

1. ``execute_mutation`` — wraps state-mutating tool operations with the
   maintenance gate and the per-user :class:`IdempotencyLedger`
   (``registry.ledger(username)``). Pre-/post-mutation AnkiConnect syncs are
   GONE: mutations now write the user's local collection only, so the
   response's ``sync_status`` stays fully ``"skipped"`` (the response schema
   is unchanged for MCP-client compatibility). The ledger check → mutate →
   record sequence runs under a per-user asyncio lock in THIS class: the
   registry's per-user lock alone serializes only the mutation itself, which
   would let two concurrent callers sharing one idempotency key both miss in
   the ledger and double-apply. Per-user granularity — different users are
   never serialized against each other.
2. ``sync_now`` — the explicit ``sync_collection`` tool. Shares the
   maintenance gate (a skipped-shaped response, mirrored from the mutation
   path), then reads the user's AnkiWeb credentials from the account record
   (raising BEFORE any ledger write when they are missing), and delegates to
   ``registry.sync_user(username, email, password, on_step=...)``.

Ambiguous-outcome rule: a network-layer failure DURING the sync transfer is
surfaced by ``registry.sync_user`` as ``outcome_unknown: True`` — in that
case the idempotency key is deliberately NOT recorded (a "failed" record
would permanently poison the key while the true outcome is unknowable) and a
warning explains this to the client.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .collections import CollectionRegistry
from .config import Settings, get_settings
from .models import MutationResponse, SyncStatus
from .users import UserStore

logger = logging.getLogger("anki_mcp.sync_manager")

_REPLAY_WARNING = "Idempotent operation replay. Returned cached result from {}."
_LEDGER_WRITE_WARNING = "Failed to write to persistent idempotency ledger."
_UNSYNCED_CHANGES_WARNING = (
    "Changes committed to the local collection. They are pushed to AnkiWeb "
    "automatically by the next pre-tool sync (a full two-way sync, at most "
    "one SYNC_BEFORE_TOOLS_TTL_SECONDS window later), or immediately by "
    "calling sync_collection."
)

# Upper bound on how long sync_before_tool WAITS for a concurrent-sync slot
# before giving up for this call. The pre-tool sync must never queue a tool
# call behind other users' full syncs: on timeout the tool proceeds on local
# data (fail-open, same as a failed sync) and the next TTL-window expiry
# retries. The bound lives inside registry.sync_user (semaphore-acquire
# timeout) — cancelling mid-sync would release the user lock while the sync
# thread keeps writing (Collection is not thread-safe).
PRETOOL_SYNC_ACQUIRE_TIMEOUT_SECONDS = 15.0


class SyncManager:
    """Coordinates mutation write-safety and explicit per-user synchronization."""

    def __init__(
        self,
        registry: CollectionRegistry,
        store: UserStore,
        settings: Optional[Settings] = None,
    ):
        self.registry = registry
        self.store = store
        self.settings = settings or get_settings()
        self._maintenance_mode = self.settings.maintenance_mode
        # Per-user asyncio locks serializing the ledger check → mutate → record
        # sequence of execute_mutation (same-key replay safety). Locks are
        # created lazily under the small guard; the guard never covers the
        # mutation itself, and different users get different locks.
        self._locks_guard = asyncio.Lock()
        self._mutation_locks: Dict[str, asyncio.Lock] = {}
        # Pre-tool sync (sync_before_tool): per-user monotonic timestamp of the
        # last attempt, guarded by one small lock so the TTL check-and-set is
        # atomic. The timestamp is recorded BEFORE the sync runs (and kept even
        # on failure): a failed attempt must also throttle, or every tool call
        # would pay a full network timeout while AnkiWeb is down.
        self._pretool_guard = asyncio.Lock()
        self._last_pretool_sync: Dict[str, float] = {}

    @property
    def maintenance_mode(self) -> bool:
        return self._maintenance_mode

    def set_maintenance_mode(self, enabled: bool) -> None:
        """Enable or disable maintenance mode."""
        self._maintenance_mode = enabled
        logger.info("Maintenance mode set to: %s", enabled)

    # -- credentials -----------------------------------------------------------

    def _user_credentials(self, username: str) -> Tuple[str, str]:
        """The account's stored AnkiWeb (email, password).

        Raises ValueError BEFORE any ledger write when the account is unknown
        or has not opted into sync by saving credentials. This check must run
        first so a missing-credentials request can never consume or poison an
        idempotency key.
        """
        user = self.store.get_user(username)
        if user is None:
            raise ValueError(f"Unknown user: {username!r}")
        if not user.ankiweb_email or not user.ankiweb_password:
            raise ValueError(
                f"AnkiWeb credentials not set for user {username!r}; "
                "save them in the web portal before syncing."
            )
        return user.ankiweb_email, user.ankiweb_password

    def _sync_nudge(self, username: str) -> Optional[str]:
        """The sync-nudge warning appended to committed mutation responses.

        None when the account has no saved AnkiWeb credentials — sync_collection
        would only fail for them, so there is nothing actionable to point the
        calling model at.
        """
        user = self.store.get_user(username)
        if user is None or not (user.ankiweb_email and user.ankiweb_password):
            return None
        return _UNSYNCED_CHANGES_WARNING

    @staticmethod
    def _on_step(username: str) -> Callable[[str], None]:
        """Defensive sync progress logger: must never raise (Task 3 contract)."""

        def log_step(step: str) -> None:
            try:
                logger.info("AnkiWeb sync for %r: step %r", username, step)
            except Exception:  # pragma: no cover - logging must not break sync
                pass

        return log_step

    # -- explicit sync ----------------------------------------------------------

    async def sync_before_tool(self, username: str) -> None:
        """Two-way sync the user's collection with AnkiWeb before a tool call.

        This is a FULL two-way sync, not a pull: besides pulling remote
        changes the tool is about to need, it also PUSHES the local changes
        made since the last window — automatic AnkiWeb egress, disclosed in
        every client-facing contract surface.

        Contract — fail-open, never blocking, never raising:
        - no-op when the feature is disabled (SYNC_BEFORE_TOOLS=false), when
          the worker is in maintenance mode, when the account is unknown or
          has no saved AnkiWeb credentials (sync stays opt-in per user), or
          when the last attempt is younger than the TTL window
          (SYNC_BEFORE_TOOLS_TTL_SECONDS; 0 = sync before every call);
        - otherwise delegates to registry.sync_user (which snapshots first,
          then runs the two-way delta sync — pulling remote changes AND
          pushing pending local ones) and swallows every failure with a log
          line: the tool call proceeds on local data. A failed attempt still
          consumes the TTL window, so a reachable-but-broken AnkiWeb cannot
          slow every tool call with back-to-back retries;
        - the WAIT for a concurrent-sync slot is bounded by
          PRETOOL_SYNC_ACQUIRE_TIMEOUT_SECONDS (forwarded as acquire_timeout
          to registry.sync_user): a busy slot skips this sync (warning log)
          instead of queueing the tool call behind other users' full syncs.

        Called from main's _tool_guard ahead of every tool except
        sync_collection (that tool IS the sync) and except tools whose
        account is denied sync_collection by a tool rule (the rule doubles
        as the per-tool opt-out from automatic AnkiWeb egress).
        """
        if not self.settings.sync_before_tools:
            return
        if self._maintenance_mode:
            return
        user = self.store.get_user(username)
        if user is None or not (user.ankiweb_email and user.ankiweb_password):
            return
        async with self._pretool_guard:
            now = time.monotonic()
            last = self._last_pretool_sync.get(username)
            if last is not None and (now - last) < self.settings.sync_before_tools_ttl_seconds:
                return
            self._last_pretool_sync[username] = now
        try:
            result = await self.registry.sync_user(
                username,
                user.ankiweb_email,
                user.ankiweb_password,
                on_step=self._on_step(username),
                acquire_timeout=PRETOOL_SYNC_ACQUIRE_TIMEOUT_SECONDS,
            )
            if result.get("success"):
                logger.debug("Pre-tool sync for %r ok", username)
            else:
                logger.warning(
                    "Pre-tool sync for %r failed (tool proceeds on local data): %s",
                    username,
                    result.get("error"),
                )
        except Exception as exc:
            logger.error(
                "Pre-tool sync for %r raised (tool proceeds on local data): %s", username, exc
            )

    async def sync_now(
        self,
        username: str,
        idempotency_key: Optional[str] = None,
        full_sync_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Synchronize the user's collection with AnkiWeb.

        Response shape (schema-compatible with the legacy ``sync_collection``
        tool output): ``{"success", "sync_status", "message", "warnings"}``.
        ``idempotency_key`` is accepted for replay/ledger semantics but the
        MCP surface passes none (the tool takes no arguments, unchanged).
        ``full_sync_direction`` ("upload"/"download") is forwarded to
        :meth:`CollectionRegistry.sync_user` and only matters when AnkiWeb
        demands a user-chosen full-sync direction; a failure of that kind
        carries ``full_sync_required: True`` back to the client.
        """
        if self._maintenance_mode:
            # Same gate as the mutation path, translated to this method's dict
            # response shape: everything skipped, no sync attempted, no ledger
            # write (the key is not consumed).
            logger.warning("Rejected sync for %r: worker is in maintenance mode", username)
            return {
                "success": False,
                "sync_status": SyncStatus(pre_sync="skipped", post_sync="skipped").model_dump(),
                "message": "Worker is currently in maintenance mode; writes are paused.",
                "warnings": [],
            }

        email, password = self._user_credentials(username)
        warnings: List[str] = []
        ledger = self.registry.ledger(username)

        if idempotency_key:
            existing = await ledger.get(idempotency_key)
            if existing:
                status = existing.get("status")
                if status == "committed":
                    # %r + truncate: the key is client-controlled and must never
                    # split or bloat a log record (CWE-117).
                    logger.info("Idempotency hit for sync key %r: returning cached committed response", idempotency_key[:64])
                    return {
                        "success": True,
                        "sync_status": SyncStatus(pre_sync="skipped", post_sync="skipped").model_dump(),
                        "message": "Synchronization completed successfully.",
                        "data": existing.get("data"),
                        "warnings": [_REPLAY_WARNING.format(existing.get("timestamp"))],
                    }
                if status == "failed":
                    logger.info("Idempotency hit for sync key %r: returning cached failure", idempotency_key[:64])
                    error = existing.get("error", "Previous attempt with this idempotency key failed.")
                    return {
                        "success": False,
                        "sync_status": SyncStatus(pre_sync="skipped", post_sync="skipped").model_dump(),
                        "message": f"Synchronization error: {error}",
                        "warnings": [_REPLAY_WARNING.format(existing.get("timestamp"))],
                    }

        result = await self.registry.sync_user(
            username, email, password, on_step=self._on_step(username),
            full_sync_direction=full_sync_direction,
        )

        if result.get("success"):
            data = {"output": result.get("output", "")}
            if idempotency_key:
                try:
                    await ledger.record_success(idempotency_key, data)
                except Exception as e:
                    logger.error("Failed to commit idempotency record for sync key '%s': %s", idempotency_key, e)
                    warnings.append(_LEDGER_WRITE_WARNING)
            logger.info("Manual synchronization with AnkiWeb completed for %r", username)
            return {
                "success": True,
                "sync_status": SyncStatus(pre_sync="ok", post_sync="ok").model_dump(),
                "message": "Synchronization completed successfully.",
                "warnings": warnings,
            }

        err_msg = str(result.get("error", "unknown error"))
        if result.get("outcome_unknown"):
            # Network-layer failure mid-transfer: NO ledger record, and the
            # client must learn the outcome is unknowable.
            logger.error("Synchronization outcome UNKNOWN for %r: %s", username, err_msg)
            warnings.append(
                "The synchronization transfer may or may not have been applied; the outcome is "
                "UNKNOWN. The idempotency key was NOT recorded. Verify the AnkiWeb state before "
                "retrying with the same key."
            )
        elif idempotency_key:
            try:
                await ledger.record_failure(idempotency_key, err_msg)
            except Exception as e:
                logger.error("Failed to record idempotency failure for sync key '%s': %s", idempotency_key, e)
                warnings.append(_LEDGER_WRITE_WARNING)
        else:
            logger.error("Synchronization failed for %r: %s", username, err_msg)
        response: Dict[str, Any] = {
            "success": False,
            "sync_status": SyncStatus(pre_sync="ok", post_sync="failed", post_sync_error=err_msg).model_dump(),
            "message": f"Synchronization error: {err_msg}",
            "warnings": warnings,
        }
        if result.get("outcome_unknown"):
            response["outcome_unknown"] = True
        if result.get("full_sync_required"):
            # Lets MCP clients (and the LLM driving them) retry with an
            # explicit full_sync_direction instead of a blind re-call.
            response["full_sync_required"] = True
        return response

    # -- mutation write-safety -----------------------------------------------------

    async def _user_mutation_lock(self, username: str) -> asyncio.Lock:
        """The per-user lock making the check → mutate → record sequence atomic.

        Two concurrent callers sharing ONE idempotency key would otherwise both
        miss in the ledger (the first has not recorded yet) and double-apply:
        the registry's per-user lock serializes only the mutation itself, not
        the surrounding ledger check and record. Locks are created lazily under
        the guard so concurrent first-time callers share one lock per username;
        different users get different locks and are never serialized against
        each other.
        """
        async with self._locks_guard:
            lock = self._mutation_locks.get(username)
            if lock is None:
                lock = asyncio.Lock()
                self._mutation_locks[username] = lock
            return lock

    async def execute_mutation(
        self,
        username: str,
        operation_name: str,
        mutation_fn: Callable[[], Awaitable[Any]],
        idempotency_key: Optional[str] = None,
    ) -> MutationResponse:
        """Execute a state-mutating operation with per-user write-safety semantics:

        1. Check maintenance mode.
        2. Under the user's mutation lock: check the user's idempotency ledger
           for duplicate requests.
        3. Apply the mutation to the user's local collection (the registry's
           per-user lock serializes concurrent writers on the collection file).
        4. Commit record to the idempotency ledger.
        5. Return the schema-compatible MutationResponse (sync_status stays
           fully "skipped": explicit sync is a separate tool now).
        """
        warnings: List[str] = []

        if self._maintenance_mode:
            logger.warning("Rejected '%s': worker is in maintenance mode", operation_name)
            return MutationResponse(
                success=False,
                local_status="skipped",
                sync_status=SyncStatus(),
                error="Worker is currently in maintenance mode; writes are paused.",
            )

        async with await self._user_mutation_lock(username):
            ledger = self.registry.ledger(username)

            if idempotency_key:
                existing = await ledger.get(idempotency_key)
                if existing:
                    status = existing.get("status")
                    if status == "committed":
                        # %r + truncate: the key is client-controlled and must
                        # never split or bloat a log record (CWE-117).
                        logger.info("Idempotency hit for key %r: returning cached committed response", idempotency_key[:64])
                        replay_warnings = [_REPLAY_WARNING.format(existing.get("timestamp"))]
                        nudge = self._sync_nudge(username)
                        if nudge:
                            replay_warnings.append(nudge)
                        return MutationResponse(
                            success=True,
                            local_status="committed",
                            sync_status=SyncStatus(pre_sync="skipped", post_sync="skipped"),
                            data=existing.get("data"),
                            warnings=replay_warnings,
                        )
                    if status == "failed":
                        logger.info("Idempotency hit for key %r: returning cached failure", idempotency_key[:64])
                        return MutationResponse(
                            success=False,
                            local_status="failed",
                            sync_status=SyncStatus(pre_sync="skipped", post_sync="skipped"),
                            error=existing.get("error", "Previous attempt with this idempotency key failed."),
                            warnings=[_REPLAY_WARNING.format(existing.get("timestamp"))],
                        )

            local_data: Any = None
            try:
                logger.info("Applying local mutation for '%s' (user %r)...", operation_name, username)
                local_data = await mutation_fn()
                local_status = "committed"
            except Exception as e:
                local_status = "failed"
                err_msg = str(e)
                # %r (not %s): err_msg embeds client-controlled content VERBATIM
                # (e.g. a find_and_replace regex=true pattern inside a backend
                # regex parse error) — %s would let one tool call author
                # multi-line log records (CWE-117). The client-facing error
                # below keeps the raw text: a structured JSON field is one value.
                logger.error("Local mutation '%s' failed: %r", operation_name, err_msg)
                if idempotency_key:
                    try:
                        await ledger.record_failure(idempotency_key, err_msg)
                    except Exception as ledger_exc:
                        logger.error("Failed to record idempotency failure for key '%s': %s", idempotency_key, ledger_exc)
                        warnings.append(_LEDGER_WRITE_WARNING)
                return MutationResponse(
                    success=False,
                    local_status=local_status,
                    sync_status=SyncStatus(),
                    error=err_msg,
                    warnings=warnings,
                )

            if idempotency_key:
                try:
                    await ledger.record_success(idempotency_key, local_data)
                except Exception as e:
                    logger.error("Failed to commit idempotency record for key '%s': %s", idempotency_key, e)
                    warnings.append(_LEDGER_WRITE_WARNING)

            nudge = self._sync_nudge(username)
            if nudge:
                warnings.append(nudge)

            return MutationResponse(
                success=True,
                local_status=local_status,
                sync_status=SyncStatus(),
                data=local_data,
                warnings=warnings,
            )
