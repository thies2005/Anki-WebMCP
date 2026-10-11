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


def _make_settings(tmp_path) -> Settings:
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(tmp_path / "users.json"),
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
def app_with_users(tmp_path):
    """App plus alice/bob accounts; returns (app, settings, alice_token, bob_token)."""
    settings = _make_settings(tmp_path)
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
async def test_search_notes_tool_schema_exposes_only_real_arguments(app_with_users):
    """tools/list for search_notes: exactly {query, limit, offset}.

    The ``ctx: Context`` parameter (and any auth state) must never leak into
    the input schema — an SDK upgrade changing the Context exclusion would
    otherwise surface a bogus ``ctx``/``username`` argument to clients.
    """
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            list_res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 30, "method": "tools/list",
            })
            assert list_res.status_code == 200
            payload = _parse_mcp_response(list_res)
            tools = {t["name"]: t for t in payload["result"]["tools"]}
            assert set(tools["search_notes"]["inputSchema"]["properties"]) == {"query", "limit", "offset"}


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


# -- scenario 5: sync discoverability (instructions, descriptions, nudge) ------------


@pytest.mark.asyncio
async def test_initialize_result_carries_sync_guidance_instructions(app_with_users):
    """The initialize handshake must teach clients the local-until-synced rule.

    Connected models (claude.ai, Perplexity, ...) only see the server through
    tools; without ``instructions`` naming sync_collection they cannot know a
    separate call is required to reach AnkiWeb.
    """
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = {
                "Authorization": f"Bearer {alice_token}",
                "Accept": "application/json, text/event-stream",
            }
            init_res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            })
            assert init_res.status_code == 200
            result = _parse_mcp_response(init_res)["result"]
            instructions = result.get("instructions") or ""
            assert "sync_collection" in instructions
            assert "AnkiWeb" in instructions


# Every tool that changes collection state; each description must tell the
# model that the change is local until sync_collection runs.
MUTATING_TOOLS = {
    "add_notes", "update_note_fields", "add_tags", "remove_tags",
    "create_deck", "rename_deck", "delete_deck", "delete_notes",
    "create_note_type", "suspend_cards", "unsuspend_cards",
    "set_due_date", "move_cards", "answer_card", "store_media_file",
}


@pytest.mark.asyncio
async def test_tool_descriptions_teach_local_only_semantics(app_with_users):
    """Every mutating tool's description names sync_collection; sync_collection
    itself explains when to call it; read-only tools stay untouched."""
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            list_res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 40, "method": "tools/list",
            })
            assert list_res.status_code == 200
            payload = _parse_mcp_response(list_res)
            tools = {t["name"]: t for t in payload["result"]["tools"]}

            missing = sorted(
                name for name in MUTATING_TOOLS
                if "sync_collection" not in tools[name].get("description", "")
            )
            assert missing == [], f"mutating tools lacking sync guidance: {missing}"

            # sync_collection explains WHEN to call it, not just that it exists
            sync_desc = tools["sync_collection"]["description"]
            assert "after" in sync_desc.lower()

            # Read-only exemplars keep their descriptions free of the suffix
            for readonly in ("list_decks", "search_notes", "get_note"):
                assert "sync_collection" not in tools[readonly]["description"]
