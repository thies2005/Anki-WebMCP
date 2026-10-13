"""Unit tests for configuration loading and deployment validation."""

import os
import stat

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
    # Retention default raised 5 -> 20 (snapshot engine: restores plus syncs
    # each consume a slot, so five was too tight a ceiling).
    assert settings.backup_retention == 20


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


def test_sync_before_tools_defaults_on_with_ttl(monkeypatch):
    for name in ("SYNC_BEFORE_TOOLS", "SYNC_BEFORE_TOOLS_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.sync_before_tools is True
    assert settings.sync_before_tools_ttl_seconds == 1800  # first pull per 30-min window


def test_sync_before_tools_reads_env_and_allows_zero_ttl(monkeypatch):
    monkeypatch.setenv("SYNC_BEFORE_TOOLS", "false")
    monkeypatch.setenv("SYNC_BEFORE_TOOLS_TTL_SECONDS", "0")
    settings = Settings()
    assert settings.sync_before_tools is False
    assert settings.sync_before_tools_ttl_seconds == 0  # 0 = sync before every call


def test_sync_before_tools_rejects_garbage_and_negative_ttl(monkeypatch):
    monkeypatch.setenv("SYNC_BEFORE_TOOLS_TTL_SECONDS", "soon")
    with pytest.raises(ValueError, match="SYNC_BEFORE_TOOLS_TTL_SECONDS"):
        Settings()
    monkeypatch.setenv("SYNC_BEFORE_TOOLS_TTL_SECONDS", "-1")
    with pytest.raises(ValueError, match="SYNC_BEFORE_TOOLS_TTL_SECONDS"):
        Settings()


# -- main() startup gating ---------------------------------------------------


def test_main_refuses_unsafe_config(monkeypatch, tmp_path):
    """Empty SESSION_SECRET plus an unwritable data dir must still fail closed.

    With a writable data dir the empty secret is auto-provisioned instead; the
    refusal is exercised by blocking the data dir with a regular file.
    """
    from src import main as main_module
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(
            session_secret="",
            data_dir=str(blocked),
            users_file=str(blocked / "users.json"),
        ),
    )
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1


def test_main_refuses_invalid_explicit_bootstrap(monkeypatch, tmp_path):
    """An explicitly configured but invalid admin username must fail closed."""
    from src import main as main_module
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(
            session_secret="real-secret",
            data_dir=str(tmp_path),
            users_file=str(tmp_path / "users.json"),
            admin_username="Not Valid",
            admin_password="some-pw",
        ),
    )
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1


def test_main_zero_touch_provisions_secrets_and_admin(monkeypatch, tmp_path):
    """No SESSION_SECRET, no ADMIN_* credentials, empty store: main() must
    provision everything itself and proceed to serving."""
    from src import main as main_module
    from src.users import UserStore

    users_file = str(tmp_path / "users.json")
    boot_settings = Settings(
        session_secret="",
        data_dir=str(tmp_path),
        users_file=users_file,
        admin_username="",
        admin_password="",
    )
    monkeypatch.setattr(main_module, "settings", boot_settings)
    monkeypatch.setattr(main_module, "user_store", UserStore(users_file))
    served = {}
    monkeypatch.setattr(
        main_module.uvicorn, "run", lambda app, **kwargs: served.setdefault("app", app)
    )
    main_module.main()
    assert "app" in served

    # Session secret: generated, persisted 0600, and visible to the settings
    # object the serving app captured.
    assert boot_settings.session_secret
    secret_file = tmp_path / ".session_secret"
    assert secret_file.exists()
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert secret_file.read_text().strip() == boot_settings.session_secret

    # Admin: created with the password recorded in the 0600 credentials file.
    credentials_file = tmp_path / "initial-admin-password.txt"
    assert credentials_file.exists()
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600
    content = credentials_file.read_text()
    assert "username: admin" in content
    password = next(
        line.split("password:", 1)[1].strip()
        for line in content.splitlines()
        if line.strip().startswith("password:")
    )
    assert password
    store = UserStore(users_file)
    assert "admin" in [u.username for u in store.list_users()]
    assert store.verify_password("admin", password)


def test_load_or_create_session_secret_is_stable_across_calls(tmp_path):
    from src.config import load_or_create_session_secret

    first, path = load_or_create_session_secret(str(tmp_path))
    assert first
    assert os.path.exists(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    second, _ = load_or_create_session_secret(str(tmp_path))
    assert second == first


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


@pytest.mark.asyncio
async def test_zero_touch_generated_admin_can_log_in(monkeypatch, tmp_path):
    """Zero-touch first boot: the admin created with a GENERATED password (no
    ADMIN_* env) must authenticate through the serving app, with the password
    read back from the persisted credentials file."""
    import httpx
    import pytest_asyncio  # noqa: F401  (ensures the plugin is active)

    from src import main as main_module

    users_file = str(tmp_path / "users.json")
    boot_settings = Settings(
        session_secret="",
        data_dir=str(tmp_path),
        users_file=users_file,
        admin_username="",
        admin_password="",
    )
    monkeypatch.setattr(main_module, "settings", boot_settings)
    monkeypatch.setattr(main_module, "user_store", __import__("src.users", fromlist=["UserStore"]).UserStore(users_file))

    # Provision the generated secret exactly as main() does, then bootstrap.
    from src.config import load_or_create_session_secret
    secret, _ = load_or_create_session_secret(str(tmp_path))
    object.__setattr__(boot_settings, "session_secret", secret)
    main_module._bootstrap_first_admin(boot_settings)

    content = (tmp_path / "initial-admin-password.txt").read_text()
    password = next(
        line.split("password:", 1)[1].strip()
        for line in content.splitlines()
        if line.strip().startswith("password:")
    )

    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await login_via_form(client, "admin", password)
            assert res.status_code == 303, (
                "zero-touch generated admin must be able to log in through the serving app "
                f"(got {res.status_code})"
            )
