"""SyncManager coordinates operation serialization, maintenance checks, and sync safety."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any, List, Optional

from .anki_client import (
    AnkiClient,
    AnkiConnectError,
    AnkiSyncAuthNotConfiguredError,
)
from .config import Settings, get_settings
from .ledger import IdempotencyLedger
from .models import MutationResponse, SyncStatus

logger = logging.getLogger("anki_mcp.sync_manager")


class SyncManager:
    """Coordinates mutation jobs, pre-sync, post-sync, and operation serialization."""

    def __init__(
        self,
        anki_client: AnkiClient,
        ledger: IdempotencyLedger,
        settings: Optional[Settings] = None,
    ):
        self.client = anki_client
        self.ledger = ledger
        self.settings = settings or get_settings()
        self._lock = asyncio.Lock()
        self._maintenance_mode = self.settings.maintenance_mode

    @property
    def maintenance_mode(self) -> bool:
        return self._maintenance_mode

    def set_maintenance_mode(self, enabled: bool) -> None:
        """Enable or disable maintenance mode."""
        self._maintenance_mode = enabled
        logger.info("Maintenance mode set to: %s", enabled)

    async def sync_now(self) -> SyncStatus:
        """Execute an explicit collection synchronization under operation lock."""
        async with self._lock:
            if self._maintenance_mode:
                return SyncStatus(
                    pre_sync="skipped",
                    post_sync="skipped",
                    pre_sync_error="Worker is in maintenance mode",
                )

            status = SyncStatus()
            try:
                await self.client.invoke("sync", timeout=self.settings.sync_timeout_seconds)
                status.post_sync = "ok"
                logger.info("Manual synchronization with AnkiWeb completed successfully")
            except AnkiSyncAuthNotConfiguredError as e:
                status.post_sync = "auth_not_configured"
                status.post_sync_error = str(e)
                logger.warning("Synchronization skipped: official AnkiWeb account is not configured in GUI")
            except Exception as e:
                status.post_sync = "failed"
                status.post_sync_error = str(e)
                logger.error("Synchronization failed: %s", e)

            return status

    async def execute_mutation(
        self,
        operation_name: str,
        mutation_fn: Callable[[], Awaitable[Any]],
        idempotency_key: Optional[str] = None,
    ) -> MutationResponse:
        """Execute a state-mutating operation following strict write-safety semantics:
        
        1. Acquire operation lock.
        2. Check maintenance mode.
        3. Check idempotency ledger for duplicate requests.
        4. Request pre-synchronization with AnkiWeb.
        5. Validate and apply the mutation to Anki Desktop.
        6. Commit record to idempotency ledger.
        7. Request post-synchronization with AnkiWeb.
        8. Return local status and synchronization status separately.
        """
        async with self._lock:
            warnings: List[str] = []
            sync_status = SyncStatus()

            # 2. Check maintenance mode
            if self._maintenance_mode:
                logger.warning("Rejected '%s': worker is in maintenance mode", operation_name)
                return MutationResponse(
                    success=False,
                    local_status="skipped",
                    sync_status=sync_status,
                    error="Worker is currently in maintenance mode; writes are paused.",
                )

            # 3. Check idempotency ledger
            if idempotency_key:
                existing = await self.ledger.get(idempotency_key)
                if existing:
                    status = existing.get("status")
                    if status == "committed":
                        logger.info("Idempotency hit for key '%s': returning cached committed response", idempotency_key)
                        return MutationResponse(
                            success=True,
                            local_status="committed",
                            sync_status=SyncStatus(pre_sync="skipped", post_sync="skipped"),
                            data=existing.get("data"),
                            warnings=[f"Idempotent operation replay. Returned cached result from {existing.get('timestamp')}."],
                        )
                    elif status == "failed":
                        logger.info("Idempotency hit for key '%s': returning cached failure", idempotency_key)
                        return MutationResponse(
                            success=False,
                            local_status="failed",
                            sync_status=SyncStatus(pre_sync="skipped", post_sync="skipped"),
                            error=existing.get("error", "Previous attempt with this idempotency key failed."),
                        )

            # 4. Request pre-synchronization
            auth_configured = True
            try:
                logger.info("Requesting pre-mutation synchronization for '%s'...", operation_name)
                await self.client.invoke("sync", timeout=self.settings.sync_timeout_seconds)
                sync_status.pre_sync = "ok"
            except AnkiSyncAuthNotConfiguredError as e:
                auth_configured = False
                sync_status.pre_sync = "auth_not_configured"
                sync_status.pre_sync_error = str(e)
                warnings.append("AnkiWeb account not configured in GUI. Operating on local container collection only.")
                logger.info("Pre-sync skipped: AnkiWeb credentials not configured in GUI")
            except Exception as e:
                sync_status.pre_sync = "failed"
                sync_status.pre_sync_error = str(e)
                warnings.append(f"Pre-mutation synchronization failed ({e}). Proceeding with local write.")
                logger.warning("Pre-mutation synchronization failed for '%s': %s", operation_name, e)

            # 5. Apply local mutation
            local_data: Any = None
            try:
                logger.info("Applying local mutation for '%s'...", operation_name)
                local_data = await mutation_fn()
                local_status = "committed"
            except Exception as e:
                local_status = "failed"
                err_msg = str(e)
                logger.error("Local mutation '%s' failed: %s", operation_name, err_msg)
                if idempotency_key:
                    await self.ledger.record_failure(idempotency_key, err_msg)
                return MutationResponse(
                    success=False,
                    local_status=local_status,
                    sync_status=sync_status,
                    error=err_msg,
                    warnings=warnings,
                )

            # 6. Commit to idempotency ledger
            if idempotency_key:
                try:
                    await self.ledger.record_success(idempotency_key, local_data)
                except Exception as e:
                    logger.error("Failed to commit idempotency record for key '%s': %s", idempotency_key, e)
                    warnings.append("Failed to write to persistent idempotency ledger.")

            # 7. Request post-synchronization
            if auth_configured and sync_status.pre_sync != "failed":
                try:
                    logger.info("Requesting post-mutation synchronization for '%s'...", operation_name)
                    await self.client.invoke("sync", timeout=self.settings.sync_timeout_seconds)
                    sync_status.post_sync = "ok"
                except Exception as e:
                    sync_status.post_sync = "failed"
                    sync_status.post_sync_error = str(e)
                    warning_msg = (
                        "Local write succeeded in Anki Desktop, but post-mutation synchronization "
                        f"to AnkiWeb failed: {e}. Changes are safely committed locally and will synchronize on the next sync cycle."
                    )
                    warnings.append(warning_msg)
                    logger.warning("Post-mutation sync failed for '%s': %s", operation_name, e)
            else:
                sync_status.post_sync = "skipped"

            # 8. Return response
            return MutationResponse(
                success=True,
                local_status=local_status,
                sync_status=sync_status,
                data=local_data,
                warnings=warnings,
            )
