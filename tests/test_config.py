"""Unit tests for configuration loading and deployment validation."""

import os

import pytest
from src.config import Settings, get_settings, validate_deployment_config
# POST /login requires the prelogin CSRF pair issued by GET /login; the
# shared helper lives with the Wave B regression tests.
from test_audit_fixes_b import login_via_form


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


# -- /mcp session-table knobs (audit fix) ------------------------------------


def test_mcp_session_knobs_have_deployment_sized_defaults(monkeypatch):
    for name in ("MCP_MAX_SESSIONS", "MCP_SESSION_IDLE_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.mcp_max_sessions == 16
    assert settings.mcp_session_idle_timeout == 300


def test_mcp_session_knobs_read_env(monkeypatch):
    monkeypatch.setenv("MCP_MAX_SESSIONS", "8")
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "60")
    settings = Settings()
    assert settings.mcp_max_sessions == 8
    assert settings.mcp_session_idle_timeout == 60


def test_mcp_session_knobs_reject_garbage_and_non_positive(monkeypatch):
    monkeypatch.setenv("MCP_MAX_SESSIONS", "not-a-number")
    with pytest.raises(ValueError, match="MCP_MAX_SESSIONS"):
        Settings()
    monkeypatch.setenv("MCP_MAX_SESSIONS", "8")
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "0")
    with pytest.raises(ValueError, match="MCP_SESSION_IDLE_TIMEOUT"):
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


@pytest.mark.asyncio
async def test_first_boot_bootstrap_visible_to_serving_app(monkeypatch, tmp_path):
    """Regression (found in container smoke): first boot must leave the SERVING
    app's user store holding the bootstrapped admin.

    The production path is main() bootstrap -> create_app() with NO custom
    settings, which captures the module-global user_store. An earlier version
    bootstrapped through a local UserStore, so the app authenticated against
    the import-time (empty) store and login 401'd until a restart.
    """
    import httpx
    import pytest_asyncio  # noqa: F401  (ensures the plugin is active)

    from src import main as main_module

    users_file = str(tmp_path / "users.json")
    boot_settings = Settings(
        session_secret="first-boot-secret",
        data_dir=str(tmp_path),
        users_file=users_file,
        admin_username="root",
        admin_password="first-boot-pw",
    )
    # Simulate the real import-time state: a module-global store over an empty file.
    monkeypatch.setattr(main_module, "settings", boot_settings)
    monkeypatch.setattr(main_module, "user_store", __import__("src.users", fromlist=["UserStore"]).UserStore(users_file))

    main_module._bootstrap_first_admin(boot_settings)

    # The serving app is created exactly as main() creates it: no custom settings.
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await login_via_form(client, "root", "first-boot-pw")
            assert res.status_code == 303, (
                "first-boot admin must be able to log in through the serving app "
                f"(got {res.status_code})"
            )

            # Same stale-instance class of bug, second organ: the module-level
            # sync_manager captured at import time must see bootstrapped users too.
            raw_token = main_module.user_store.peek_token("root")
            assert raw_token, "bootstrapped admin must have a stored token_plaintext"
            headers = {"Authorization": f"Bearer {raw_token}",
                       "Accept": "application/json, text/event-stream"}
            init = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "regression", "version": "0"}},
            })
            assert init.status_code == 200
            sid = init.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            await client.post("/mcp", headers=headers,
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            call = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "sync_collection", "arguments": {}},
            })
            body = call.text
            assert "Unknown user" not in body, (
                "first-boot sync manager must resolve bootstrapped users "
                f"(got: {body[:300]})"
            )
            assert "credentials" in body.lower(), (
                f"sync without creds must yield the credentials error, got: {body[:300]}"
            )
