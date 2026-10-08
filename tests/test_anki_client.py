"""Unit tests for AnkiClient."""

import httpx
import pytest
import respx
from src.anki_client import (
    AnkiClient,
    AnkiConnectConnectionError,
    AnkiConnectError,
    AnkiSyncAuthNotConfiguredError,
)
from src.config import Settings


@pytest.fixture
def client():
    settings = Settings(
        anki_connect_url="http://mock-anki:8765",
        anki_connect_api_key="secret-key",
        request_timeout_seconds=5.0,
    )
    return AnkiClient(settings)


@pytest.mark.asyncio
@respx.mock
async def test_invoke_success(client):
    respx.post("http://mock-anki:8765/").mock(
        return_value=httpx.Response(200, json={"result": ["Default", "Spanish"], "error": None})
    )

    result = await client.invoke("deckNames")
    assert result == ["Default", "Spanish"]

    # Verify request payload included api key and version
    request = respx.calls.last.request
    import json
    body = json.loads(request.content)
    assert body["action"] == "deckNames"
    assert body["version"] == 6
    assert body["key"] == "secret-key"


@pytest.mark.asyncio
@respx.mock
async def test_invoke_error(client):
    respx.post("http://mock-anki:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": "deck 'NonExistent' not found"})
    )

    with pytest.raises(AnkiConnectError, match="deck 'NonExistent' not found"):
        await client.invoke("changeDeck", {"cards": [1], "deck": "NonExistent"})


@pytest.mark.asyncio
@respx.mock
async def test_invoke_sync_auth_error(client):
    respx.post("http://mock-anki:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": "sync: auth not configured"})
    )

    with pytest.raises(AnkiSyncAuthNotConfiguredError):
        await client.invoke("sync")


@pytest.mark.asyncio
@respx.mock
async def test_invoke_connection_failure(client):
    respx.post("http://mock-anki:8765/").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    with pytest.raises(AnkiConnectConnectionError, match="Could not connect to Anki Desktop"):
        await client.invoke("version")
