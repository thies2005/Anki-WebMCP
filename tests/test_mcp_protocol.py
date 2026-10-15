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
            # No collection may have been opened/created by the rejected batch.
            # (The user directory itself may exist since the tool-usage log
            # moved under users/<name>/ — the collection file is the precise
            # invariant.)
            assert not os.path.isfile(
                os.path.join(settings.data_dir, "users", "alice", "collection.anki2")
            )


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
            assert set(tools["search_notes"]["inputSchema"]["properties"]) == {"query", "limit", "offset", "fields"}
            # The batch getter exposes exactly its real arguments.
            assert set(tools["get_notes"]["inputSchema"]["properties"]) == {"note_ids"}
            # The search description must advertise that results carry all
            # field values — otherwise clients fall back to find_cards plus
            # one get_note per card — and steer them to the batch getter.
            search_desc = tools["search_notes"]["description"]
            assert "all field values" in search_desc
            assert "get_note" in search_desc
            assert "fields" in search_desc
            # The note-type change pair exposes exactly their real arguments —
            # in particular no ``ctx`` leak and no auth state.
            assert set(tools["preview_note_type_change"]["inputSchema"]["properties"]) == {
                "new_note_type", "note_ids", "query", "field_map", "template_map",
            }
            assert set(tools["change_note_type"]["inputSchema"]["properties"]) == {
                "new_note_type", "note_ids", "query", "field_map", "template_map",
                "confirm", "idempotency_key",
            }
            # The note-type control set exposes exactly its real arguments —
            # in particular no ``ctx`` leak and no auth state.
            assert set(tools["get_note_type"]["inputSchema"]["properties"]) == {"name"}
            assert set(tools["update_note_type"]["inputSchema"]["properties"]) == {
                "name", "new_name", "css", "template_updates", "sort_field",
            }
            assert set(tools["remove_note_type_field"]["inputSchema"]["properties"]) == {
                "note_type", "field_name", "confirm",
            }
            assert set(tools["restore_note_type_to_stock"]["inputSchema"]["properties"]) == {
                "name", "force_kind", "confirm",
            }
            # The phase-4 scheduling set exposes exactly its real arguments —
            # in particular no ``ctx`` leak and no auth state.
            assert set(tools["get_deck_config"]["inputSchema"]["properties"]) == {"deck_id"}
            assert set(tools["update_deck_config"]["inputSchema"]["properties"]) == {
                "deck_id", "updates",
            }
            assert set(tools["set_card_flags"]["inputSchema"]["properties"]) == {
                "flag", "card_ids", "query",
            }
            assert set(tools["bury_cards"]["inputSchema"]["properties"]) == {"card_ids", "query"}
            assert set(tools["unbury_cards"]["inputSchema"]["properties"]) == {"card_ids", "query"}
            assert set(tools["reposition_new_cards"]["inputSchema"]["properties"]) == {
                "start", "step", "card_ids", "query", "randomize", "shift_existing",
            }
            # The read-only preview never carries the local-until-synced suffix,
            # and neither do the read-only note-type/deck-config detail tools.
            assert "sync_collection" not in tools["preview_note_type_change"]["description"]
            assert "sync_collection" not in tools["get_note_type"]["description"]
            assert "sync_collection" not in tools["get_deck_config"]["description"]
            # tools-3: the preview description tells clients to pass the same
            # maps they will pass to change_note_type.
            assert "field_map" in tools["preview_note_type_change"]["description"]
            assert "template_map" in tools["preview_note_type_change"]["description"]
            # Phase-4 disclosure pins: the unbury description must admit that
            # suspended cards in the selection are restored too, and the deck
            # config update must admit the preset is shared.
            assert "suspended" in tools["unbury_cards"]["description"]
            assert "SHARED" in tools["update_deck_config"]["description"]
            # tools-1 companion: `unburied` counts cards actually restored,
            # not the selection size.
            unbury_desc = tools["unbury_cards"]["description"]
            assert "unburied" in unbury_desc
            assert "actually restored" in unbury_desc


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
    """The initialize handshake must teach clients the sync contract.

    Connected models (claude.ai, Perplexity, ...) only see the server through
    tools; without ``instructions`` naming sync_collection and disclosing the
    automatic pre-tool two-way push, they cannot know when changes reach
    AnkiWeb.
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


# Every tool that changes collection state (32 as of the phase-4 scheduling
# set); each description must tell the model that the change is local until
# sync_collection runs.
MUTATING_TOOLS = {
    "add_notes", "update_note_fields", "add_tags", "remove_tags",
    "create_deck", "rename_deck", "delete_deck", "delete_notes",
    "create_note_type", "change_note_type", "find_and_replace",
    "update_note_type", "add_note_type_field", "remove_note_type_field",
    "rename_note_type_field", "reposition_note_type_field",
    "add_note_type_template", "remove_note_type_template",
    "reposition_note_type_template", "delete_note_type",
    "restore_note_type_to_stock",
    "suspend_cards", "unsuspend_cards",
    "set_due_date", "move_cards", "answer_card", "store_media_file",
    "update_deck_config", "set_card_flags",
    "bury_cards", "unbury_cards", "reposition_new_cards",
}

# The readOnlyHint complement of MUTATING_TOOLS: pure reads, annotated
# readOnlyHint=true so annotation-aware clients can treat them as reads
# instead of assuming every tool mutates. sync_collection is its own bucket:
# not in either set, annotated readOnlyHint=false (a two-way sync writes).
READ_ONLY_TOOLS = {
    "list_decks", "get_deck_config", "search_notes", "get_note", "get_notes",
    "find_duplicates", "list_note_types", "get_note_type",
    "preview_note_type_change", "find_cards", "get_card", "get_review_queue",
    "get_media_file", "list_media_files", "get_collection_stats",
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


@pytest.mark.asyncio
async def test_tool_annotations_readonly_hints(app_with_users):
    """tools/list annotations: reads declare readOnlyHint=true, no other tool
    claims to be a read.

    The union pin forces every registered tool into an explicit bucket — a
    future tool must join READ_ONLY_TOOLS, MUTATING_TOOLS, or be
    sync-like, or this test fails until someone decides what its
    annotation says.
    """
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            list_res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 45, "method": "tools/list",
            })
            assert list_res.status_code == 200
            payload = _parse_mcp_response(list_res)
            tools = {t["name"]: t for t in payload["result"]["tools"]}

            unbucketed = sorted(
                set(tools) - READ_ONLY_TOOLS - MUTATING_TOOLS - {"sync_collection"}
            )
            assert unbucketed == [], f"tools without an annotation bucket: {unbucketed}"

            for name, tool in tools.items():
                hints = tool.get("annotations") or {}
                if name in READ_ONLY_TOOLS:
                    assert hints.get("readOnlyHint") is True, (
                        f"{name} must declare readOnlyHint=true"
                    )
                else:
                    assert hints.get("readOnlyHint") is not True, (
                        f"{name} must not claim readOnlyHint=true"
                    )


@pytest.mark.asyncio
async def test_preview_note_type_change_forwards_field_map_over_mcp(app_with_users):
    """tools-3 wiring end-to-end: the preview tool's field_map/template_map
    arguments reach the resolver, so the preview reflects the MAPPED change
    (not the positional default) — exactly what change_note_type would apply
    with the same maps."""
    app, settings, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)

            created = json.loads((await _call_tool(client, headers, "create_note_type", {
                "name": "SrcRev", "fields": ["Alpha", "Beta"],
            }))["result"]["content"][0]["text"])
            assert created.get("success") is not False, created

            added = json.loads((await _call_tool(client, headers, "add_notes", {
                "notes": [{"deck_name": "Default", "note_type": "SrcRev",
                           "fields": {"Alpha": "frontish", "Beta": "backish"}}],
            }))["result"]["content"][0]["text"])
            assert added["success"] is True, added
            nid = added["data"]["created_note_ids"][0]

            payload = await _call_tool(client, headers, "preview_note_type_change", {
                "new_note_type": "Basic", "note_ids": [nid],
                "field_map": {"Front": "Beta", "Back": "Alpha"},
            })
            preview = json.loads(payload["result"]["content"][0]["text"])
            assert "field_map" in preview, preview  # read envelope: no success key
            # The MCP-layer map reached the resolver: the resolved field_map
            # is the SWAPPED mapping, not the positional default.
            assert preview["field_map"] == {"Front": "Beta", "Back": "Alpha"}

            # Same selection without maps: the positional default differs.
            payload = await _call_tool(client, headers, "preview_note_type_change", {
                "new_note_type": "Basic", "note_ids": [nid],
            })
            default = json.loads(payload["result"]["content"][0]["text"])
            assert default["field_map"] == {"Front": "Alpha", "Back": "Beta"}


@pytest.mark.asyncio
async def test_sync_collection_tool_supports_full_sync_direction(app_with_users, monkeypatch):
    """MCP access to the user-chosen full-sync direction: the tools/call
    argument flows through sync_now to registry.sync_user, and the FULL_SYNC
    refusal carries full_sync_required so the model can retry informed."""
    app, settings, alice_token, _ = app_with_users

    from src.main import collection_registry

    calls = []

    async def fake_sync_user(
        username, email, password, endpoint=None, on_step=None, full_sync_direction=None
    ):
        calls.append(full_sync_direction)
        if full_sync_direction is None:
            return {
                "success": False,
                "error": (
                    "AnkiWeb requires a full sync with a user-chosen direction "
                    "(the desktop client's 'force full sync' option was used on this account)."
                ),
                "error_type": "ValueError",
                "full_sync_required": True,
            }
        return {
            "success": True,
            "output": f"Full {full_sync_direction} completed with user-chosen direction.",
        }

    monkeypatch.setattr(collection_registry, "sync_user", fake_sync_user)
    # Credentials are required before the manager reaches the registry.
    app.state.anki_store.set_ankiweb_credentials("alice", "a@example.com", "ankiweb-pw")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)

            payload = await _call_tool(client, headers, "sync_collection", {})
            structured = json.loads(payload["result"]["content"][0]["text"])
            assert structured["success"] is False
            assert structured["full_sync_required"] is True
            assert "full sync" in structured["message"].lower()

            payload = await _call_tool(
                client, headers, "sync_collection", {"full_sync_direction": "upload"}
            )
            structured = json.loads(payload["result"]["content"][0]["text"])
            assert structured["success"] is True
            assert calls == [None, "upload"]


@pytest.mark.asyncio
async def test_tools_sync_from_web_before_running(app_with_users, monkeypatch):
    """Pre-tool AnkiWeb pull (SYNC_BEFORE_TOOLS): a tool call first syncs the
    account's AnkiWeb state; an immediate second tool call is throttled by the
    TTL window; an account without credentials never attempts one; and
    sync_collection skips the pre-step because it performs the sync itself."""
    app, settings, alice_token, bob_token = app_with_users

    from src.main import collection_registry

    synced = []

    async def fake_sync_user(
        username, email, password, endpoint=None, on_step=None,
        full_sync_direction=None, acquire_timeout=None,
    ):
        # acquire_timeout MUST be accepted: sync_before_tool forwards it, and
        # a TypeError here would be swallowed fail-open (zero syncs recorded).
        synced.append(username)
        return {"success": True, "output": "ok"}

    monkeypatch.setattr(collection_registry, "sync_user", fake_sync_user)
    app.state.anki_store.set_ankiweb_credentials("alice", "a@example.com", "ankiweb-pw")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)

            await _call_tool(client, headers, "list_decks", {})
            assert synced == ["alice"]  # the pull happened before the tool ran

            # Second call inside the default 30-min TTL window: throttled.
            await _call_tool(client, headers, "search_notes", {"query": "*", "limit": 1})
            assert synced == ["alice"]

            # Credential-less account: tools run, no sync attempted.
            bob_headers = await _init_session(client, bob_token, request_id=10)
            await _call_tool(client, bob_headers, "list_decks", {}, request_id=11)
            assert synced == ["alice"]

            # sync_collection does its own sync (no pre-step double-fire).
            await _call_tool(client, headers, "sync_collection", {}, request_id=12)
            assert synced == ["alice", "alice"]
