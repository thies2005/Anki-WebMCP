"""Portal Tools page + admin global tool rules (session/CSRF-protected UIs)."""

import re

import httpx
import pytest

# src.main pulls anki.collection first — the wheel's anki.notes cannot be the
# FIRST anki module imported (circular import through anki.cards).
from src.config import Settings
from src.main import create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_session

SECRET = "test-session-secret"
ALICE_PASSWORD = "correct-horse"


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", ALICE_PASSWORD)
    store.create_user("root", "root-pw", role="admin")
    return Settings(
        session_secret=SECRET,
        data_dir=str(tmp_path),
        users_file=str(users_file),
    )


@pytest.fixture
def app(tmp_path):
    return create_app(_make_settings(tmp_path))


@pytest.fixture
def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )


def _login(client, username="alice"):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, username))


def _csrf_from(html_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', html_text)
    assert match, "page must carry a CSRF token"
    return match.group(1)


# -- Tools page ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_page_requires_session(client):
    res = await client.get("/portal/tools")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_tools_page_renders_activity_rules_and_reference(client):
    _login(client)
    res = await client.get("/portal/tools")
    assert res.status_code == 200
    assert "Tool activity" in res.text
    assert "Your disabled-tool rules" in res.text
    assert "All tools" in res.text
    # The rules form posts to the right place; the reference covers every group.
    assert 'action="/portal/tools/disabled"' in res.text
    for group, names in [
        ("Decks", ["list_decks", "delete_deck"]),
        ("Review", ["get_review_queue", "answer_card"]),
        ("Sync &amp; stats", ["sync_collection"]),
    ]:
        assert group in res.text
        for name in names:
            assert name in res.text


@pytest.mark.asyncio
async def test_tools_page_shows_own_and_global_rule_chips(client, app):
    app.state.anki_store.set_disabled_tool_patterns("alice", ["delete_notes"])
    app.state.anki_tool_policy.set_patterns(["sync_*"])
    _login(client)
    res = await client.get("/portal/tools")
    assert res.status_code == 200
    assert "disabled: your rule delete_notes" in res.text
    assert "disabled: global rule sync_*" in res.text
    # Global rules are explained as read-only, not editable here.
    assert "Global rules currently in effect" in res.text
    assert 'action="/admin/tool-rules"' not in res.text


@pytest.mark.asyncio
async def test_save_own_rules_roundtrip_and_flash(client, app):
    _login(client)
    page = await client.get("/portal/tools")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/portal/tools/disabled",
        data={"csrf_token": csrf, "patterns": "delete_*\n\nsync_collection\n"},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/portal/tools"
    # Saved normalized (blank line dropped) on the app's own store.
    assert app.state.anki_store.get("alice").disabled_tool_patterns == [
        "delete_*",
        "sync_collection",
    ]
    # The saved rules render back into the textarea and the status table.
    page2 = await client.get("/portal/tools")
    assert "disabled: your rule delete_*" in page2.text


@pytest.mark.asyncio
async def test_save_own_rules_rejects_bad_pattern_without_clobbering(client, app):
    app.state.anki_store.set_disabled_tool_patterns("alice", ["delete_*"])
    _login(client)
    page = await client.get("/portal/tools")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/portal/tools/disabled",
        data={"csrf_token": csrf, "patterns": "not a rule"},
    )
    assert res.status_code == 303  # flash-error redirect back to the page
    assert app.state.anki_store.get("alice").disabled_tool_patterns == ["delete_*"]


@pytest.mark.asyncio
async def test_save_own_rules_requires_csrf(client):
    _login(client)
    res = await client.post("/portal/tools/disabled", data={"patterns": "delete_*"})
    assert res.status_code == 403


# -- Admin global rules -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_page_has_global_rules_section(client):
    _login(client, "root")
    res = await client.get("/admin")
    assert res.status_code == 200
    assert "Global tool rules" in res.text
    assert 'action="/admin/tool-rules"' in res.text


@pytest.mark.asyncio
async def test_admin_saves_global_rules_gating_every_account(client, app):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post(
        "/admin/tool-rules", data={"csrf_token": csrf, "patterns": "sync_collection"}
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/admin"
    assert app.state.anki_tool_policy.get_patterns() == ["sync_collection"]
    # Visible on the (non-admin) user's Tools page as a read-only global rule.
    _login(client, "alice")
    page2 = await client.get("/portal/tools")
    assert "disabled: global rule sync_collection" in page2.text


@pytest.mark.asyncio
async def test_admin_tool_rules_forbidden_for_normal_users(client, app):
    _login(client, "alice")
    res = await client.post("/admin/tool-rules", data={"patterns": "sync_collection"})
    assert res.status_code == 403
    assert app.state.anki_tool_policy.get_patterns() == []


@pytest.mark.asyncio
async def test_admin_tool_rules_rejects_bad_pattern(client, app):
    _login(client, "root")
    page = await client.get("/admin")
    csrf = _csrf_from(page.text)
    res = await client.post("/admin/tool-rules", data={"csrf_token": csrf, "patterns": "*"})
    assert res.status_code == 400
    assert app.state.anki_tool_policy.get_patterns() == []


@pytest.mark.asyncio
async def test_admin_tool_rules_requires_csrf(client):
    _login(client, "root")
    res = await client.post("/admin/tool-rules", data={"patterns": "sync_collection"})
    assert res.status_code == 403
