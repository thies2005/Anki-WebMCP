"""Unit tests for SyncManager write-safety and synchronization workflows."""

import httpx
import pytest
import respx
from src.anki_client import AnkiClient
from src.sync_manager import SyncManager


@pytest.fixture
def sync_mgr(mock_settings, ledger):
    client = AnkiClient(mock_settings)
    return SyncManager(client, ledger, mock_settings)


@pytest.mark.asyncio
@respx.mock
async def test_mutation_success_with_full_sync(sync_mgr):
    # Pre-sync and post-sync both succeed
    sync_route = respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": None})
    )

    called = False

    async def dummy_mutation():
        nonlocal called
        called = True
        return {"note_id": 100}

    res = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=dummy_mutation,
        idempotency_key="idemp-1",
    )

    assert res.success is True
    assert res.local_status == "committed"
    assert res.sync_status.pre_sync == "ok"
    assert res.sync_status.post_sync == "ok"
    assert res.data == {"note_id": 100}
    assert called is True
    # Pre-sync and post-sync called (2 calls)
    assert sync_route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_mutation_auth_not_configured(sync_mgr):
    # Pre-sync returns auth not configured
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": "sync: auth not configured"})
    )

    called = False

    async def dummy_mutation():
        nonlocal called
        called = True
        return {"note_id": 101}

    res = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=dummy_mutation,
    )

    # Local write still commits safely
    assert res.success is True
    assert res.local_status == "committed"
    assert res.sync_status.pre_sync == "auth_not_configured"
    assert res.sync_status.post_sync == "skipped"
    assert res.data == {"note_id": 101}
    assert called is True
    assert any("AnkiWeb account not configured" in w for w in res.warnings)


@pytest.mark.asyncio
@respx.mock
async def test_mutation_post_sync_fails_preserves_local_commit(sync_mgr):
    # Pre-sync succeeds, post-sync fails
    call_count = 0

    def sync_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"result": None, "error": None})
        else:
            return httpx.Response(200, json={"result": None, "error": "AnkiWeb sync timeout"})

    respx.post("http://127.0.0.1:8765/").mock(side_effect=sync_side_effect)

    async def dummy_mutation():
        return {"note_id": 102}

    res = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=dummy_mutation,
    )

    # Local commit is successful, but post_sync is marked failed with clear warnings
    assert res.success is True
    assert res.local_status == "committed"
    assert res.sync_status.pre_sync == "ok"
    assert res.sync_status.post_sync == "failed"
    assert res.sync_status.post_sync_error == "AnkiWeb sync timeout"
    assert res.data == {"note_id": 102}
    assert any("post-mutation synchronization to AnkiWeb failed" in w for w in res.warnings)


@pytest.mark.asyncio
@respx.mock
async def test_mutation_local_write_failure(sync_mgr):
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": None})
    )

    async def failing_mutation():
        raise ValueError("Invalid field Front")

    res = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=failing_mutation,
        idempotency_key="idemp-fail",
    )

    assert res.success is False
    assert res.local_status == "failed"
    assert res.sync_status.pre_sync == "ok"
    assert res.sync_status.post_sync == "skipped"
    assert "Invalid field Front" in res.error


@pytest.mark.asyncio
async def test_maintenance_mode_rejects_mutation(sync_mgr):
    sync_mgr.set_maintenance_mode(True)

    called = False

    async def dummy_mutation():
        nonlocal called
        called = True
        return {}

    res = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=dummy_mutation,
    )

    assert res.success is False
    assert res.local_status == "skipped"
    assert "maintenance mode" in res.error
    assert called is False


@pytest.mark.asyncio
@respx.mock
async def test_idempotency_replay_prevents_duplicate_mutation(sync_mgr):
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": None, "error": None})
    )

    counter = 0

    async def counting_mutation():
        nonlocal counter
        counter += 1
        return {"note_id": 500}

    # First call: executes mutation
    res1 = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=counting_mutation,
        idempotency_key="unique-req-1",
    )
    assert res1.success is True
    assert counter == 1

    # Second call with same idempotency key: returns cached result without invoking mutation fn
    res2 = await sync_mgr.execute_mutation(
        operation_name="create_note",
        mutation_fn=counting_mutation,
        idempotency_key="unique-req-1",
    )
    assert res2.success is True
    assert counter == 1  # Still 1!
    assert res2.data == {"note_id": 500}
    assert any("Idempotent operation replay" in w for w in res2.warnings)
