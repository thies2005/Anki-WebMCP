"""Unit tests for MCP server authentication middleware."""

import httpx
import pytest
import respx
from src.config import Settings
from src.main import create_app


@pytest.mark.asyncio
@respx.mock
async def test_health_endpoint_exempt_from_auth(temp_ledger_file):
    settings = Settings(
        anki_connect_url="http://127.0.0.1:8765",
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    respx.post("http://127.0.0.1:8765/").mock(
        return_value=httpx.Response(200, json={"result": 6, "error": None})
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["service"] == "anki-mcp"
        assert data["anki_connected"] is True
        assert data["auth_enabled"] is True


@pytest.mark.asyncio
@respx.mock
async def test_health_degraded_returns_503(temp_ledger_file):
    settings = Settings(
        anki_connect_url="http://127.0.0.1:8765",
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    respx.post("http://127.0.0.1:8765/").mock(side_effect=httpx.ConnectError("refused"))
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        res = await client.get("/health")
        assert res.status_code == 503
        assert res.json()["status"] == "degraded"
        assert res.json()["anki_connected"] is False


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
async def test_mcp_query_param_token_rejected(temp_ledger_file):
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
            assert res.status_code == 401


@pytest.mark.asyncio
async def test_create_app_rejects_empty_token(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)
    # Starlette builds the middleware stack lazily; construction of
    # AuthMiddleware (which must raise) happens on stack build.
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        app.build_middleware_stack()


@pytest.mark.asyncio
async def test_mcp_non_ascii_token_rejected_without_500(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        # Send the token as raw UTF-8 bytes, as it would arrive on the wire;
        # httpx rejects non-ASCII str header values before sending.
        res = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer tökén".encode("utf-8")},
            json={},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_mcp_malformed_authorization_header_rejected(temp_ledger_file):
    settings = Settings(
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        for header in ("Bearer", "Token secure-token", "Basic dXNlcjpwYXNz"):
            res = await client.post("/mcp", headers={"Authorization": header}, json={})
            assert res.status_code == 401
