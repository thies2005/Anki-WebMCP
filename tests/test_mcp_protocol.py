"""End-to-end MCP protocol tests over the Streamable HTTP transport (in-process ASGI).

Keeps the three legacy scenarios' spirit — structured error on collection-layer
failure, empty add_notes batch, invalid search limit — now over per-user token
auth, and adds the Task 5 keystone proof: two users, end-to-end isolation.
"""

import json
import os

import httpx
import pytest

from src.config import Settings
from src.main import create_app
from src.users import UserStore

SECRET = "test-session-secret"


def _parse_mcp_response(res: httpx.Response) -> dict:
    if res.headers.get("content-type", "").startswith("text/event-stream"):
        for line in res.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"No SSE data line in response: {res.text!r}")
    return res.json()


def _make_settings(temp_ledger_file, tmp_path) -> Settings:
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(tmp_path / "users.json"),
        idempotency_ledger_path=temp_ledger_file,
    )


async def _init_session(client: httpx.AsyncClient, token: str, request_id: int = 1) -> dict:
    """Run the initialize handshake for ``token``; returns the request headers."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    init_res = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": request_id, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    })
    assert init_res.status_code == 200
    session_id = init_res.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


async def _call_tool(client: httpx.AsyncClient, headers: dict, name: str, arguments: dict, request_id: int = 2) -> dict:
    call_res = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert call_res.status_code == 200
    return _parse_mcp_response(call_res)


@pytest.fixture
def app_with_users(temp_ledger_file, tmp_path):
    """App plus alice/bob accounts; returns (app, settings, alice_token, bob_token)."""
    settings = _make_settings(temp_ledger_file, tmp_path)
    store = UserStore(settings.users_file)
    _, alice_token = store.create_user("alice", "alice-pw")
    _, bob_token = store.create_user("bob", "bob-pw")
    app = create_app(settings)
    return app, settings, alice_token, bob_token


# -- scenario 1: collection-layer failure surfaces as structured error -------------


@pytest.mark.asyncio
async def test_tools_call_returns_structured_error_when_registry_fails(
    app_with_users, monkeypatch
):
    app, settings, alice_token, _ = app_with_users

    async def boom(username, fn, *args, **kwargs):
        raise RuntimeError("Registry exploded")

    monkeypatch.setattr(app.state.anki_registry, "run", boom)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            payload = await _call_tool(client, headers, "list_decks", {})
            text = payload["result"]["content"][0]["text"]
            # The upstream failure message must reach the client as structured
            # JSON, not the SDK's generic "Error executing tool" mask.
            assert "Error executing tool" not in text
            structured = json.loads(text)
            assert structured.get("success") is False
            assert structured.get("error_type") == "RuntimeError"
            assert "Registry exploded" in structured.get("error", "")


# -- scenario 2: empty add_notes batch ----------------------------------------------


@pytest.mark.asyncio
async def test_add_notes_empty_batch_returns_structured_protocol_error(
    app_with_users,
):
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            payload = await _call_tool(client, headers, "add_notes", {"notes": []})
            text = payload["result"]["content"][0]["text"]
            assert "Error executing tool" not in text
            structured = json.loads(text)
            assert structured.get("success") is False
            assert structured.get("error_type") == "ValidationError"
            # No collection may have been opened/created by the rejected batch
            assert not os.path.exists(os.path.join(settings.data_dir, "users", "alice"))


# -- scenario 3: invalid search limit ------------------------------------------------


@pytest.mark.asyncio
async def test_search_notes_invalid_limit_returns_structured_protocol_error(
    app_with_users,
):
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            payload = await _call_tool(
                client, headers, "search_notes", {"query": "deck:Default", "limit": 0}
            )
            text = payload["result"]["content"][0]["text"]
            assert "Error executing tool" not in text
            structured = json.loads(text)
            assert structured.get("success") is False
            assert structured.get("error_type") == "ValidationError"


# -- scenario 4 (keystone): two users, end-to-end isolation ---------------------------


@pytest.mark.asyncio
async def test_user_scoping_end_to_end_two_users_isolated(app_with_users):
    """Alice adds a note over the MCP protocol; Bob must not see it.

    Proves the middleware's request.state.username reaches the tool wrappers
    and scopes every collection operation to the authenticated account.
    """
    app, settings, alice_token, bob_token = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            alice_headers = await _init_session(client, alice_token)
            bob_headers = await _init_session(client, bob_token, request_id=10)

            # Alice adds one note
            payload = await _call_tool(client, alice_headers, "add_notes", {
                "notes": [{"deck_name": "Default", "model_name": "Basic",
                           "fields": {"Front": "alice secret", "Back": "x"}}],
            }, request_id=11)
            added = json.loads(payload["result"]["content"][0]["text"])
            assert added["success"] is True
            alice_note_id = added["data"]["created_note_ids"][0]

            # Alice sees it
            alice_search = json.loads((await _call_tool(
                client, alice_headers, "search_notes", {"query": "secret"}, request_id=12
            ))["result"]["content"][0]["text"])
            assert alice_search["total_matches"] == 1
            assert alice_search["notes"][0]["fields"]["Front"] == "alice secret"

            # Bob does not see it — neither by search nor by id
            bob_search = json.loads((await _call_tool(
                client, bob_headers, "search_notes", {"query": "secret"}, request_id=13
            ))["result"]["content"][0]["text"])
            assert bob_search["total_matches"] == 0
            assert bob_search["notes"] == []

            bob_get = json.loads((await _call_tool(
                client, bob_headers, "get_note", {"note_id": alice_note_id}, request_id=14
            ))["result"]["content"][0]["text"])
            assert bob_get["success"] is False
            assert "not found" in bob_get["error"]

            # Sanity: bob's own collection works and knows only the default deck
            bob_decks = json.loads((await _call_tool(
                client, bob_headers, "list_decks", {}, request_id=15
            ))["result"]["content"][0]["text"])
            assert bob_decks["count"] == 1
            assert bob_decks["decks"][0]["name"] == "Default"

            # Alice's stats see her note; her collection lives under her own dir
            alice_stats = json.loads((await _call_tool(
                client, alice_headers, "get_collection_stats", {}, request_id=16
            ))["result"]["content"][0]["text"])
            assert alice_stats["summary"]["total_cards"] == 1
            assert os.path.isfile(os.path.join(settings.data_dir, "users", "alice", "collection.anki2"))
            assert os.path.isfile(os.path.join(settings.data_dir, "users", "bob", "collection.anki2"))
