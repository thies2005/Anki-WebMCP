"""Unit tests for MCP server authentication middleware."""

import httpx
import pytest
from src.config import Settings
from src.main import create_app


@pytest.mark.asyncio
async def test_health_endpoint_exempt_from_auth(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["service"] == "anki-mcp"
        assert data["auth_enabled"] is True


@pytest.mark.asyncio
async def test_mcp_unauthenticated_request_rejected(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post("/mcp", json={})
        assert res.status_code == 401
        assert "Missing authentication token" in res.json().get("error", "")


@pytest.mark.asyncio
async def test_mcp_invalid_token_rejected(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )
        assert res.status_code == 401
        assert "Invalid authentication token" in res.json().get("error", "")


@pytest.mark.asyncio
async def test_mcp_bearer_token_accepted(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            res = await client.post(
                "/mcp",
                headers={"Authorization": "Bearer secure-token"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            # Middleware allowed the request through, so it's not 401
            assert res.status_code != 401


@pytest.mark.asyncio
async def test_mcp_api_key_header_accepted(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            res = await client.post(
                "/mcp",
                headers={"X-API-Key": "secure-token"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert res.status_code != 401


@pytest.mark.asyncio
async def test_mcp_query_param_token_accepted(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            res = await client.post(
                "/mcp?token=secure-token",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert res.status_code != 401


@pytest.mark.asyncio
async def test_auth_disabled_when_token_empty(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            res = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert res.status_code != 401
