"""Unit tests for configuration loading and deployment validation."""

import os

import pytest
from src.config import Settings, get_settings, validate_deployment_config


def test_get_settings_reads_env_at_call_time(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "late-secret")
    assert get_settings().session_secret == "late-secret"
    monkeypatch.setenv("SESSION_SECRET", "later-secret")
    assert get_settings().session_secret == "later-secret"


def test_mcp_port_must_be_integer(monkeypatch):
    monkeypatch.setenv("MCP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="MCP_PORT"):
        Settings()


# -- validation rules --------------------------------------------------------


def test_validate_deployment_config_empty_session_secret():
    settings = Settings(session_secret="")
    problems = validate_deployment_config(settings)
    assert len(problems) == 1
    assert "SESSION_SECRET" in problems[0]
    assert "openssl rand -hex 24" in problems[0]


def test_validate_deployment_config_placeholder_session_secret():
    settings = Settings(session_secret="replace-with-strong-random-session-secret")
    problems = validate_deployment_config(settings)
    assert len(problems) == 1
    assert "placeholder" in problems[0]


def test_removed_legacy_auth_fields_are_rejected():
    """The legacy single-user bridge fields are gone from Settings entirely."""
    with pytest.raises(TypeError):
        Settings(mcp_auth_token="legacy-token")
    with pytest.raises(TypeError):
        Settings(anki_connect_url="http://127.0.0.1:8765")
    with pytest.raises(TypeError):
        Settings(anki_connect_api_key="legacy-key")


def test_validate_deployment_config_valid():
    settings = Settings(session_secret="real-secret")
    assert validate_deployment_config(settings) == []


# -- new multi-user settings -------------------------------------------------


def test_users_file_derives_from_data_dir(monkeypatch):
    monkeypatch.delenv("USERS_FILE", raising=False)  # isolation from ambient env
    monkeypatch.setenv("DATA_DIR", "/tmp/anki-test-data")
    settings = Settings()
    assert settings.data_dir == "/tmp/anki-test-data"
    assert settings.users_file == "/tmp/anki-test-data/users.json"


def test_users_file_env_override_wins(monkeypatch):
    monkeypatch.setenv("USERS_FILE", "/custom/users.json")
    assert Settings().users_file == "/custom/users.json"


def test_new_tuning_settings_have_correct_defaults(monkeypatch):
    for name in ("IDLE_TIMEOUT_MINUTES", "MAX_OPEN_COLLECTIONS", "MAX_CONCURRENT_SYNCS", "BACKUP_RETENTION"):
        monkeypatch.delenv(name, raising=False)  # isolation from ambient env
    settings = Settings()
    assert settings.idle_timeout_minutes == 15
    assert settings.max_open_collections == 5
    assert settings.max_concurrent_syncs == 2
    assert settings.backup_retention == 5


def test_tuning_settings_read_env_and_enforce_minimum(monkeypatch):
    monkeypatch.setenv("IDLE_TIMEOUT_MINUTES", "30")
    assert Settings().idle_timeout_minutes == 30
    monkeypatch.setenv("MAX_OPEN_COLLECTIONS", "0")
    with pytest.raises(ValueError, match="MAX_OPEN_COLLECTIONS"):
        Settings()


# -- main() startup gating ---------------------------------------------------


def test_main_refuses_unsafe_config(monkeypatch):
    from src import main as main_module
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(session_secret=""),
    )
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1


def test_main_refuses_empty_user_store_without_bootstrap(monkeypatch, tmp_path):
    """No users + no ADMIN_USERNAME/ADMIN_PASSWORD must fail closed with SystemExit(1)."""
    from src import main as main_module
    users_file = str(tmp_path / "users.json")
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(
            session_secret="real-secret",
            users_file=users_file,
            admin_username="",
            admin_password="",
        ),
    )
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1
    # Fail-closed: the refusal path must not create the store file or its directory.
    assert not os.path.exists(users_file)
