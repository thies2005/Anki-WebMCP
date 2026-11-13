"""Regression tests covering security fixes, MCP additions, and portal/admin UX improvements."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from src.config import Settings, get_encryption_secret
from src.main import _tool_error_response, create_app
from src.models import NoteInput
from src.oauth import OAuthStore
from src.sync_manager import SyncManager
from src.toollog import ToolUsageLogger
from src.tools import cards as card_tools
from src.tools import media as media_tools
from src.tools import notes as note_tools
from src.tools import sync as sync_tools
from src.users import UserStore, decrypt_secret_field, encrypt_secret_field
from src.webauth import SESSION_COOKIE_NAME, issue_session

SECRET = "test-improvements-secret"


def _csrf_from(html_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', html_text)
    assert match, "expected hidden csrf_token field in HTML"
    return match.group(1)


def _parse_mcp(res: httpx.Response) -> dict:
    if res.headers.get("content-type", "").startswith("text/event-stream"):
        for line in res.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError(f"Missing SSE data line in {res.text!r}")
    return res.json()


async def _init_mcp(client: httpx.AsyncClient, token: str, req_id: int = 1) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    init = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert init.status_code == 200
    sid = init.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
    notif = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif.status_code in (200, 202, 204)
    return headers


def _pkce_pair(verifier: str = "v" * 48) -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# 1. Encryption at rest & credentials clear
# ---------------------------------------------------------------------------


def test_user_store_encrypts_secrets_at_rest_and_migrates_legacy_plaintext(tmp_path):
    users_path = str(tmp_path / "users.json")

    # Separate DATA_ENCRYPTION_KEY takes precedence over SESSION_SECRET.
    settings = Settings(session_secret="session-only", data_encryption_key="enc-key-only")
    assert get_encryption_secret(settings) == "enc-key-only"

    store = UserStore(users_path, encryption_secret="enc-secret-1")
    _user, raw_token = store.create_user("alice", "alice-pw-strong")
    store.set_ankiweb_credentials("alice", "alice@example.com", "super-secret-ankiweb-pw")

    raw_disk = json.loads(Path(users_path).read_text(encoding="utf-8"))["alice"]
    assert raw_disk["token_plaintext"].startswith("enc:v1:")
    assert raw_disk["ankiweb_password"].startswith("enc:v1:")
    assert raw_token not in raw_disk["token_plaintext"]
    assert "super-secret-ankiweb-pw" not in raw_disk["ankiweb_password"]

    reloaded = UserStore(users_path, encryption_secret="enc-secret-1")
    assert reloaded.peek_token("alice") == raw_token
    assert reloaded.get("alice").ankiweb_password == "super-secret-ankiweb-pw"

    # Tampered ciphertext fails closed (flip a real byte inside the MAC segment).
    tampered = json.loads(Path(users_path).read_text(encoding="utf-8"))
    val = tampered["alice"]["ankiweb_password"]
    tampered["alice"]["ankiweb_password"] = val[:-4] + ("A" if val[-4] != "A" else "B") + val[-3:]
    Path(users_path).write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="failed integrity verification"):
        UserStore(users_path, encryption_secret="enc-secret-1")

    # Legacy plaintext JSON loads cleanly and re-encrypts on next save.
    legacy_path = str(tmp_path / "legacy.json")
    Path(legacy_path).write_text(
        json.dumps(
            {
                "alice": {
                    "username": "alice",
                    "password_hash": reloaded.get("alice").password_hash,
                    "token_hash": reloaded.get("alice").token_hash,
                    "role": "user",
                    "token_plaintext": raw_token,
                    "ankiweb_email": "alice@example.com",
                    "ankiweb_password": "legacy-plain-password",
                }
            }
        ),
        encoding="utf-8",
    )
    migrated = UserStore(legacy_path, encryption_secret="enc-secret-2")
    assert migrated.get("alice").ankiweb_password == "legacy-plain-password"
    migrated.clear_ankiweb_credentials("alice")
    assert migrated.get("alice").ankiweb_email == ""
    assert migrated.get("alice").ankiweb_password == ""
    migrated_disk = json.loads(Path(legacy_path).read_text(encoding="utf-8"))["alice"]
    assert migrated_disk["token_plaintext"].startswith("enc:v1:")


def test_encrypt_decrypt_helper_rejects_missing_secret():
    ct = encrypt_secret_field("secret-a", "hello")
    assert ct.startswith("enc:v1:")
    assert decrypt_secret_field("secret-a", ct) == "hello"
    with pytest.raises(ValueError, match="encryption secret"):
        decrypt_secret_field("", ct)


# ---------------------------------------------------------------------------
# 2. OAuth hardening, headers, error redaction, rate limits & portal revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_hardening_loopback_pkce_nostore_and_portal_revoke(monkeypatch, tmp_path):
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file, encryption_secret=SECRET)
    store.create_user("alice", "alice-password-1")

    monkeypatch.setenv("FORCE_SECURE_COOKIES", "true")
    settings = Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=users_file,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            follow_redirects=False,
        ) as client:
            # IPv6 loopback redirect URI is accepted.
            reg = await client.post(
                "/register",
                json={
                    "client_name": "IPv6 CLI",
                    "redirect_uris": ["http://[::1]:9876/callback"],
                },
            )
            assert reg.status_code == 201
            cid = reg.json()["client_id"]

            verifier, challenge = _pkce_pair()
            # Short PKCE challenge rejected at authorize.
            client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "alice"))
            bad_pkce = await client.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": "http://[::1]:9876/callback",
                    "response_type": "code",
                    "state": "s1",
                    "code_challenge": "too-short",
                    "code_challenge_method": "S256",
                },
            )
            assert bad_pkce.status_code == 400

            auth_get = await client.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": "http://[::1]:9876/callback",
                    "response_type": "code",
                    "state": "s1",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            assert auth_get.status_code == 200
            assert auth_get.headers.get("cache-control") == "no-store"
            assert "alice" in auth_get.text
            assert "http://[::1]:9876/callback" in auth_get.text
            # Verify strict CSP, Permissions-Policy, and HSTS (force_secure_cookies=True).
            csp = auth_get.headers.get("content-security-policy", "")
            assert "script-src 'self'" in csp and "style-src 'self'" in csp
            assert "strict-transport-security" in auth_get.headers
            assert "permissions-policy" in auth_get.headers

            hidden = dict(
                re.findall(
                    r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
                    auth_get.text,
                )
            )
            approve = await client.post(
                "/oauth/authorize",
                data={**hidden, "decision": "allow"},
            )
            assert approve.status_code == 302
            code = re.search(r"code=([^&]+)", approve.headers["location"]).group(1)

            tok = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": cid,
                    "code": code,
                    "redirect_uri": "http://[::1]:9876/callback",
                    "code_verifier": verifier,
                },
            )
            assert tok.status_code == 200
            oauth_access = tok.json()["access_token"]

            # Portal lists grant, and revoking it blocks OAuth bearer immediately
            # while leaving static MCP token intact.
            portal = await client.get("/")
            assert portal.status_code == 200
            assert "IPv6 CLI" in portal.text
            grant_id = app.state.anki_oauth_store.list_user_grants("alice")[0]["grant_id"]
            p_csrf = _csrf_from(portal.text)

            # Initialize an MCP session under OAuth access token first.
            oauth_headers = await _init_mcp(client, oauth_access)
            revoke = await client.post(
                "/portal/oauth/revoke",
                data={"csrf_token": p_csrf, "grant_id": grant_id},
            )
            assert revoke.status_code == 303

            after_revoke = await client.post(
                "/mcp",
                headers=oauth_headers,
                json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            )
            assert after_revoke.status_code == 401

            # Static token still works!
            static_token = app.state.anki_store.peek_token("alice")
            static_headers = await _init_mcp(client, static_token, req_id=10)
            ok = await client.post(
                "/mcp",
                headers=static_headers,
                json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
            )
            assert ok.status_code == 200

            # Expired session on POST /oauth/authorize preserves next= in login redirect.
            client.cookies.clear()
            expired_post = await client.post(
                "/oauth/authorize",
                data={
                    "client_id": cid,
                    "redirect_uri": "http://[::1]:9876/callback",
                    "response_type": "code",
                    "scope": "mcp",
                    "state": "s2",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "decision": "allow",
                },
            )
            assert expired_post.status_code == 302
            assert "/login?next=" in expired_post.headers["location"]


def test_tool_error_response_redacts_secrets():
    res = _tool_error_response(RuntimeError("boom for alice secret=xyz"))
    assert res["success"] is False
    assert "xyz" not in res["error"]
    assert "[REDACTED]" in res["error"]


# ---------------------------------------------------------------------------
# 3. Sync serialization, JSONL tool usage log, and new/improved MCP tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_before_tool_serializes_concurrent_calls_and_stamps_ttl_after(tmp_path):
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file, encryption_secret=SECRET)
    store.create_user("alice", "alice-pw")
    store.set_ankiweb_credentials("alice", "alice@example.com", "pw")

    sync_calls = 0

    class FakeRegistry:
        async def sync_user(self, *_args, **_kwargs):
            nonlocal sync_calls
            sync_calls += 1
            await asyncio.sleep(0.03)
            return {"success": True, "output": "ok"}

    settings = Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=users_file,
        sync_before_tools_ttl_seconds=60,
    )
    sm = SyncManager(FakeRegistry(), store, settings)

    # Launch two concurrent pre-tool syncs: per-user lock runs the first, and
    # the second observes the post-sync TTL stamp and skips redundant sync.
    await asyncio.gather(
        sm.sync_before_tool("alice"),
        sm.sync_before_tool("alice"),
    )
    assert sync_calls == 1


@pytest.mark.asyncio
async def test_tool_usage_logger_jsonl_persistence_migration_and_compaction(tmp_path):
    # Migrate legacy JSON list first.
    alice_dir = tmp_path / "users" / "alice"
    alice_dir.mkdir(parents=True)
    legacy = alice_dir / "tool_usage.json"
    legacy.write_text(
        json.dumps([{"ts": "2026-01-01T00:00:00Z", "tool": "list_decks", "ok": True, "duration_ms": 5}]),
        encoding="utf-8",
    )
    logger = ToolUsageLogger(data_dir=str(tmp_path), max_entries=3)
    assert (await logger.recent("alice"))[0]["tool"] == "list_decks"

    # Append 8 more entries to trigger compaction (> 2 * max_entries = 6).
    for i in range(8):
        await logger.record("alice", f"tool_{i}", ok=True, duration_ms=i)

    jsonl_path = alice_dir / "tool_usage.jsonl"
    assert jsonl_path.exists()
    reloaded = ToolUsageLogger(data_dir=str(tmp_path), max_entries=3)
    recent = await reloaded.recent("alice", limit=10)
    assert len(recent) == 3
    assert [e["tool"] for e in recent] == ["tool_7", "tool_6", "tool_5"]


@pytest.mark.asyncio
async def test_mcp_tool_improvements_resources_and_prompts(tmp_path):
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file, encryption_secret=SECRET)
    _u, token = store.create_user("alice", "alice-pw")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    app = create_app(settings)

    # Unit check on _truncate_html stripping <style> blocks.
    styled = "<style>.card { font-size: 99px; }</style><div>Question body</div>"
    assert card_tools._truncate_html(styled, 200, strip_style=True) == (
        "<div>Question body</div>",
        False,
    )

    async with app.router.lifespan_context(app):
        reg = app.state.anki_registry
        sm = app.state.anki_sync_manager

        # Seed a note with tags, media, and check direct tool helpers.
        add_res = await note_tools.add_notes(
            sm,
            reg,
            "alice",
            notes=[
                NoteInput(
                    deck_name="Default",
                    note_type="Basic",
                    fields={"Front": "Hello world", "Back": "Hola mundo"},
                    tags=["spanish", "greetings"],
                )
            ],
        )
        assert add_res.success is True
        nid = add_res.data["created_note_ids"][0]

        # find_and_replace dry_run previews without mutating.
        dry = await note_tools.find_and_replace(
            sm,
            reg,
            "alice",
            search="Hello",
            replacement="Hi",
            note_ids=[nid],
            dry_run=True,
        )
        assert dry.success is True
        assert dry.data["dry_run"] is True
        assert dry.data["matched_notes"] == 1
        assert dry.data["matched_occurrences"] == 1
        fetched = await note_tools.get_note(reg, "alice", nid)
        assert fetched["fields"]["Front"] == "Hello world"

        # Actual find_and_replace with idempotency_key deduplicates retries.
        real_edit = await note_tools.find_and_replace(
            sm,
            reg,
            "alice",
            search="Hello",
            replacement="Hi",
            note_ids=[nid],
            idempotency_key="far-1",
        )
        assert real_edit.data["changed"] == 1
        replay = await note_tools.find_and_replace(
            sm,
            reg,
            "alice",
            search="Hello",
            replacement="Hi",
            note_ids=[nid],
            idempotency_key="far-1",
        )
        assert any("replay" in w.lower() for w in replay.warnings)
        assert (await note_tools.get_note(reg, "alice", nid))["fields"]["Front"] == "Hi world"

        # list_tags, get_sync_status
        tags = await note_tools.list_tags(reg, "alice")
        assert tags == {"tags": ["greetings", "spanish"], "count": 2}
        sync_st = await sync_tools.get_sync_status(sm, "alice")
        assert sync_st["ankiweb_configured"] is False
        assert sync_st["maintenance_mode"] is False

        # store_media_file + delete_media_file (confirm gate + snapshot + removal)
        b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
        stored = await media_tools.store_media_file(sm, reg, "alice", "sample_test.png", b64)
        stored_name = stored.data["filename"]
        needs_confirm = await media_tools.delete_media_file(sm, reg, "alice", stored_name, confirm=False)
        assert needs_confirm.success is False
        deleted = await media_tools.delete_media_file(sm, reg, "alice", stored_name, confirm=True)
        assert deleted.success is True
        assert deleted.data["deleted"] is True
        assert stored_name not in (await media_tools.list_media_files(reg, "alice"))["files"]

        # answer_card accepts elapsed_ms
        q = await card_tools.get_review_queue(reg, "alice", limit=5, deck="Default")
        cid = q["cards"][0]["card_id"]
        ans = await card_tools.answer_card(sm, reg, "alice", cid, rating=3, elapsed_ms=4200)
        assert ans.success is True

        # MCP protocol checks for Resources and Prompts
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            headers = await _init_mcp(client, token)

            res_tags = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "resources/read",
                    "params": {"uri": "anki://collection/tags"},
                },
            )
            assert res_tags.status_code == 200
            tags_payload = json.loads(_parse_mcp(res_tags)["result"]["contents"][0]["text"])
            assert tags_payload["tags"] == ["greetings", "spanish"]

            prompts_list = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 21, "method": "prompts/list"},
            )
            assert prompts_list.status_code == 200
            names = {p["name"] for p in _parse_mcp(prompts_list)["result"]["prompts"]}
            assert {"create_flashcards", "review_session"} <= names

            prompt_get = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "prompts/get",
                    "params": {"name": "create_flashcards", "arguments": {"topic": "Biology"}},
                },
            )
            assert prompt_get.status_code == 200
            msg = _parse_mcp(prompt_get)["result"]["messages"][0]["content"]["text"]
            assert "Biology" in msg


# ---------------------------------------------------------------------------
# 4. Web Dashboard UI/UX routes (/static/*, password, disconnect, test, backups, maintenance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_static_password_credentials_and_maintenance(tmp_path):
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file, encryption_secret=SECRET)
    store.create_user("root", "root-password-1", role="admin")
    store.create_user("alice", "alice-old-pw-1", role="user")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            # Static assets are readable unauthenticated.
            css_res = await client.get("/static/app.css")
            js_res = await client.get("/static/app.js")
            assert css_res.status_code == 200 and "text/css" in css_res.headers["content-type"]
            assert js_res.status_code == 200 and "javascript" in js_res.headers["content-type"]

            # Sign in as alice and inspect portal UI/UX elements.
            old_cookie = issue_session(SECRET, "alice")
            client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
            portal = await client.get("/")
            assert portal.status_code == 200
            assert 'name="color-scheme" content="light dark"' in portal.text
            assert 'href="/static/app.css"' in portal.text
            assert 'src="/static/app.js"' in portal.text
            assert 'id="confirm-dialog"' in portal.text
            assert "onclick=" not in portal.text
            assert "MCP client setup" in portal.text

            csrf = _csrf_from(portal.text)

            # Change password keeps current browser session active while killing old cookie.
            pw_res = await client.post(
                "/portal/password",
                data={
                    "csrf_token": csrf,
                    "current_password": "alice-old-pw-1",
                    "new_password": "alice-new-pw-2",
                    "confirm_password": "alice-new-pw-2",
                },
            )
            assert pw_res.status_code == 303
            assert app.state.anki_store.verify_password("alice", "alice-new-pw-2")
            assert (await client.get("/")).status_code == 200

            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
            assert (await client.get("/")).status_code == 303  # invalidated old session!

            # Re-auth with new session, save/test/disconnect AnkiWeb credentials.
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "alice"))
            csrf = _csrf_from((await client.get("/")).text)
            await client.post(
                "/portal/credentials",
                data={"csrf_token": csrf, "email": "alice@example.com", "password": "aw-pw"},
            )
            with patch(
                "anki.collection.Collection.sync_login",
                return_value=object(),
            ):
                test_res = await client.post("/portal/credentials/test", data={"csrf_token": csrf})
                assert test_res.status_code == 303
            after_test = await client.get("/")
            assert "AnkiWeb login check succeeded." in after_test.text

            csrf = _csrf_from(after_test.text)
            disc = await client.post("/portal/credentials/disconnect", data={"csrf_token": csrf})
            assert disc.status_code == 303
            assert app.state.anki_store.get("alice").ankiweb_email == ""

            # Admin maintenance toggle & token reveal username attribution.
            client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "root"))
            admin_get = await client.get("/admin")
            a_csrf = _csrf_from(admin_get.text)

            maint_on = await client.post(
                "/admin/maintenance",
                data={"csrf_token": a_csrf, "enabled": "true"},
            )
            assert maint_on.status_code == 303
            assert app.state.anki_sync_manager.maintenance_mode is True

            maint_off = await client.post(
                "/admin/maintenance",
                data={"csrf_token": a_csrf, "enabled": "false"},
            )
            assert maint_off.status_code == 303
            assert app.state.anki_sync_manager.maintenance_mode is False

            regen = await client.post(
                "/admin/regenerate-token",
                data={"csrf_token": a_csrf, "username": "alice"},
            )
            assert regen.status_code == 303
            admin_flash = await client.get("/admin")
            assert "Copy this now for <strong>alice</strong>" in admin_flash.text
