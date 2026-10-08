"""Unit tests for configuration loading and deployment validation."""

import pytest
from src.config import Settings, get_settings, validate_deployment_config


def test_get_settings_reads_env_at_call_time(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "late-token")
    assert get_settings().mcp_auth_token == "late-token"
    monkeypatch.setenv("MCP_AUTH_TOKEN", "later-token")
    assert get_settings().mcp_auth_token == "later-token"


def test_mcp_port_must_be_integer(monkeypatch):
    monkeypatch.setenv("MCP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="MCP_PORT"):
        Settings()


def test_validate_deployment_config_empty_token():
    settings = Settings(mcp_auth_token="", anki_connect_api_key="real-key")
    problems = validate_deployment_config(settings)
    assert len(problems) == 1
    assert "MCP_AUTH_TOKEN" in problems[0]


def test_validate_deployment_config_placeholder_token():
    settings = Settings(
        mcp_auth_token="replace-with-strong-random-mcp-bearer-token",
        anki_connect_api_key="real-key",
    )
    problems = validate_deployment_config(settings)
    assert len(problems) == 1
    assert "placeholder" in problems[0]


def test_validate_deployment_config_empty_api_key():
    settings = Settings(mcp_auth_token="real-token", anki_connect_api_key="")
    problems = validate_deployment_config(settings)
    assert len(problems) == 1
    assert "ANKI_CONNECT_API_KEY" in problems[0]


def test_validate_deployment_config_valid():
    settings = Settings(mcp_auth_token="real-token", anki_connect_api_key="real-key")
    assert validate_deployment_config(settings) == []


def test_main_refuses_unsafe_config(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANKI_CONNECT_API_KEY", raising=False)
    from src.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
