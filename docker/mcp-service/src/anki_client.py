"""Async client for communicating with AnkiConnect."""

import logging
from typing import Any, Dict, Optional
import httpx

from .config import Settings, get_settings
from .logging_util import sanitize_data

logger = logging.getLogger("anki_mcp.client")


class AnkiConnectError(Exception):
    """Exception raised when AnkiConnect returns an error in the response body."""
    pass


class AnkiConnectConnectionError(Exception):
    """Exception raised when unable to connect to AnkiConnect service."""
    pass


class AnkiSyncAuthNotConfiguredError(AnkiConnectError):
    """Exception raised specifically when AnkiWeb synchronization account is not configured."""
    pass


class AnkiClient:
    """HTTP client interface for AnkiConnect."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.settings.anki_connect_url,
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def invoke(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Execute an AnkiConnect action and return the result.
        
        Does NOT blindly retry mutations to guarantee write safety.
        """
        payload: Dict[str, Any] = {
            "action": action,
            "version": 6,
        }
        if params is not None:
            payload["params"] = params
        if self.settings.anki_connect_api_key:
            payload["key"] = self.settings.anki_connect_api_key

        client = await self.get_client()
        call_timeout = timeout or self.settings.request_timeout_seconds

        logger.debug("Calling AnkiConnect action '%s' with params: %s", action, sanitize_data(params))
        try:
            response = await client.post("/", json=payload, timeout=call_timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.error("Failed to connect to AnkiConnect at %s: %s", self.settings.anki_connect_url, e)
            raise AnkiConnectConnectionError(
                f"Could not connect to Anki Desktop at {self.settings.anki_connect_url}. "
                "Ensure anki-worker container is running and Anki Desktop has started."
            ) from e
        except httpx.TimeoutException as e:
            logger.error("AnkiConnect action '%s' timed out after %ss: %s", action, call_timeout, e)
            raise AnkiConnectConnectionError(
                f"AnkiConnect timed out while executing '{action}'."
            ) from e
        except httpx.HTTPError as e:
            logger.error("AnkiConnect HTTP communication error for '%s': %s", action, e)
            raise AnkiConnectConnectionError(f"HTTP communication error: {e}") from e

        if response.status_code != 200:
            logger.error("AnkiConnect returned unexpected HTTP status %s: %s", response.status_code, response.text)
            raise AnkiConnectConnectionError(
                f"AnkiConnect returned HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
        except Exception as e:
            logger.error("AnkiConnect returned invalid JSON response: %s", response.text)
            raise AnkiConnectError(f"Invalid JSON returned from AnkiConnect: {e}") from e

        if not isinstance(data, dict):
            raise AnkiConnectError(f"Malformed response from AnkiConnect (expected JSON object): {data}")

        error = data.get("error")
        if error is not None:
            err_str = str(error)
            logger.warning("AnkiConnect action '%s' returned error: %s", action, err_str)
            if "sync: auth not configured" in err_str.lower():
                raise AnkiSyncAuthNotConfiguredError(err_str)
            raise AnkiConnectError(err_str)

        return data.get("result")

    async def check_health(self) -> bool:
        """Check if AnkiConnect is reachable and responsive."""
        try:
            res = await self.invoke("version", timeout=3.0)
            return bool(res)
        except Exception:
            return False
