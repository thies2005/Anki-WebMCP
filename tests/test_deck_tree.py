"""Tests for the portal's collapsible deck tree (version-history stage).

The flat per-deck table was replaced by nested ``<details class="decknode">``
markup: summary rows show the LAST ``::`` path segment only, parent decks show
SUBTREE aggregates (own + all descendants) with a muted ``(own ...)`` hint when
they directly hold cards, top-level details stay collapsed, and the full dotted
names must never render again.
"""

import httpx
import pytest

# src.main pulls anki.collection first — the wheel's anki.notes cannot be the
# FIRST anki module imported (circular import through anki.cards).
from src.config import Settings
from src.main import _deck_section, create_app
from src.users import UserStore
from src.webauth import SESSION_COOKIE_NAME, issue_session

SECRET = "test-session-secret"


def _make_settings(tmp_path) -> Settings:
    users_file = tmp_path / "users.json"
    store = UserStore(str(users_file))
    store.create_user("alice", "correct-horse")
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
    """Replace the jar's session cookie (clearing any server-set one first)."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, issue_session(SECRET, username))


def _stats():
    """Decks A > A::B > A::B::C plus a sibling Z, each with distinct counts."""
    return {
        "decks": [
            {"name": "A", "notes": 1, "cards": 2, "due": 3},
            {"name": "A::B", "notes": 10, "cards": 20, "due": 30},
            {"name": "A::B::C", "notes": 100, "cards": 200, "due": 300},
            {"name": "Z", "notes": 4, "cards": 5, "due": 6},
        ],
        "due": {"new": 7, "learn": 8, "review": 9},
    }


# ---------------------------------------------------------------------------
# _deck_section unit behaviour
# ---------------------------------------------------------------------------


def test_summary_rows_show_last_segment_only():
    out = _deck_section(_stats())
    # Each row is labelled with the LAST path segment only; the full dotted
    # names (which contain "A::B" as a prefix of "A::B::C" too) never render.
    for label in ("A", "B", "C", "Z"):
        assert f'<span class="deckname">{label}</span>' in out
    assert "A::B" not in out


def test_nested_details_structure_and_default_collapsed():
    out = _deck_section(_stats())
    # One <details> per deck: A, A::B, A::B::C, Z.
    assert out.count('<details class="decknode') == 4
    # Leaves (no children): A::B::C and Z; A and A::B are branch nodes.
    assert out.count('decknode leaf') == 2
    # C is nested inside B which is nested inside A (markup ordering).
    assert out.index('deckname">C</span>') > out.index('deckname">B</span>')
    assert out.index('deckname">B</span>') > out.index('deckname">A</span>')
    # Collapsed by default: no <details> carries an open attribute.
    assert "<details open" not in out
    assert out.count("<details") == out.count('<details class="decknode')


def test_parent_shows_subtree_aggregate_plus_own_hint():
    out = _deck_section(_stats())
    # A aggregates itself (1/2/3) + A::B (10/20/30) + A::B::C (100/200/300).
    assert "111 notes &middot; 222 cards &middot; 333 due" in out
    assert "(own 1 notes, 2 cards, 3 due)" in out
    # A::B aggregates itself + A::B::C.
    assert "110 notes &middot; 220 cards &middot; 330 due" in out
    assert "(own 10 notes, 20 cards, 30 due)" in out
    # Leaves show their own numbers with no (own ...) hint.
    assert "100 notes &middot; 200 cards &middot; 300 due" in out
    assert "4 notes &middot; 5 cards &middot; 6 due" in out
    assert out.count("(own ") == 2


def test_totals_line_kept_and_unavailable_fallback():
    out = _deck_section(_stats())
    assert "Due now: 7 new, 8 learning, 9 review." in out
    # Stats failure keeps the section (and the page) alive.
    fallback = _deck_section(None)
    assert "Deck statistics are unavailable right now." in fallback


# ---------------------------------------------------------------------------
# Route level: the real portal page never renders flat full names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_page_renders_tree_not_flat_names(client):
    from src.main import collection_registry

    def make_decks(col):
        for name in ("A", "A::B", "A::B::C", "Z"):
            col.decks.id(name)

    _login(client)
    await collection_registry.run("alice", make_decks)
    res = await client.get("/")
    assert res.status_code == 200
    # Real collection: Default + the four created decks = 5 nodes; leaves are
    # A::B::C, Default and Z. The full dotted names never render.
    assert "A::B" not in res.text
    assert '<span class="deckname">C</span>' in res.text
    assert res.text.count('<details class="decknode') == 5
    assert res.text.count("decknode leaf") == 3
    # The totals line survives the rewrite.
    assert "Due now:" in res.text
