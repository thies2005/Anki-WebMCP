"""Phase 3 verification gate (audit F-09..F-13, Medium).

Covers exactly the Phase 3 gate scenarios from ``.zcode/remediation-spec.md``:

- F-09: an ``add_notes`` first field containing ``*`` or ``_`` no longer
  falsely matches unrelated notes (the duplicate candidate query escapes the
  Anki wildcards, ``_escape_exact_search_term``, and every candidate is
  verified in Python), and deck-scoped duplicate detection does not reject a
  note whose only same-field twin sits in a SUBDECK — ``deck:"Parent"``
  matches ``Parent::Child`` on the wheel (probed), so the Python
  exact-effective-deck check (``odid or did == target_did``) is what keeps
  the parent-deck add clean while the exact-deck add still fails.
- F-10: malformed Anki search syntax through ``search_notes``,
  ``find_cards`` and ``find_duplicates`` surfaces as a clean
  ``CollectionToolError`` instead of the raw backend exception, and an
  invalid regex with ``find_and_replace(..., regex=True)`` fails with the
  dedicated tool-error message instead of a raw ``InvalidInput``.
  Both malformed-query classes the wheel produces are covered: parser
  failures raise ``SearchError`` and regex PARSE errors (e.g. ``re:[invalid``,
  the spec F-10's own example) raise its sibling ``InvalidInput`` — the
  production catches on the find paths handle both.
- F-11: restoring a backup clears the user's idempotency ledger, so a key
  committed AFTER the restored snapshot executes fresh instead of replaying
  a result the restored collection never produced (verified in memory, on
  a fresh ledger reloaded from disk, and by the actual re-execution).
- F-12: changing the initial admin's password deletes the
  ``initial-admin-password.txt`` bootstrap file from the data directory
  (a non-admin password change leaves it alone; a second change on an
  already-deleted file is a no-op, not an error).
- F-13: ``/oauth/token`` responses carry ``Cache-Control: no-store`` (fresh
  code exchange, refresh grant, and 400 error alike); ``POST /logout``
  revokes the session SERVER-side (a cookie copied before logout no longer
  authenticates); ``FORCE_SECURE_COOKIES=1``/``yes``/``true`` all enable
  the ``Secure`` cookie attribute while the default keeps it off; the
  logging redaction covers ``refresh_token`` (and the other compound OAuth
  credential keys) plus quoted passwords containing spaces.

Determinism: everything is in-process — real per-user collections in a tmp
data dir (the established tool-test contract) and real ASGI requests via
httpx; no sleeps and no network. No same-second sequencing is needed for
this phase's scenarios.
"""

import logging
from http.cookies import SimpleCookie

import anki.lang
import httpx
import pytest
import pytest_asyncio

# src.collections imports anki.collection first; the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through
# anki.cards) — same guarded import order as the other registry tests.
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.ledger import IdempotencyLedger
from src.logging_util import RedactingFormatter, redact_string, sanitize_data
from src.models import NoteInput
from src.main import PRELOGIN_COOKIE_NAME, PRELOGIN_FIELD_NAME
from src.sync_manager import SyncManager
from src.tools import cards as card_tools
from src.tools import notes as note_tools
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME
from src.errors import CollectionToolError

# Shared form-login + OAuth-flow helpers (established cross-module pattern).
from test_audit_fixes_b import ALICE_PASSWORD, CSRF_RE, login_via_form
from test_phase1_security_fixes import (
    _consent_code,
    _exchange_code,
    _make_app,
    _refresh,
    _register_client,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirror test_mcp_tools.py / test_mass_edit_tools.py)
# ---------------------------------------------------------------------------


if anki.lang.current_i18n is None:  # find_dupes needs the wheel's i18n shim
    anki.lang.set_lang("en")


def _add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return int(note.id)


def _count_notes(col):
    return len(col.find_notes(""))


async def _fronts(reg, username, nids):
    def _get(col):
        return {nid: col.get_note(nid).fields[0] for nid in nids}

    return await reg.run(username, _get)


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data dir (empty user store:
    the mutation path only reads it for the local-only sync nudge)."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


def _note(front, deck="Default", back="b"):
    return NoteInput(deck_name=deck, note_type="Basic", fields={"Front": front, "Back": back})


# ---------------------------------------------------------------------------
# F-09: add_notes duplicate check — wildcard escaping + exact-deck scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_notes_wildcard_first_field_does_not_false_match(env):
    """An existing note whose first field contains ``*`` or ``_`` must not
    swallow unrelated additions: the candidate query escapes the wildcards,
    so a decoy differing exactly in those positions is NOT a duplicate —
    while re-adding the exact value still is. (Probed on the wheel: inside a
    quoted term the UNescaped ``cat_dog*fish`` matches ``catXdogYfish`` too.)"""
    reg, manager = env
    await reg.run("nora", _add_basic_note, "cat_dog*fish")
    await reg.run("nora", _add_basic_note, "under_score")

    # The decoys the unescaped wildcard query used to sweep up.
    star_decoy = await note_tools.add_notes(manager, reg, "nora", [_note("catXdogYfish")])
    assert star_decoy.success is True, star_decoy.error
    underscore_decoy = await note_tools.add_notes(manager, reg, "nora", [_note("underXscore")])
    assert underscore_decoy.success is True, underscore_decoy.error

    # All four notes exist: nothing was silently swallowed or mis-flagged.
    assert await reg.run("nora", _count_notes) == 4

    # The exact wildcard-bearing values still detect as duplicates.
    dup_star = await note_tools.add_notes(manager, reg, "nora", [_note("cat_dog*fish")])
    assert dup_star.success is False
    assert dup_star.local_status == "failed"
    assert "Duplicate check failed" in dup_star.error
    dup_underscore = await note_tools.add_notes(manager, reg, "nora", [_note("under_score")])
    assert dup_underscore.success is False
    assert "Duplicate check failed" in dup_underscore.error


@pytest.mark.asyncio
async def test_add_notes_deck_scope_ignores_duplicate_in_subdeck(env):
    """duplicate_scope 'deck' semantics: a same-first-field note that exists
    ONLY in ``Parent::Child`` does not block adding the value to ``Parent`` —
    the candidate's cards must sit in the TARGET deck itself, and
    ``deck:"Parent"`` matching subdecks on the wheel makes the Python
    exact-effective-deck check the deciding gate. The exact-deck add still
    fails as a duplicate."""
    reg, manager = env
    seed = await note_tools.add_notes(
        manager, reg, "otto", [_note("scoped-q", deck="Parent::Child")]
    )
    assert seed.success is True, seed.error

    # The duplicate exists only in the SUBDECK: the parent-deck add goes through.
    parent_add = await note_tools.add_notes(manager, reg, "otto", [_note("scoped-q", deck="Parent")])
    assert parent_add.success is True, parent_add.error

    # ...while the same value in the EXACT deck is still caught.
    dup = await note_tools.add_notes(
        manager, reg, "otto", [_note("scoped-q", deck="Parent::Child")]
    )
    assert dup.success is False
    assert dup.local_status == "failed"
    assert "Duplicate check failed" in dup.error

    # Exactly the two legitimate notes exist (subdeck + parent).
    assert await reg.run("otto", _count_notes) == 2


# ---------------------------------------------------------------------------
# F-10: malformed queries / invalid regex surface as CollectionToolError
# ---------------------------------------------------------------------------

# Both malformed shapes raise the wheel's SearchError (probed): a bare
# unbalanced quote fails the search parser, and ``re:(`` fails while building
# the regex node — both regardless of collection contents.
MALFORMED_QUERIES = ['Front:"unclosed', "re:("]
_QUERY_IDS = ["unbalanced-quote", "unbalanced-regex-bracket"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", MALFORMED_QUERIES, ids=_QUERY_IDS)
async def test_search_notes_malformed_query_raises_cleanly(env, query):
    reg, _manager = env
    await reg.run("kaja", _add_basic_note, "hello")
    with pytest.raises(CollectionToolError, match="Invalid search query"):
        await note_tools.search_notes(reg, "kaja", query)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", MALFORMED_QUERIES, ids=_QUERY_IDS)
async def test_find_cards_malformed_query_raises_cleanly(env, query):
    reg, _manager = env
    await reg.run("leif", _add_basic_note, "hello")
    with pytest.raises(CollectionToolError, match="Invalid search query"):
        await card_tools.find_cards(reg, "leif", query)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", MALFORMED_QUERIES, ids=_QUERY_IDS)
async def test_find_duplicates_malformed_prefilter_raises_cleanly(env, query):
    reg, _manager = env
    await reg.run("maja", _add_basic_note, "hello")
    with pytest.raises(CollectionToolError, match="Invalid search query"):
        await note_tools.find_duplicates(reg, "maja", "Front", search=query)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["search_notes", "find_cards", "find_duplicates"]
)
async def test_regex_parse_error_query_raises_cleanly(env, tool_name):
    """``re:[invalid`` — the spec F-10's own malformed-syntax example — compiles
    lazily and raises the wheel's InvalidInput once candidates exist (probed:
    empty collections short-circuit, populated ones raise 'regex parse error:
    unclosed character class'). The find-path catches handle InvalidInput
    alongside SearchError, so this surfaces as a clean CollectionToolError."""
    reg, _manager = env
    await reg.run("nils", _add_basic_note, "hello")
    if tool_name == "search_notes":
        call = note_tools.search_notes(reg, "nils", "re:[invalid")
    elif tool_name == "find_cards":
        call = card_tools.find_cards(reg, "nils", "re:[invalid")
    else:
        call = note_tools.find_duplicates(reg, "nils", "Front", search="re:[invalid")
    with pytest.raises(CollectionToolError, match="Invalid search query"):
        await call


@pytest.mark.asyncio
async def test_find_and_replace_invalid_regex_is_a_clean_tool_error(env):
    """With regex=True a pattern the backend cannot parse fails with the
    dedicated tool-error message (the InvalidInput was converted to a
    CollectionToolError inside the mutation, which execute_mutation surfaces
    as a structured failed response) — and the note is untouched."""
    reg, manager = env
    nid = await reg.run("pia", _add_basic_note, "the colour red")

    res = await note_tools.find_and_replace(
        manager, reg, "pia", search="(", replacement="x", note_ids=[nid], regex=True
    )
    assert res.success is False
    assert res.local_status == "failed"
    assert res.error.startswith("Invalid find_and_replace input or regular expression")
    assert (await _fronts(reg, "pia", [nid]))[nid] == "the colour red"


# ---------------------------------------------------------------------------
# F-11: restore clears the idempotency ledger — post-snapshot keys re-run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_backup_clears_ledger_and_post_snapshot_key_executes_fresh(tmp_path):
    """Snapshot -> keyed mutation -> replay hit -> restore. After the restore
    the key must be gone (memory AND disk) and re-run FRESH against the
    restored collection: without the clear, execute_mutation would replay the
    cached committed result and the restored collection would silently stay
    without the mutation."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    try:
        first = await note_tools.add_notes(
            manager, reg, "vera", [_note("pre-snapshot")], idempotency_key="pre-snapshot-key"
        )
        assert first.success is True, first.error
        snapshot = await reg.backup_user("vera", "manual")

        # A mutation committed AFTER the snapshot, then its replay hit.
        second = await note_tools.add_notes(
            manager, reg, "vera", [_note("post-snapshot")], idempotency_key="post-snapshot-key"
        )
        assert second.success is True, second.error
        assert second.warnings == []  # no AnkiWeb credentials -> no nudge
        replay = await note_tools.add_notes(
            manager, reg, "vera", [_note("post-snapshot")], idempotency_key="post-snapshot-key"
        )
        assert replay.success is True
        assert any("Idempotent operation replay" in w for w in replay.warnings)
        assert await reg.run("vera", _count_notes) == 2  # the replay applied nothing

        result = await reg.restore_backup("vera", snapshot)
        assert result["restored_from"] == snapshot

        # The ledger is wiped in memory and on disk (a fresh instance loaded
        # from the ledger file sees no records either).
        assert await reg.ledger("vera").get("post-snapshot-key") is None
        assert await reg.ledger("vera").get("pre-snapshot-key") is None
        reloaded = IdempotencyLedger(reg._ledger_path("vera"))
        assert await reloaded.get("post-snapshot-key") is None

        # The collection is back to the snapshot state (one note) ...
        assert await reg.run("vera", _count_notes) == 1

        # ... and the SAME key now executes FRESH: no replay warning, the
        # mutation really runs, and the restored collection gains the note.
        fresh_run = await note_tools.add_notes(
            manager, reg, "vera", [_note("post-snapshot")], idempotency_key="post-snapshot-key"
        )
        assert fresh_run.success is True, fresh_run.error
        assert fresh_run.local_status == "committed"
        assert fresh_run.warnings == []
        assert await reg.run("vera", _count_notes) == 2
    finally:
        await reg.close_all()


# ---------------------------------------------------------------------------
# F-12: admin password change deletes the bootstrap credentials file
# ---------------------------------------------------------------------------


def test_admin_password_change_deletes_bootstrap_credentials_file(tmp_path):
    """set_password on the initial admin removes initial-admin-password.txt
    next to the user store; a NON-admin password change leaves it alone, and
    changing the admin password again (file already gone) is a silent no-op."""
    store = UserStore(str(tmp_path / "users.json"))
    store.create_user("root", "first-pw", role="admin")
    store.create_user("alice", "alice-pw")
    bootstrap = tmp_path / "initial-admin-password.txt"
    bootstrap.write_text("username: root\npassword: first-pw\n", encoding="utf-8")
    assert bootstrap.exists()

    # Non-admin change: the bootstrap file is not theirs to delete.
    store.set_password("alice", "alice-pw-2")
    assert bootstrap.exists()

    # The initial admin sets their own password: the stale plaintext goes.
    store.set_password("root", "second-pw")
    assert not bootstrap.exists()

    # Idempotent: no error when the file is already gone.
    store.set_password("root", "third-pw")
    assert not bootstrap.exists()


# ---------------------------------------------------------------------------
# F-13: token response caching, server-side logout revocation,
#       FORCE_SECURE_COOKIES normalization, secret redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_token_responses_carry_cache_control_no_store(tmp_path):
    """Every /oauth/token response — fresh code exchange, refresh grant, and
    the 400 for a spent code — carries Cache-Control: no-store (RFC 6749 5.1)."""
    app = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            client_id = await _register_client(client)
            code, verifier = await _consent_code(client, client_id)

            res = await _exchange_code(client, client_id, code, verifier)
            assert res.status_code == 200, res.text
            assert res.headers["cache-control"] == "no-store"
            refresh = res.json()["refresh_token"]

            res = await _refresh(client, client_id, refresh)
            assert res.status_code == 200, res.text
            assert res.headers["cache-control"] == "no-store"

            # Error branch: replaying the spent authorization code.
            res = await _exchange_code(client, client_id, code, verifier)
            assert res.status_code == 400
            assert res.json()["error"] == "invalid_grant"
            assert res.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_logout_revokes_copied_session_cookie_server_side(tmp_path):
    """Logout must not merely clear the cookie in the logging-out browser: a
    COPY of the pre-logout cookie value must stop authenticating, because
    logout stamps the account's session-revocation epoch."""
    app = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            stolen = client.cookies.get(SESSION_COOKIE_NAME)
            assert stolen, "login must have set the session cookie"

            page = await client.get("/")
            assert page.status_code == 200  # the stolen cookie is live pre-logout
            match = CSRF_RE.search(page.text)
            assert match, "portal page must embed a CSRF token"

            res = await client.post("/logout", data={"csrf_token": match.group(1)})
            assert res.status_code == 303
            assert res.headers["location"] == "/login"
            # The logging-out browser is also cleaned up (all three cookies).
            set_cookie = res.headers.get("set-cookie", "")
            assert SESSION_COOKIE_NAME in set_cookie

            # The copied pre-logout cookie no longer authenticates anyone.
            client.cookies.set(SESSION_COOKIE_NAME, stolen)
            bounced = await client.get("/")
            assert bounced.status_code == 303
            assert bounced.headers["location"] == "/login"


async def _secure_login_response(client: httpx.AsyncClient) -> httpx.Response:
    """Complete the login form under FORCE_SECURE_COOKIES.

    httpx never replays Secure cookies over plain http, so the prelogin pair
    cannot round-trip through the jar under this flag: the GET /login response
    is parsed for the prelogin cookie (whose Secure attribute is asserted by
    the caller), and the POST carries that cookie manually in the header —
    the same pair the browser would send.
    """
    page = await client.get("/login")
    assert page.status_code == 200
    jar = SimpleCookie()
    jar.load(page.headers["set-cookie"])
    assert PRELOGIN_COOKIE_NAME in jar, "GET /login must set the prelogin cookie"
    prelogin = jar[PRELOGIN_COOKIE_NAME]
    assert prelogin["secure"], "the prelogin cookie must carry Secure under the flag"
    return await client.post(
        "/login",
        data={
            "username": "alice",
            "password": ALICE_PASSWORD,
            PRELOGIN_FIELD_NAME: prelogin.value,
        },
        headers={"cookie": f"{PRELOGIN_COOKIE_NAME}={prelogin.value}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", "yes", "true", "TRUE"])
async def test_force_secure_cookie_values_enable_secure_attribute(tmp_path, monkeypatch, value):
    """FORCE_SECURE_COOKIES accepts the same truthy spellings as config's
    _env_bool: 1 / yes / true, any case — all must put the Secure attribute
    on the session cookie the login form issues."""
    monkeypatch.setenv("FORCE_SECURE_COOKIES", value)
    app = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await _secure_login_response(client)
            assert login.status_code == 303, login.text
            jar = SimpleCookie()
            jar.load(login.headers["set-cookie"])
            assert SESSION_COOKIE_NAME in jar
            assert jar[SESSION_COOKIE_NAME]["secure"], (
                f"FORCE_SECURE_COOKIES={value!r} must set the Secure attribute"
            )


@pytest.mark.asyncio
async def test_default_keeps_session_cookie_non_secure(tmp_path, monkeypatch):
    """Contrast guard: without the env var the cookie stays non-Secure (the
    flag is opt-in for plain-HTTP deployments, not always-on)."""
    monkeypatch.delenv("FORCE_SECURE_COOKIES", raising=False)
    app = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            login = await login_via_form(client, "alice", ALICE_PASSWORD)
            assert login.status_code == 303
            jar = SimpleCookie()
            jar.load(login.headers["set-cookie"])
            assert SESSION_COOKIE_NAME in jar
            assert not jar[SESSION_COOKIE_NAME]["secure"]


def test_redaction_covers_refresh_token_and_quoted_passwords():
    """The exact-match key redaction covers the compound OAuth/credential key
    names, and the pattern redaction consumes a QUOTED value containing
    spaces in full (the bare-token pattern used to leave the tail visible)."""
    sanitized = sanitize_data(
        {
            "refresh_token": "rt-secret-123",
            "access_token": "at-secret-456",
            "code_verifier": "cv-secret-789",
            "ankiweb_password": "hunter2",
            "token_plaintext": "tp-secret-000",
            "client_secret": "cs-secret-111",
            "safe_field": "visible",
        }
    )
    for key in (
        "refresh_token",
        "access_token",
        "code_verifier",
        "ankiweb_password",
        "token_plaintext",
        "client_secret",
    ):
        assert sanitized[key] == "[REDACTED]", key
    assert sanitized["safe_field"] == "visible"

    quoted = '"password": "my secret pass"'
    redacted = redact_string(quoted)
    assert "my secret pass" not in redacted
    assert redacted == '"password": "[REDACTED]"'

    # Single-quoted values with spaces are consumed in full too.
    single = "'password': 'another secret phrase'"
    redacted_single = redact_string(single)
    assert "another secret phrase" not in redacted_single

    # And the live formatter applies the same redaction to a log record.
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='token refresh for u: "refresh_token": "rt-secret-123" failed',
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert "rt-secret-123" not in formatted
    assert "[REDACTED]" in formatted
