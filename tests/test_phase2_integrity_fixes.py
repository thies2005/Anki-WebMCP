"""Phase 2 verification gate (audit F-06..F-08, Medium-High).

Covers exactly the Phase 2 gate scenarios from ``.zcode/remediation-spec.md``:

- F-06: a password reset followed by a login completing in the SAME
  wall-clock second must reject the pre-reset cookie and accept the
  post-reset one. ``issue_session`` stamps the issued-at segment at
  microsecond resolution and ``session_issued_before`` compares it as a
  float against ``User.sessions_invalid_before``, so the post-reset stamp
  floors ABOVE the reset's fractional epoch (a whole-second stamp would
  fall below it and kill the fresh login). No sleeping: a fake
  ``time.time`` walks the same second in fractions.
- F-07a: when the backend writes no new file (collection unchanged since
  the last pre-sync backup), ``_backup_locked`` with a destructive-op
  reason materializes a SAFETY-tagged copy (``backup-*-delete-deck.colpkg``)
  of the latest snapshot; that copy survives 35 subsequent pre-sync
  backups because retention is per CLASS (SAFETY cap vs routine budget)
  instead of one age-ordered pool.
- F-07b: same-second collision ordering — ``backup-<ts>-pre-sync-2.colpkg``
  (collision attempt 2) sorts NEWER than the same-second
  ``backup-<ts>-pre-sync.colpkg``, via ``_backup_sort_key``'s
  (timestamp, mtime_ns, attempt) ordering; a plain filename tiebreak would
  invert this (ASCII ``'.' > '-'``).
- F-08: ``SyncManager.sync_now`` serializes the ledger check → sync →
  record sequence under the per-user mutation lock (two concurrent calls
  sharing one idempotency key run ``registry.sync_user`` exactly once);
  a fresh success carries the ledger-recorded ``data`` payload so fresh
  and replayed responses have identical keys (the spec gate names this
  the "data.last_sync" payload; the concrete recorded payload is the sync
  output string under ``output`` — the only shape the ledger and the
  replay path have ever carried); a successful sync stamps the pre-tool
  cooldown (``record_explicit_sync``) and ``invalidate_pretool_ttl``
  clears it.

Determinism: no real sleeps anywhere. Same-second sequencing runs on a
fake ``time.time`` (the ``clock`` fixture); the one ``asyncio.sleep(0)``
in the concurrency test is a pure event-loop yield point that forces the
interleaving the lock must survive, not a wait.
"""

import asyncio
import os
import time

import httpx
import pytest
import pytest_asyncio

# src.collections imports anki.collection first; the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through
# anki.cards) — same guarded import order as the other registry tests.
from src.collections import (  # isort: skip
    ROUTINE_BACKUP_REASONS,
    CollectionRegistry,
    _backup_sort_key,
    _parse_backup_filename,
)
from src.config import Settings
from src.main import create_app
from src.sync_manager import SyncManager
from src.users import UserStore
from src.webauth import (
    SESSION_COOKIE_NAME,
    issue_session,
    session_issued_before,
    verify_session,
)

SECRET = "test-session-secret"


# ---------------------------------------------------------------------------
# Deterministic clock + shared fixtures
# ---------------------------------------------------------------------------


class _Clock:
    """Controllable ``time.time`` replacement: frozen until advanced."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Freeze time.time() for the whole test (src.* reads it at call time)."""
    fake = _Clock()
    monkeypatch.setattr(time, "time", fake)
    return fake


def _make_app(tmp_path):
    """Store (alice only) + app, built under whatever clock is installed."""
    users_file = str(tmp_path / "users.json")
    store = UserStore(users_file)
    store.create_user("alice", "alice-pw")
    settings = Settings(session_secret=SECRET, data_dir=str(tmp_path), users_file=users_file)
    return create_app(settings)


def count_notes(col):
    return len(col.find_notes(""))


def _write_backup(path: str, content: bytes = b"collection-state") -> None:
    with open(path, "wb") as f:
        f.write(content)


def _writing_create_backup(clock):
    """A ``Collection.create_backup`` fake mimicking the wheel when the
    collection HAS changed: each call writes a fresh plain
    ``backup-<ts>.colpkg`` named after the fake clock's current second."""

    def create_backup(backup_folder, force, wait_for_completion):
        os.makedirs(backup_folder, exist_ok=True)
        ts = time.strftime("%Y-%m-%d-%H.%M.%S", time.gmtime(clock.now))
        _write_backup(os.path.join(backup_folder, f"backup-{ts}.colpkg"), b"changed-state")
        return True

    return create_backup


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + store + SyncManager against a fresh tmp data dir; alice has
    AnkiWeb credentials (sync_now raises before any ledger write without)."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    store = UserStore(str(tmp_path / "users.json"))
    store.create_user("alice", "pw")
    store.set_ankiweb_credentials("alice", "alice@example.com", "ankiweb-pw")
    manager = SyncManager(reg, store)
    yield reg, store, manager
    await reg.close_all()


@pytest_asyncio.fixture
async def make_registry_fixture(tmp_path):
    """Factory for registries sharing one tmp data_dir (as in
    test_collections.py); tears down all open collections."""
    made = []

    def _make(**kwargs):
        reg = CollectionRegistry(data_dir=str(tmp_path), **kwargs)
        made.append(reg)
        return reg

    yield _make
    for reg in made:
        await reg.close_all()


def _settings(sync_before_tools=True, ttl=0):
    return Settings(
        session_secret="test-session-secret",
        data_dir="/tmp/anki-pretool-test",
        sync_before_tools=sync_before_tools,
        sync_before_tools_ttl_seconds=ttl,
    )


def _patch_sync_user(monkeypatch, reg, result):
    """Replace registry.sync_user with a recording fake returning ``result``.

    The signature must accept every kwarg sync_now / sync_before_tool may
    forward (endpoint, on_step, full_sync_direction, acquire_timeout).
    """
    calls = []

    async def fake_sync_user(
        username, email, password, endpoint=None, on_step=None,
        full_sync_direction=None, acquire_timeout=None,
    ):
        calls.append(
            {
                "username": username,
                "email": email,
                "password": password,
                "full_sync_direction": full_sync_direction,
            }
        )
        return dict(result)

    monkeypatch.setattr(reg, "sync_user", fake_sync_user)
    return calls


# ---------------------------------------------------------------------------
# F-06: same-second password reset
# ---------------------------------------------------------------------------


def test_same_second_reset_rejects_pre_and_accepts_post_reset_cookie(tmp_path, clock):
    """issue session -> set_password -> issue a new session, all inside one
    fake-second walk (no sleeping). The pre-reset cookie stays MAC-valid and
    unexpired — it dies on the revocation EPOCH, which its issued-at stamp
    predates. The post-reset cookie is issued in the SAME wall-clock second
    as the reset and must still clear the epoch: its microsecond issued-at
    stamp floors above the reset's fractional ``sessions_invalid_before``."""
    store = UserStore(str(tmp_path / "users.json"))
    store.create_user("alice", "old-pw")  # created_at/epochs at 1700000000.0

    pre_cookie = issue_session(SECRET, "alice")
    assert session_issued_before(pre_cookie) == 1_700_000_000.0

    clock.advance(0.25)  # the reset lands a fraction of a second later
    store.set_password("alice", "brand-new-pw")
    epoch = store.get("alice").sessions_invalid_before
    assert epoch == 1_700_000_000.25  # sub-second epoch (float time.time())

    clock.advance(0.17)  # login completes in the SAME wall-clock second
    post_cookie = issue_session(SECRET, "alice")
    post_issued = session_issued_before(post_cookie)
    assert int(post_issued) == int(epoch), "scenario requires the same second"

    # The pre-reset cookie fails only on the epoch comparison...
    assert verify_session(SECRET, pre_cookie) == "alice"  # MAC + expiry fine
    assert session_issued_before(pre_cookie) < epoch  # -> portal rejects it
    # ...while the same-second post-reset cookie clears it.
    assert post_issued >= epoch
    assert verify_session(SECRET, post_cookie) == "alice"


@pytest.mark.asyncio
async def test_same_second_reset_through_portal_gate(tmp_path, clock):
    """The real gate (main.get_session_user): the pre-reset cookie is bounced
    to /login, and a cookie issued in the SAME second as the reset renders
    the portal — the exact case a whole-second issued-at stamp used to break."""
    app = _make_app(tmp_path)
    store = app.state.anki_store
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )
    try:
        # Session issued at the frozen instant (same second as account creation).
        client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "alice"))
        assert (await client.get("/")).status_code == 200

        # Reset a fraction of a second later, under the same fake clock.
        clock.advance(0.25)
        store.set_password("alice", "brand-new-pw")

        # The pre-reset cookie is dead: the portal bounces to login.
        res = await client.get("/")
        assert res.status_code == 303
        assert res.headers["location"] == "/login"

        # A login completing in the SAME wall-clock second as the reset works.
        clock.advance(0.17)
        client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, "alice"))
        assert (await client.get("/")).status_code == 200
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# F-07a: unchanged-collection backup promotes a SAFETY-tagged copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_collection_backup_promotes_surviving_delete_deck_copy(
    make_registry_fixture, clock, monkeypatch
):
    """When create_backup writes no new file (collection unchanged since the
    last pre-sync snapshot), _backup_locked with the deck-deletion reason must
    materialize a SAFETY-tagged copy of the latest snapshot — and that copy
    must still be on disk after 35 subsequent pre-sync backups churn through
    the routine retention budget. (The spec gate's "reason delete-deck" is the
    deck-deletion destructive op; the production reason vocabulary prefixes
    destructive ops with ``pre-`` — BACKUP_REASONS / _check_backup_reason —
    so the deck-deletion snapshot is tagged ``pre-delete-deck``.)"""
    reg = make_registry_fixture()  # backup_retention default: 20
    await reg.run("nia", count_notes)  # open collection, create backups dir
    col = reg._users["nia"].col
    backups_dir = reg._backups_dir("nia")

    # The pre-sync backup the collection is unchanged since.
    seeded = "backup-2020-01-01-00.00.00-pre-sync.colpkg"
    _write_backup(os.path.join(backups_dir, seeded), b"collection-state-v1")

    # The wheel writes NO new file while nothing changed.
    monkeypatch.setattr(col, "create_backup", lambda **kwargs: False)

    name = await reg.run("nia", lambda c: reg._backup_locked(c, "nia", "pre-delete-deck"))

    # A genuine, delete-deck-tagged backup file exists on disk (not merely the
    # routine name echoed back) holding the promoted snapshot's bytes.
    parsed = _parse_backup_filename(name)
    assert parsed is not None
    assert parsed[1] == "pre-delete-deck"
    target = os.path.join(backups_dir, name)
    assert os.path.isfile(target)
    with open(target, "rb") as f:
        assert f.read() == b"collection-state-v1"

    # 35 subsequent pre-sync backups, each writing a fresh snapshot one fake
    # second apart; every _backup_locked prunes by retention class on the way
    # out — routine churn must never evict the SAFETY copy.
    monkeypatch.setattr(col, "create_backup", _writing_create_backup(clock))
    for _ in range(35):
        clock.advance(1.0)
        await reg.run("nia", lambda c: reg._backup_locked(c, "nia", "pre-sync"))

    files = os.listdir(backups_dir)
    assert name in files, "delete-deck safety copy must survive 35 pre-sync backups"

    def _reason(fname):
        return _parse_backup_filename(fname)[1]

    safety = [f for f in files if _reason(f) not in ROUTINE_BACKUP_REASONS]
    assert safety == [name]  # exactly the one safety snapshot, still tagged
    routine = [f for f in files if f != name]
    assert len(routine) == 19  # newest max(2, 20 - 1) routine files survive
    assert len(files) == 20  # retention caps respected overall (1 + 19)

    entries = reg.list_backups("nia")
    assert [e["filename"] for e in entries if e["reason"] == "pre-delete-deck"] == [name]


# ---------------------------------------------------------------------------
# F-07b: same-second collision suffix sorts newer
# ---------------------------------------------------------------------------


def test_same_second_collision_suffix_sorts_newer_than_base_name(tmp_path):
    """backup-<ts>-pre-sync-2.colpkg (collision attempt 2) must sort NEWER
    than the same-second backup-<ts>-pre-sync.colpkg — by mtime_ns when the
    write times differ, and by the numeric attempt when they tie. A plain
    filename sort gets this backwards (ASCII '.' > '-')."""
    backups = tmp_path / "backups"
    backups.mkdir()
    base = backups / "backup-2026-04-17-12.00.00-pre-sync.colpkg"
    collision = backups / "backup-2026-04-17-12.00.00-pre-sync-2.colpkg"
    base.write_bytes(b"first")
    collision.write_bytes(b"second")

    # The parser separates the trailing collision counter from the reason:
    # "pre-sync-2" is reason "pre-sync", attempt 2 — not a reason of its own.
    assert _parse_backup_filename(base.name) == ("2026-04-17-12.00.00", "pre-sync", 1)
    assert _parse_backup_filename(collision.name) == ("2026-04-17-12.00.00", "pre-sync", 2)

    # Distinct write times within the same second: mtime_ns decides.
    base_ns = 1_000_000_000
    later_ns = base_ns + 50_000_000
    os.utime(base, ns=(base_ns, base_ns))
    os.utime(collision, ns=(later_ns, later_ns))
    assert _backup_sort_key(str(collision)) > _backup_sort_key(str(base))
    assert (
        sorted([str(base), str(collision)], key=_backup_sort_key, reverse=True)
        == [str(collision), str(base)]
    )

    # Exact same-second tie (identical mtime_ns): the attempt breaks it (1 < 2).
    os.utime(collision, ns=(base_ns, base_ns))
    assert _backup_sort_key(str(base)) == ("2026-04-17-12.00.00", base_ns, 1)
    assert _backup_sort_key(str(collision)) == ("2026-04-17-12.00.00", base_ns, 2)
    assert _backup_sort_key(str(collision)) > _backup_sort_key(str(base))

    # The regression this pins: ascending filename order puts the -2 file
    # FIRST ('-' < '.'), so a name-based newest-first would pick the OLDER
    # snapshot. The chronological key must not reproduce that.
    assert sorted([base.name, collision.name]) == [collision.name, base.name]
    assert (
        sorted([base.name, collision.name], key=_backup_sort_key, reverse=True)
        == [collision.name, base.name]
    )

    # End to end through the user-facing listing: newest first puts the
    # collision copy above the base snapshot.
    reg = CollectionRegistry(data_dir=str(tmp_path))
    os.makedirs(reg._backups_dir("pia"), exist_ok=True)
    for entry in (base, collision):
        _write_backup(os.path.join(reg._backups_dir("pia"), entry.name), b"x")
    listed = reg.list_backups("pia")
    assert [e["filename"] for e in listed] == [collision.name, base.name]
    # Nothing was opened: no collection handles to close.


# ---------------------------------------------------------------------------
# F-08: sync_now serialization, data payload, pre-tool cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_key_syncs_invoke_sync_user_once(env, monkeypatch):
    """Two concurrent sync_now calls sharing one idempotency key: exactly one
    registry.sync_user call. The ledger check → sync → record sequence runs
    under the per-user mutation lock, so the second caller — whose ledger
    check lands while the first's sync is still in flight — replays the
    committed record instead of double-syncing."""
    reg, _store, manager = env
    counter = {"n": 0}

    async def slow_sync_user(username, email, password, endpoint=None, on_step=None,
                             full_sync_direction=None, acquire_timeout=None):
        counter["n"] += 1
        # Pure event-loop yield (not a timed wait): the second caller's ledger
        # check now provably lands while the first sync is still in flight.
        await asyncio.sleep(0)
        return {"success": True, "output": f"out-{counter['n']}"}

    monkeypatch.setattr(reg, "sync_user", slow_sync_user)

    results = await asyncio.gather(
        manager.sync_now("alice", idempotency_key="race-key"),
        manager.sync_now("alice", idempotency_key="race-key"),
    )

    assert counter["n"] == 1
    committed = [
        r for r in results
        if not any("Idempotent operation replay" in w for w in r["warnings"])
    ]
    replayed = [
        r for r in results
        if any("Idempotent operation replay" in w for w in r["warnings"])
    ]
    assert len(committed) == 1
    assert len(replayed) == 1
    assert committed[0]["success"] is True
    assert replayed[0]["success"] is True
    assert replayed[0]["data"] == committed[0]["data"]


@pytest.mark.asyncio
async def test_fresh_sync_response_carries_the_recorded_data_payload(env, monkeypatch):
    """A fresh success includes the ledger-recorded data payload — the same
    object an idempotent replay returns (the spec gate's "data.last_sync"
    payload; the recorded shape is the sync output under ``output``) — so
    fresh and replayed responses carry an identical key set."""
    reg, _store, manager = env
    calls = _patch_sync_user(monkeypatch, reg, {"success": True, "output": "ok"})

    fresh = await manager.sync_now("alice", idempotency_key="payload-key")

    assert fresh["success"] is True
    assert calls and calls[0]["username"] == "alice"
    record = await reg.ledger("alice").get("payload-key")
    assert record["status"] == "committed"
    assert fresh["data"] == record["data"] == {"output": "ok"}
    assert fresh["warnings"] == []  # a fresh success carries no replay warning

    replay = await manager.sync_now("alice", idempotency_key="payload-key")
    assert len(calls) == 1  # replay did not sync again
    assert replay["data"] == fresh["data"]
    assert set(fresh) == set(replay)


@pytest.mark.asyncio
async def test_successful_sync_now_refreshes_pretool_sync_timestamp(env, monkeypatch):
    """A successful explicit sync stamps the pre-tool cooldown: the next tool
    call inside the TTL window must not pay a redundant pre-tool sync."""
    reg, _store, manager = env
    monkeypatch.setattr(manager, "settings", _settings(ttl=60))
    calls = _patch_sync_user(monkeypatch, reg, {"success": True, "output": "ok"})

    await manager.sync_now("alice", idempotency_key="stamp-key")

    assert "alice" in manager._last_pretool_sync  # the cooldown stamp exists
    await manager.sync_before_tool("alice")
    assert len(calls) == 1  # only sync_now ran; the pre-tool sync was throttled


@pytest.mark.asyncio
async def test_invalidate_pretool_ttl_clears_cooldown(env, monkeypatch):
    """record_explicit_sync sets the cooldown stamp; invalidate_pretool_ttl
    clears it so the next pre-tool sync runs again inside the window."""
    reg, _store, manager = env
    monkeypatch.setattr(manager, "settings", _settings(ttl=60))
    calls = _patch_sync_user(monkeypatch, reg, {"success": True, "output": "ok"})

    manager.record_explicit_sync("alice")
    await manager.sync_before_tool("alice")
    assert len(calls) == 0  # throttled by the explicit-sync stamp

    manager.invalidate_pretool_ttl("alice")
    assert "alice" not in manager._last_pretool_sync  # stamp gone
    await manager.sync_before_tool("alice")
    assert len(calls) == 1  # the pre-tool sync ran again
