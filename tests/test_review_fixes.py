"""Regression tests for the review-gate fixes on fix/audit-run-1.

Covers: chunked-transfer body-cap refusal (F1), the store-level last-admin
guard surfacing as a 400 with archive disclosure in admin_delete_user (F2),
the pre-auth scrypt capacity limiter (F4), and the limit_concurrency /
MCP_MAX_SESSIONS coupling (F5).
"""

import asyncio

import httpx
import pytest

from src import main as src_main
from src.config import Settings
from src.users import UserStore


@pytest.fixture
def review_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    users_file = data_dir / "users.json"
    store = UserStore(str(users_file))
    store.create_user("root", "root-password-123", role="admin")
    store.create_user("alice", "alice-password-123", role="admin")
    settings = Settings(
        session_secret="review-fixes-secret",
        data_dir=str(data_dir),
        users_file=str(users_file),
    )
    app = src_main.create_app(custom_settings=settings)
    return app, settings, store, data_dir


async def _login(app, username, password):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get("/login")
        token = r1.headers.get("set-cookie", "")
        # extract prelogin cookie + field
        cookie_val = token.split("anki_prelogin=")[-1].split(";")[0]
        import re
        m = re.search(r'name="prelogin_token" value="([^"]+)"', r1.text)
        assert m, "prelogin field missing"
        return await client.post(
            "/login",
            data={"username": username, "password": password, "prelogin_token": m.group(1)},
            cookies={"anki_prelogin": cookie_val},
        )


@pytest.mark.asyncio
async def test_chunked_login_body_refused_before_parse(review_env):
    """F1: transfer-encoding bodies are refused outright (413) — starlette's
    per-field caps do not bound multipart file parts, so admitting chunked
    would reopen the pre-auth spool path."""
    app, settings, store, data_dir = review_env
    transport = httpx.ASGITransport(app=app)

    async def chunked_body():
        yield b"username=ro"
        yield b"ot&password=root-password-123"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # httpx emits transfer-encoding: chunked when given a generator
        r = await client.post("/login", content=chunked_body())
    assert r.status_code == 413
    # the refusal precedes parsing: no scrypt burn, no limiter key consumed
    # (the body was never read, so no form fields exist to key a failure on)


@pytest.mark.asyncio
async def test_delete_guard_race_after_archive_returns_400_with_disclosure(review_env, monkeypatch):
    """F2: when the in-lock last-admin guard fires AFTER archiving (raced by a
    concurrent demotion), the handler returns a 400 that discloses the archive
    divergence instead of a bare 500."""
    app, settings, store, data_dir = review_env
    app_store = app.state.anki_store  # the instance the handler closed over
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await _login(app, "root", "root-password-123")
        cookie = r.headers["set-cookie"].split("anki_session=")[-1].split(";")[0]
        client.cookies.set("anki_session", cookie)
        # Open root's collection so the archive step has real data to move.
        await client.get("/")
        page = await client.get("/admin")
        import re
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

        # Simulate the race: the handler's pre-check sees two admins and
        # passes, the archive runs, and THEN the in-lock store guard fires —
        # exactly as a concurrent demotion of alice would cause.
        original_delete = app_store.delete_user

        def racing_delete(username):
            raise ValueError(
                f"Cannot delete {username!r}: it is the last admin account."
            )

        monkeypatch.setattr(app_store, "delete_user", racing_delete)
        r = await client.post(
            "/admin/delete-user",
            data={"username": "root", "confirm_username": "root", "csrf_token": csrf},
        )
    assert r.status_code == 400
    assert "already archived" in r.text
    # archive divergence is real: the data dir moved to deleted/
    deleted = list((data_dir / "deleted").glob("root-*"))
    assert deleted, "expected the archive to have happened before the guard fired"
    monkeypatch.setattr(app_store, "delete_user", original_delete)


@pytest.mark.asyncio
async def test_login_verify_capacity_bounds_concurrent_scrypt(review_env, monkeypatch):
    """F4: at most login_verify_capacity.available_tokens scrypt verifications
    run concurrently, however many logins are in flight."""
    app, settings, store, data_dir = review_env
    app_store = app.state.anki_store  # the instance the handler closed over
    transport = httpx.ASGITransport(app=app)

    state = {"running": 0, "peak": 0}

    original_verify = app_store.verify_password

    def slow_verify(username, password):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        import time as _t
        _t.sleep(0.05)
        state["running"] -= 1
        return original_verify(username, password)

    monkeypatch.setattr(app_store, "verify_password", slow_verify)

    async def one_login(i):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.get("/login")
            import re
            m = re.search(r'name="prelogin_token" value="([^"]+)"', r1.text)
            cookie_val = r1.headers.get("set-cookie", "").split("anki_prelogin=")[-1].split(";")[0]
            return await client.post(
                "/login",
                data={
                    "username": f"user{i}",
                    "password": "wrong",
                    "prelogin_token": m.group(1),
                },
                cookies={"anki_prelogin": cookie_val},
            )

    # 10 concurrent wrong-password logins with distinct usernames (no
    # per-username lockout interference); the peak concurrency of scrypt work
    # must not exceed the dedicated capacity limiter.
    cap = src_main.login_verify_capacity.available_tokens
    await asyncio.gather(*(one_login(i) for i in range(10)))
    assert state["peak"] <= cap
    assert state["peak"] >= 1  # the limiter actually admitted work


def test_uvicorn_concurrency_scales_with_mcp_sessions(tmp_path):
    """F5: limit_concurrency stays at or above twice the MCP session cap."""
    s16 = Settings(session_secret="x", data_dir=str(tmp_path))
    kw16 = src_main._uvicorn_run_kwargs(s16)
    assert kw16["limit_concurrency"] >= 2 * s16.mcp_max_sessions
    assert kw16["limit_concurrency"] >= src_main.UVICORN_LIMIT_CONCURRENCY

    s64 = Settings(
        session_secret="x",
        data_dir=str(tmp_path),
        mcp_max_sessions=64,
    )
    kw64 = src_main._uvicorn_run_kwargs(s64)
    assert kw64["limit_concurrency"] >= 2 * 64
