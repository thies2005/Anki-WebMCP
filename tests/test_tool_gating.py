"""Two-layer tool disablement (global ∪ per-user) + tool-usage logging.

Unit tests for the rule grammar and stores, then protocol-level proofs over
the full MCP-over-ASGI path: a disabled tool is refused with a structured
error naming rule and layer, enabled tools are unaffected, every call lands
in the per-user usage log, and the @_tool_guard wrapper leaves tools/list
schemas untouched.
"""

import json
import os

import httpx
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.main import TOOL_REFERENCE_GROUPS, create_app
from src.models import NoteInput
from src.toollog import ToolUsageLogger, sanitize_error
from src.toolpolicy import (
    ToolPolicyStore,
    matching_rule,
    parse_pattern_text,
    rule_matches,
    validate_patterns,
)
from src.users import UserStore

SECRET = "test-session-secret"


# ---------------------------------------------------------------------------
# Rule grammar
# ---------------------------------------------------------------------------


def test_rule_matches_exact_and_prefix_only():
    assert rule_matches("delete_notes", "delete_notes")
    assert rule_matches("delete_*", "delete_deck")
    assert rule_matches("delete_*", "delete_notes")
    assert rule_matches("delete_*", "delete_note_type")
    # No substring matching: a bare word must not contain-match.
    assert not rule_matches("delete", "delete_notes")
    assert not rule_matches("note", "add_notes")
    assert not rule_matches("delete_*", "deletelete")
    # Prefix runs to the wildcard: "movecards" does not start with "move_".
    assert not rule_matches("move_*", "movecards")


def test_matching_rule_returns_first_match():
    rules = ["sync_*", "sync_collection"]
    assert matching_rule("sync_collection", rules) == "sync_*"
    assert matching_rule("list_decks", rules) is None


def test_validate_patterns_normalizes_and_dedupes():
    assert validate_patterns([" delete_* ", "", "delete_*", "sync_collection", ""]) == [
        "delete_*",
        "sync_collection",
    ]


def test_validate_patterns_rejects_malformed():
    for bad in ["deck.list", "with space", "infix*star", "UPPER", "*", "a" * 200]:
        with pytest.raises(ValueError):
            validate_patterns([bad])


def test_validate_patterns_caps_rule_count():
    with pytest.raises(ValueError):
        validate_patterns([f"tool_{i}" for i in range(101)])


def test_parse_pattern_text_splits_lines():
    assert parse_pattern_text("delete_*\n\nsync_collection\n") == [
        "delete_*",
        "sync_collection",
    ]


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def test_tool_policy_store_roundtrip_and_persistence(tmp_path):
    path = str(tmp_path / "tool_policy.json")
    store = ToolPolicyStore(path)
    assert store.get_patterns() == []
    store.set_patterns(["delete_*", "sync_collection"])
    assert store.get_patterns() == ["delete_*", "sync_collection"]
    # A fresh instance reloads from disk.
    reloaded = ToolPolicyStore(path)
    assert reloaded.get_patterns() == ["delete_*", "sync_collection"]
    with open(path) as f:
        os.chmod(path, 0o600)  # atomic saves must be owner-only
        assert json.load(f) == {"disabled_tool_patterns": ["delete_*", "sync_collection"]}


def test_tool_policy_store_rejects_invalid_and_tolerates_corruption(tmp_path):
    path = str(tmp_path / "tool_policy.json")
    store = ToolPolicyStore(path)
    with pytest.raises(ValueError):
        store.set_patterns(["not a rule"])
    with open(path, "w") as f:
        f.write("{ not json")
    tolerant = ToolPolicyStore(path)  # corrupt file: log + start empty
    assert tolerant.get_patterns() == []


@pytest.mark.asyncio
async def test_tool_usage_logger_records_and_orders_newest_first(tmp_path):
    log = ToolUsageLogger(str(tmp_path), max_entries=3)
    for i in range(5):
        await log.record("alice", f"tool_{i}", ok=(i != 4), duration_ms=i, error=None)
    entries = await log.recent("alice", 10)
    # Cap 3 evicts the oldest; newest first.
    assert [e["tool"] for e in entries] == ["tool_4", "tool_3", "tool_2"]
    assert entries[0]["ok"] is False
    # Persistence across instances.
    reloaded = ToolUsageLogger(str(tmp_path), max_entries=3)
    assert [e["tool"] for e in await reloaded.recent("alice", 10)] == [
        "tool_4",
        "tool_3",
        "tool_2",
    ]


def test_sanitize_error_strips_control_chars_and_truncates():
    hostile = "boom\ninjected\tline\x00\x07" + "x" * 500
    cleaned = sanitize_error(hostile)
    assert "\n" not in cleaned and "\x00" not in cleaned and "\t" not in cleaned
    assert len(cleaned) == 300


def test_user_store_tool_rule_mutator(tmp_path):
    store = UserStore(str(tmp_path / "users.json"))
    store.create_user("alice", "pw")
    saved = store.set_disabled_tool_patterns("alice", [" delete_* ", "delete_*", "sync_collection"])
    assert saved == ["delete_*", "sync_collection"]
    # Persisted: a fresh store reloads it.
    assert UserStore(str(tmp_path / "users.json")).get("alice").disabled_tool_patterns == [
        "delete_*",
        "sync_collection",
    ]
    with pytest.raises(ValueError):
        store.set_disabled_tool_patterns("alice", ["bad rule"])
    # Invalid update must not clobber the previously saved rules.
    assert store.get("alice").disabled_tool_patterns == ["delete_*", "sync_collection"]


def test_user_records_without_tool_rules_load_as_empty(tmp_path):
    """Legacy records written before the field existed get no rules."""
    path = str(tmp_path / "users.json")
    store = UserStore(path)
    store.create_user("legacy", "pw")
    # Rewrite the record without the new field, as an old file would look.
    with open(path) as f:
        data = json.load(f)
    del data["legacy"]["disabled_tool_patterns"]
    with open(path, "w") as f:
        json.dump(data, f)
    assert UserStore(path).get("legacy").disabled_tool_patterns == []


# ---------------------------------------------------------------------------
# NoteInput: note_type canonical, model_name legacy alias
# ---------------------------------------------------------------------------


def test_note_input_accepts_canonical_note_type():
    n = NoteInput(deck_name="Default", note_type="Basic", fields={"Front": "q"})
    assert n.note_type == "Basic"


def test_note_input_accepts_model_name_alias():
    n = NoteInput(deck_name="Default", model_name="Basic", fields={"Front": "q"})
    assert n.note_type == "Basic"


def test_note_input_alias_and_canonical_must_agree():
    with pytest.raises(ValidationError):
        NoteInput(
            deck_name="Default", note_type="Basic", model_name="Cloze", fields={"Front": "q"}
        )
    # Same value through both spellings is fine.
    n = NoteInput(deck_name="Default", note_type="Basic", model_name="Basic", fields={"Front": "q"})
    assert n.note_type == "Basic"


def test_note_input_missing_note_type_names_the_canonical_field():
    with pytest.raises(ValidationError) as exc:
        NoteInput(deck_name="Default", fields={"Front": "q"})
    assert "note_type" in str(exc.value)


# ---------------------------------------------------------------------------
# Protocol level: gate + log over the full MCP path
# ---------------------------------------------------------------------------


def _make_settings(tmp_path) -> Settings:
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(tmp_path / "users.json"),
    )


@pytest.fixture
def app_with_users(tmp_path):
    settings = _make_settings(tmp_path)
    store = UserStore(settings.users_file)
    _, alice_token = store.create_user("alice", "alice-pw")
    app = create_app(settings)
    return app, settings, alice_token, store


def _parse_mcp_response(res: httpx.Response) -> dict:
    if res.headers.get("content-type", "").startswith("text/event-stream"):
        for line in res.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"No SSE data line in response: {res.text!r}")
    return res.json()


async def _init_session(client: httpx.AsyncClient, token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
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
    session_id = init_res.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


async def _call_tool(client, headers, name, arguments, request_id=2):
    res = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert res.status_code == 200
    return _parse_mcp_response(res)


def _structured(payload):
    return json.loads(payload["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_user_rule_refuses_tool_with_structured_error(app_with_users):
    app, _, alice_token, _ = app_with_users
    # Rule changes must hit the app's OWN store (create_app builds a fresh
    # UserStore from disk; a pre-construction instance would not be consulted).
    app.state.anki_store.set_disabled_tool_patterns("alice", ["sync_collection"])

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            payload = await _call_tool(client, headers, "sync_collection", {})
            structured = _structured(payload)
            assert structured["success"] is False
            assert structured["error_type"] == "ToolDisabledError"
            assert "matched your rule 'sync_collection'" in structured["error"]
            # Enabled tools are unaffected by the rule (read envelopes carry
            # no success key — decks present means it ran).
            ok = _structured(await _call_tool(client, headers, "list_decks", {}))
            assert ok.get("success") is not False
            assert "decks" in ok


@pytest.mark.asyncio
async def test_global_rule_outranks_and_masks_user_duplicate(app_with_users):
    app, _, alice_token, _ = app_with_users
    app.state.anki_tool_policy.set_patterns(["delete_*"])
    app.state.anki_store.set_disabled_tool_patterns("alice", ["delete_deck"])  # user duplicate

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            structured = _structured(await _call_tool(client, headers, "delete_notes", {"note_ids": [1]}))
            assert structured["error_type"] == "ToolDisabledError"
            # The global layer is named, not the user-side duplicate.
            assert "matched global rule 'delete_*'" in structured["error"]
            # Prefix semantics: delete_deck would match too; list_decks must not.
            assert "delete" not in "list_decks"


@pytest.mark.asyncio
async def test_global_sync_rule_suppresses_pretool_sync(app_with_users, monkeypatch):
    """A GLOBAL rule disabling sync_collection is also the opt-out from the
    automatic pre-tool sync: other tools run WITHOUT it (while still
    succeeding) — the rule is the admin's per-tool control over automatic
    AnkiWeb egress."""
    app, _, alice_token, _ = app_with_users
    app.state.anki_tool_policy.set_patterns(["sync_collection"])
    # Saved credentials: without the rule the pre-tool sync WOULD run, so an
    # empty recording discriminates suppression from a credential-less no-op.
    app.state.anki_store.set_ankiweb_credentials("alice", "a@example.com", "ankiweb-pw")
    called = []

    async def _recorder(username):
        called.append(username)

    monkeypatch.setattr(app.state.anki_sync_manager, "sync_before_tool", _recorder)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            ok = _structured(await _call_tool(client, headers, "list_decks", {}))
            assert ok.get("success") is not False  # the tool itself still works
            assert "decks" in ok
            assert called == []  # no pre-tool sync despite saved credentials


@pytest.mark.asyncio
async def test_user_sync_rule_suppresses_pretool_sync_for_that_user_only(
    app_with_users, monkeypatch
):
    """A user-side sync_collection rule suppresses that user's automatic
    pre-tool sync only — another user without the rule still gets it."""
    app, _, alice_token, _ = app_with_users
    store = app.state.anki_store
    store.set_disabled_tool_patterns("alice", ["sync_collection"])
    store.set_ankiweb_credentials("alice", "a@example.com", "ankiweb-pw")
    _, bob_token = store.create_user("bob", "bob-pw")
    store.set_ankiweb_credentials("bob", "b@example.com", "ankiweb-pw")
    called = []

    async def _recorder(username):
        called.append(username)

    monkeypatch.setattr(app.state.anki_sync_manager, "sync_before_tool", _recorder)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            alice_headers = await _init_session(client, alice_token)
            bob_headers = await _init_session(client, bob_token)
            await _call_tool(client, alice_headers, "list_decks", {})
            await _call_tool(client, bob_headers, "list_decks", {})
            # Alice's rule suppressed her pre-tool sync; Bob's ran.
            assert called == ["bob"]


@pytest.mark.asyncio
async def test_usage_log_records_calls_and_refusals(app_with_users):
    app, _, alice_token, _ = app_with_users
    app.state.anki_store.set_disabled_tool_patterns("alice", ["sync_collection"])

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            await _call_tool(client, headers, "list_decks", {})
            await _call_tool(client, headers, "sync_collection", {})

    entries = await app.state.anki_tool_usage.recent("alice", 10)
    by_tool = {e["tool"]: e for e in entries}
    assert by_tool["list_decks"]["ok"] is True
    assert by_tool["list_decks"]["duration_ms"] >= 0
    assert by_tool["sync_collection"]["ok"] is False
    assert "refused" in by_tool["sync_collection"]["error"]


@pytest.mark.asyncio
async def test_tools_list_unchanged_names_and_review_deck_params(app_with_users):
    """Schema parity: the @_tool_guard wrapper must not alter tools/list —
    same 47 names as the portal reference table, and the review tools expose
    their new deck argument (and still no ctx leak)."""
    app, _, alice_token, _ = app_with_users

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = await _init_session(client, alice_token)
            res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 9, "method": "tools/list",
            })
            payload = _parse_mcp_response(res)
    tools = {t["name"]: t for t in payload["result"]["tools"]}
    reference = [name for _, names in TOOL_REFERENCE_GROUPS for name in names]
    assert len(reference) == len(set(reference)) == 47
    assert set(tools) == set(reference)
    assert set(tools["get_review_queue"]["inputSchema"]["properties"]) == {"limit", "deck"}
    assert set(tools["answer_card"]["inputSchema"]["properties"]) == {
        "card_id", "rating", "deck", "idempotency_key",
    }
