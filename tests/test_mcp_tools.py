"""Unit tests for individual MCP tool implementations."""

import httpx
import pytest
import respx
from src.anki_client import AnkiClient, AnkiConnectError
from src.models import NoteInput
from src.sync_manager import SyncManager
from src.tools import decks as deck_tools
from src.tools import notes as note_tools
from src.tools import stats as stats_tools
from src.tools import sync as sync_tools


@pytest.fixture
def test_setup(mock_settings, ledger):
    client = AnkiClient(mock_settings)
    sync_mgr = SyncManager(client, ledger, mock_settings)
    return client, sync_mgr


@pytest.mark.asyncio
@respx.mock
async def test_list_decks(test_setup):
    client, _ = test_setup
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": {"Default": 1, "Japanese": 2}, "error": None})
    )

    data = await deck_tools.list_decks(client)
    assert data["count"] == 2
    assert {"name": "Default", "id": 1} in data["decks"]
    assert {"name": "Japanese", "id": 2} in data["decks"]


@pytest.mark.asyncio
@respx.mock
async def test_search_notes_bounded_pagination(test_setup):
    client, _ = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        if body["action"] == "findNotes":
            # Return 150 simulated IDs
            return httpx.Response(200, json={"result": list(range(1001, 1151)), "error": None})
        elif body["action"] == "notesInfo":
            notes = body["params"]["notes"]
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "noteId": nid,
                            "modelName": "Basic",
                            "tags": ["vocab"],
                            "fields": {
                                "Front": {"value": f"Question {nid}", "order": 0},
                                "Back": {"value": f"Answer {nid}", "order": 1},
                            },
                        }
                        for nid in notes
                    ],
                    "error": None,
                },
            )
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    # Request limit=200 should be clamped to 100
    res = await note_tools.search_notes(client, query="deck:Default", limit=200, offset=10)
    assert res["total_matches"] == 150
    assert res["limit"] == 100  # Clamped to 100
    assert res["offset"] == 10
    assert res["returned_count"] == 100
    assert res["notes"][0]["note_id"] == 1011
    assert res["notes"][0]["fields"]["Front"] == "Question 1011"


@pytest.mark.asyncio
@respx.mock
async def test_get_note(test_setup):
    client, _ = test_setup
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": [
                    {
                        "noteId": 42,
                        "modelName": "Basic",
                        "tags": ["tag1", "tag2"],
                        "fields": {
                            "Front": {"value": "Hello", "order": 0},
                            "Back": {"value": "World", "order": 1},
                        },
                    }
                ],
                "error": None,
            },
        )
    )

    note = await note_tools.get_note(client, 42)
    assert note["note_id"] == 42
    assert note["model_name"] == "Basic"
    assert note["tags"] == ["tag1", "tag2"]
    assert note["fields"] == {"Front": "Hello", "Back": "World"}


@pytest.mark.asyncio
@respx.mock
async def test_get_note_not_found(test_setup):
    client, _ = test_setup
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": [], "error": None})
    )

    with pytest.raises(AnkiConnectError, match="not found"):
        await note_tools.get_note(client, 99999)


@pytest.mark.asyncio
@respx.mock
async def test_get_collection_stats(test_setup):
    client, _ = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "deckNames":
            return httpx.Response(200, json={"result": ["Default"], "error": None})
        elif action == "getDeckStats":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "1": {
                            "deck_id": 1,
                            "name": "Default",
                            "new_count": 5,
                            "learn_count": 3,
                            "review_count": 12,
                            "total_in_deck": 50,
                        }
                    },
                    "error": None,
                },
            )
        elif action == "getNumCardsReviewedToday":
            return httpx.Response(200, json={"result": 25, "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    stats = await stats_tools.get_collection_stats(client)
    assert stats["cards_reviewed_today"] == 25
    assert stats["deck_count"] == 1
    assert stats["summary"]["total_new"] == 5
    assert stats["summary"]["total_learn"] == 3
    assert stats["summary"]["total_review"] == 12
    assert stats["summary"]["total_cards"] == 50
    assert stats["decks"][0]["name"] == "Default"


@pytest.mark.asyncio
@respx.mock
async def test_add_notes_duplicate_prevention(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        elif action == "canAddNotes":
            # Simulate first note is duplicate (False), second note is valid (True)
            return httpx.Response(200, json={"result": [False], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    notes = [
        NoteInput(
            deck_name="Default",
            model_name="Basic",
            fields={"Front": "Duplicate Question", "Back": "Answer"},
        )
    ]

    res = await note_tools.add_notes(sync_mgr, notes, allow_duplicates=False)
    assert res.success is False
    assert res.local_status == "failed"
    assert "Duplicate check failed" in res.error


@pytest.mark.asyncio
@respx.mock
async def test_add_notes_success(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        elif action == "canAddNotes":
            return httpx.Response(200, json={"result": [True], "error": None})
        elif action == "addNotes":
            return httpx.Response(200, json={"result": [1700000000001], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    notes = [
        NoteInput(
            deck_name="Default",
            model_name="Basic",
            fields={"Front": "Unique Q", "Back": "Ans"},
            tags=["sample"],
        )
    ]

    res = await note_tools.add_notes(sync_mgr, notes, allow_duplicates=False)
    assert res.success is True
    assert res.local_status == "committed"
    assert res.data["created_note_ids"] == [1700000000001]


@pytest.mark.asyncio
@respx.mock
async def test_update_note_fields(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        elif action == "notesInfo":
            return httpx.Response(200, json={"result": [{
                "noteId": 12345,
                "modelName": "Basic",
                "tags": [],
                "fields": {"Front": {"value": "Old", "order": 0}, "Back": {"value": "Old", "order": 1}},
            }], "error": None})
        elif action == "updateNoteFields":
            return httpx.Response(200, json={"result": None, "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.update_note_fields(sync_mgr, 12345, {"Front": "New Question"})
    assert res.success is True
    assert res.local_status == "committed"
    assert res.data["note_id"] == 12345
    assert res.data["updated_fields"] == ["Front"]


@pytest.mark.asyncio
@respx.mock
async def test_add_tags(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        elif action == "notesInfo":
            return httpx.Response(200, json={"result": [
                {"noteId": 10, "modelName": "Basic", "tags": [], "fields": {}},
                {"noteId": 20, "modelName": "Basic", "tags": [], "fields": {}},
            ], "error": None})
        elif action == "addTags":
            assert body["params"]["notes"] == [10, 20]
            assert body["params"]["tags"] == "tag1 tag2"
            return httpx.Response(200, json={"result": None, "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.add_tags(sync_mgr, [10, 20], ["tag1", "tag2"])
    assert res.success is True
    assert res.local_status == "committed"
    assert res.data["tags_added"] == ["tag1", "tag2"]


@pytest.mark.asyncio
@respx.mock
async def test_sync_collection(test_setup):
    _, sync_mgr = test_setup
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": None})
    )

    data = await sync_tools.sync_collection(sync_mgr)
    assert data["success"] is True
    assert data["sync_status"]["post_sync"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_update_note_fields_unknown_field_rejected(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        action = body["action"]
        if action == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        elif action == "notesInfo":
            return httpx.Response(200, json={"result": [{
                "noteId": 12345,
                "modelName": "Basic",
                "tags": [],
                "fields": {"Front": {"value": "Old", "order": 0}, "Back": {"value": "Old", "order": 1}},
            }], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.update_note_fields(sync_mgr, 12345, {"Frant": "typo"})
    assert res.success is False
    assert res.local_status == "failed"
    assert "unknown field" in res.error
    assert "Front" in res.error  # valid fields listed


@pytest.mark.asyncio
@respx.mock
async def test_update_note_fields_missing_note_rejected(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        if body["action"] == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        if body["action"] == "notesInfo":
            return httpx.Response(200, json={"result": [{}], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.update_note_fields(sync_mgr, 99999, {"Front": "x"})
    assert res.success is False
    assert "not found" in res.error


@pytest.mark.asyncio
@respx.mock
async def test_add_tags_missing_note_ids_rejected(test_setup):
    _, sync_mgr = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        if body["action"] == "sync":
            return httpx.Response(200, json={"result": None, "error": None})
        if body["action"] == "notesInfo":
            # note 10 vanished, note 20 exists (AnkiConnect returns {} for missing)
            return httpx.Response(200, json={"result": [
                {},
                {"noteId": 20, "modelName": "Basic", "tags": [], "fields": {}},
            ], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.add_tags(sync_mgr, [10, 20], ["tag1"])
    assert res.success is False
    assert "not found" in res.error
    assert "10" in res.error


@pytest.mark.asyncio
@respx.mock
async def test_add_notes_empty_batch_rejected(test_setup):
    _, sync_mgr = test_setup
    sync_route = respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": None})
    )

    with pytest.raises(AnkiConnectError, match="At least one note"):
        await note_tools.add_notes(sync_mgr, [])

    # No sync may have been triggered for an empty batch
    assert sync_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_search_notes_skips_phantom_entries(test_setup):
    client, _ = test_setup

    def side_effect(request):
        import json
        body = json.loads(request.content)
        if body["action"] == "findNotes":
            return httpx.Response(200, json={"result": [1001, 1002], "error": None})
        if body["action"] == "notesInfo":
            return httpx.Response(200, json={"result": [
                {"noteId": 1001, "modelName": "Basic", "tags": [], "fields": {"Front": {"value": "Q", "order": 0}}},
                {},  # vanished between findNotes and notesInfo
            ], "error": None})
        return httpx.Response(400)

    respx.post("http://127.0.0.1:8765/").mock(side_effect=side_effect)

    res = await note_tools.search_notes(client, query="deck:Default")
    assert res["returned_count"] == 1
    assert res["notes"][0]["note_id"] == 1001
