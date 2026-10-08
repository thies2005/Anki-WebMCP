"""End-to-end MCP protocol tests over the Streamable HTTP transport (in-process ASGI)."""

import json
import httpx
import pytest
import respx
from src.config import Settings
from src.main import create_app

AUTH_HEADERS = {
    "Authorization": "Bearer secure-token",
    "Accept": "application/json, text/event-stream",
}


def _parse_mcp_response(res: httpx.Response) -> dict:
    if res.headers.get("content-type", "").startswith("text/event-stream"):
        for line in res.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"No SSE data line in response: {res.text!r}")
    return res.json()


@pytest.mark.asyncio
@respx.mock
async def test_tools_call_returns_structured_error_when_anki_down(temp_ledger_file):
    respx.post("http://127.0.0.1:8765/").mock(side_effect=httpx.ConnectError("connection refused"))

    settings = Settings(
        anki_connect_url="http://127.0.0.1:8765",
        mcp_auth_token="secure-token",
        idempotency_ledger_path=temp_ledger_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            init_res = await client.post("/mcp", headers=AUTH_HEADERS, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            })
            assert init_res.status_code == 200
            headers = dict(AUTH_HEADERS)
            session_id = init_res.headers.get("mcp-session-id")
            if session_id:
                headers["mcp-session-id"] = session_id
            await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

            call_res = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_decks", "arguments": {}},
            })
            assert call_res.status_code == 200
            payload = _parse_mcp_response(call_res)
            assert "error" not in payload
            text = payload["result"]["content"][0]["text"]
            # The upstream failure message must reach the client as structured
            # data, not the SDK's generic "Error executing tool" mask.
            assert "Error executing tool" not in text
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
            if structured is not None:
                assert structured.get("success") is False
                assert "Could not connect to Anki Desktop" in structured.get("error", "")
            else:
                assert "Could not connect to Anki Desktop" in text
