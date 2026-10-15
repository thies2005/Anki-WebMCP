"""Phase 4 verification gate (audit F-16: input validation & tool polish).

Covers exactly the Phase 4 gate scenarios from ``.zcode/remediation-spec.md``
(all six belong to finding F-16):

- ``NoteInput.model_validate`` folds the legacy ``model_name`` alias onto a
  COPY of the caller's dict: afterwards the dict still holds ``model_name``
  and never gains a ``note_type`` key, while validation itself still succeeds
  with the folded note type.
- ``SetDueDateInput`` rejects an inverted range (``"10-5"``, lo > hi) and a
  bound violation (``"999999999"`` > 36500) with ``ValueError`` — through the
  model (pydantic's ``ValidationError`` IS a ValueError subclass) and through
  the bare ``_check_days`` validator the spec names.
- ``find_duplicates`` clamps a NEGATIVE offset to 0 and an excessive limit to
  the 500 cap instead of erroring. Observable with 501 duplicate groups: the
  unclamped slice ``groups[-5 : -5 + 10**9]`` would return the LAST 5 groups,
  the clamp returns the FIRST 500 — both the page length and the page content
  distinguish clamped from unclamped (probed: ``[-5:-5+10**9]`` on a
  501-element list is 5 items).
- ``get_review_queue`` reports ``truncated`` as a real bool: False while every
  queued question is short, True (with the question capped at exactly the
  2000-char limit) once any queued question was cut.
- ``update_note_type`` with template strings identical to the current ones and
  a sort_field that is already the current sort field returns ``changed == []``
  and never calls ``col.models.save``. The save spy is installed on the user's
  CACHED collection instance (``registry.run`` reuses ``state.col``), and a
  guard re-read asserts the spy was actually in place, so the assertion cannot
  pass vacuously after an internal collection reopen.
- ``OAuthStore.issue_code`` bounds the in-memory pending-code map at
  ``MAX_CODES`` (250): issuing 255 codes without redemption never exceeds the
  cap, and every eviction drops the SOONEST-EXPIRING entry — proven by a
  long-lived code pre-seeded FIRST, which survives all six evictions (a
  first-inserted FIFO policy would have dropped it at the very first one)
  while the earliest-issued codes are the victims.

Determinism: everything is in-process — pydantic validation, the established
real per-user collection contract in a tmp data dir, and a real OAuthStore on
a tmp path. No sleeps and no network; no scenario needs same-second sequencing
or monkeypatched time (the eviction orders entries by their ``expires_at``
field, which the pre-seeded long-lived code makes strictly distinct from every
issued code's ``now + CODE_TTL_SECONDS``).
"""

import time

import pytest
import pytest_asyncio

# src.collections imports anki.collection first — the wheel's anki.notes
# cannot be the FIRST anki module imported (circular import through anki.cards).
from src.collections import CollectionRegistry  # isort: skip
from anki.notes import Note

from src.models import NoteInput, SetDueDateInput, TemplateUpdateInput
from src.oauth import OAuthStore
from src.sync_manager import SyncManager
from src.tools import cards as card_tools
from src.tools import notes as note_tools
from src.tools import notetypes as notetype_tools
from src.users import UserStore


def _add_basic_note(col, front, back="back", deck="Default"):
    note = Note(col, col.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    col.add_note(note, col.decks.id(deck))
    return int(note.id)


@pytest_asyncio.fixture
async def env(tmp_path):
    """Registry + sync manager over a shared tmp data dir (established
    contract: real per-user collections, empty user store)."""
    reg = CollectionRegistry(data_dir=str(tmp_path))
    manager = SyncManager(reg, UserStore(str(tmp_path / "users.json")))
    yield reg, manager
    await reg.close_all()


# ---------------------------------------------------------------------------
# F-16.1: NoteInput.model_validate must not mutate the caller's dict
# ---------------------------------------------------------------------------


def test_note_input_model_validate_does_not_mutate_caller_dict():
    """The legacy ``model_name`` alias is folded onto a COPY: the caller's dict
    keeps the alias key it passed, gains no canonical key, and validation still
    resolves the note type."""
    raw = {
        "deck_name": "Default",
        "model_name": "Basic",
        "fields": {"Front": "q", "Back": "a"},
        "tags": ["audit"],
    }
    snapshot = dict(raw)

    note = NoteInput.model_validate(raw)

    assert raw == snapshot, "validation must not mutate the caller's dict"
    assert "model_name" in raw
    assert "note_type" not in raw
    # The copy still did the alias folding the validator exists for.
    assert note.note_type == "Basic"
    assert note.deck_name == "Default"
    assert note.fields == {"Front": "q", "Back": "a"}
    assert note.tags == ["audit"]


# ---------------------------------------------------------------------------
# F-16.2: SetDueDateInput rejects inverted and overbound day ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", ["10-5", "999999999"], ids=["inverted-range", "over-36500"])
def test_set_due_date_input_rejects_inverted_and_overbound_ranges(days):
    """Both malformed ``days`` values are refused with ValueError before any
    snapshot is taken: through the model (pydantic's ValidationError IS a
    ValueError subclass) and through the bare validator function."""
    with pytest.raises(ValueError) as exc:
        SetDueDateInput.model_validate({"card_ids": [1], "days": days})
    if days == "10-5":
        assert "inverted" in str(exc.value)
    else:
        assert "36500" in str(exc.value)

    # The spec-named validator itself raises the bare ValueError.
    with pytest.raises(ValueError):
        SetDueDateInput._check_days(days)


# ---------------------------------------------------------------------------
# F-16.3: find_duplicates clamps negative offset and excessive limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_duplicates_clamps_negative_offset_and_excessive_limit(env):
    """Direct calls bypass the pydantic bounds, so the tool itself must clamp:
    offset=-5 behaves exactly like offset=0 and limit=10**9 is cut to the 500
    cap. With 501 duplicate groups BOTH clamps are observable — the unclamped
    slice would return the last 5 groups (probe: [-5:-5+10**9] on 501 items is
    5 items) instead of the first 500."""
    reg, _manager = env
    group_count = 501  # one more than the cap: the excess must be cut off

    def _seed(col):
        for i in range(group_count):
            value = f"dup-{i:04d}"
            for _copy in range(2):  # a duplicate group needs a twin
                _add_basic_note(col, value)

    await reg.run("xaver", _seed)

    res = await note_tools.find_duplicates(
        reg, "xaver", "Front", limit=10**9, offset=-5
    )
    assert res["total_groups"] == group_count
    # Excessive limit clamped to 500 (unclamped would return all 501).
    assert len(res["duplicates"]) == 500
    # Negative offset clamped to 0: identical to the offset-0 page (unclamped
    # slicing would have returned the LAST groups, a different page entirely).
    zero_page = await note_tools.find_duplicates(
        reg, "xaver", "Front", limit=500, offset=0
    )
    assert res["duplicates"] == zero_page["duplicates"]


# ---------------------------------------------------------------------------
# F-16.4: get_review_queue reports the truncated flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_review_queue_reports_truncated_true_and_false(env):
    """``truncated`` is present and a real bool: False while every queued
    question fits the 2000-char cap, True once one was cut — and the cut
    question comes back at exactly the cap."""
    reg, _manager = env
    await reg.run("yelda", _add_basic_note, "short question")

    short = await card_tools.get_review_queue(reg, "yelda")
    assert short["truncated"] is False

    await reg.run("yelda", _add_basic_note, "L" * 3000)

    queued = await card_tools.get_review_queue(reg, "yelda")
    assert isinstance(queued["truncated"], bool)
    assert queued["truncated"] is True
    cut = [c for c in queued["cards"] if len(c["question"]) == 2000]
    assert cut, "the over-long question must come back capped at exactly 2000 chars"


# ---------------------------------------------------------------------------
# F-16.5: update_note_type with identical edits changes nothing, calls no save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_note_type_identical_edits_report_no_change_and_no_save(env):
    """Templates identical to the current qfmt/afmt and a sort_field that is
    already the current one: ``changed`` stays empty and ``col.models.save``
    is never called. The spy rides on the user's cached collection instance,
    and a guard re-read proves it was installed, so the no-save assertion
    cannot pass vacuously."""
    reg, manager = env
    front_tpl = "{{F1}}"
    back_tpl = "{{FrontSide}}<hr>{{F2}}"

    def _setup(col):
        mm = col.models
        nt = mm.new(name="Idle")
        for field_name in ("F1", "F2"):
            mm.add_field(nt, mm.new_field(field_name))
        template = mm.new_template("Card 1")
        template["qfmt"] = front_tpl
        template["afmt"] = back_tpl
        mm.add_template(nt, template)
        mm.add(nt)  # sortf defaults to 0 = "F1"

    await reg.run("zacharias", _setup)

    save_calls = []

    def _install_spy(col):
        original = col.models.save

        def _spy(notetype=None, **kwargs):
            save_calls.append(notetype["name"] if notetype is not None else None)
            return original(notetype, **kwargs)

        _spy._is_test_save_spy = True
        col.models.save = _spy

    await reg.run("zacharias", _install_spy)

    def _spy_installed(col):
        return getattr(col.models.save, "_is_test_save_spy", False)

    assert (
        await reg.run("zacharias", _spy_installed) is True
    ), "save spy missing from the cached collection — the no-save assertion would be vacuous"

    res = await notetype_tools.update_note_type(
        manager,
        reg,
        "zacharias",
        "Idle",
        template_updates=[
            TemplateUpdateInput(template="Card 1", front=front_tpl, back=back_tpl)
        ],
        sort_field="F1",
    )
    assert res.success is True, res.error
    assert res.data["changed"] == []
    assert save_calls == []


# ---------------------------------------------------------------------------
# F-16.6: OAuthStore bounds pending authorization codes at MAX_CODES
# ---------------------------------------------------------------------------


def test_oauth_store_bounds_pending_codes_evicting_soonest_expiring(tmp_path):
    """A consent retry loop issuing codes that are never redeemed must not
    grow ``_codes`` without limit: after MAX_CODES + 5 issues the map holds
    exactly MAX_CODES entries, and the evictions took the SOONEST-EXPIRING
    codes — the long-lived code pre-seeded FIRST survives them all, while the
    earliest-issued (earliest-expiring) codes are the victims."""
    store = OAuthStore(str(tmp_path / "oauth_state.json"))
    cap = OAuthStore.MAX_CODES
    assert cap == 250

    store._codes["preseeded-long-lived"] = {
        "client_id": "seed-client",
        "redirect_uri": "http://localhost/callback",
        "username": "seed-user",
        "scope": "read",
        "code_challenge": "seed-challenge",
        "created_at": 0.0,
        # Outlives every code issue_code mints (now + CODE_TTL_SECONDS); it
        # must be future-dated or issue_code's expiry purge would drop it
        # before the cap logic ever runs.
        "expires_at": time.time() + 86400.0,
    }

    issued = []
    sizes = []
    for _ in range(cap + 5):
        issued.append(
            store.issue_code(
                "client", "http://localhost/callback", "alice", "read", "challenge"
            )
        )
        sizes.append(len(store._codes))

    assert max(sizes) == cap  # the cap was never exceeded during the burst
    assert len(store._codes) == cap  # bounded after it
    # Soonest-expiring eviction skipped the far-future entry inserted first
    # (FIFO would have dropped it at the very first eviction) ...
    assert "preseeded-long-lived" in store._codes
    # ... and consumed the earliest-issued codes instead: the 6 evictions
    # (255 issued + 1 preseed - 250 cap) took issued[0]..issued[5], the six
    # earliest-expiring codes.
    assert all(code not in store._codes for code in issued[:6])
    assert all(code in store._codes for code in issued[6:])
