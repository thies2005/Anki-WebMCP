"""Note searching, retrieval, creation, and updating tools.

All operations run against the calling user's collection via the
``CollectionRegistry`` (no AnkiConnect). Search/note reads replicate the
summary shape the old ``notesInfo``-based code produced:
``{"note_id", "model_name", "tags", "fields"}``.

Wheel notes (see .superpowers/sdd/anki-api-facts.md): tags are space-separated
STRINGS at the storage layer while ``note.tags`` is a List[str] property;
``anki.notes`` must never be the FIRST anki import (import-order footgun) —
``anki.errors`` is imported first here, and the app imports
``src.collections`` (anki.collection) before the tools package anyway.
"""

import logging
from typing import Any, Dict, List, Optional

from anki.errors import NotFoundError
from anki.notes import Note

from ..errors import CollectionToolError
from ..models import MutationResponse, NoteInput
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.notes")


def _note_summary(col, note) -> Dict[str, Any]:
    """The notesInfo-equivalent summary shape (schema compatibility bar)."""
    return {
        "note_id": int(note.id),
        "model_name": note.note_type()["name"],
        "tags": list(note.tags),
        "fields": dict(note.items()),
    }


def _get_note_or_none(col, note_id: int):
    try:
        return col.get_note(note_id)
    except NotFoundError:
        return None


def _escape_search_term(value: str) -> str:
    """Escape a value for use inside a double-quoted Anki search term."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def search_notes(
    registry,
    username: str,
    query: str,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search notes in the collection with bounded pagination."""
    # Clamp pagination bounds (direct calls bypass the pydantic input model).
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    logger.info("Executing search_notes for %r: query='%s', limit=%d, offset=%d", username, query, limit, offset)

    def _search(col) -> Dict[str, Any]:
        note_ids = list(col.find_notes(query))
        total = len(note_ids)
        notes: List[Dict[str, Any]] = []
        # Slice offset/limit AFTER the full find (same semantics as before).
        for nid in note_ids[offset : offset + limit]:
            note = _get_note_or_none(col, nid)
            if note is None:
                # The note vanished between find_notes and the read (the
                # phantom-entry case AnkiConnect handled with `{}` entries).
                continue
            notes.append(_note_summary(col, note))
        return {"total": total, "notes": notes}

    result = await registry.run(username, _search)
    return {
        "total_matches": result["total"],
        "returned_count": len(result["notes"]),
        "offset": offset,
        "limit": limit,
        "notes": result["notes"],
    }


async def get_note(registry, username: str, note_id: int) -> Dict[str, Any]:
    """Retrieve full details of a specific note by its note ID."""
    logger.info("Executing get_note for %r, id %d", username, note_id)

    def _get(col):
        note = _get_note_or_none(col, note_id)
        if note is None:
            raise CollectionToolError(f"Note with ID {note_id} not found in collection.")
        return _note_summary(col, note)

    return await registry.run(username, _get)


def _is_first_field_duplicate(col, model, deck_name: str, fields: Dict[str, str]) -> bool:
    """True when an existing note in ``deck_name`` shares the first field value.

    Mirrors the legacy AnkiConnect ``canAddNotes`` check with
    ``duplicateScope: "deck"``: duplicates are judged on the note type's FIRST
    field within the target deck. (Approximation of AnkiConnect's normalized
    comparison: values are compared as exact strings, not HTML-stripped.)
    """
    try:
        first_field_name = model["flds"][0]["name"]
    except (KeyError, IndexError, TypeError):
        return False
    value = (fields.get(first_field_name) or "").strip()
    if not value:
        return False
    # The combined-quoted term "field:value" is the one Anki search form that
    # survives field names AND values containing spaces, quotes, and colons
    # (probed against the wheel: ``Field:"value"`` silently misses when the
    # field name has a space, and ``"Field":"value"`` is a SearchError).
    query = (
        f'deck:"{_escape_search_term(deck_name)}" '
        f'"{_escape_search_term(first_field_name)}:{_escape_search_term(value)}"'
    )
    return len(col.find_notes(query)) > 0


def _add_notes_impl(col, notes: List[NoteInput], allow_duplicates: bool) -> Dict[str, Any]:
    # Pass 1: resolve models and run duplicate detection for the WHOLE batch
    # up front (same all-or-nothing reporting as the legacy canAddNotes gate).
    resolved = []
    failures: List[int] = []
    for idx, n in enumerate(notes):
        model = col.models.by_name(n.model_name)
        if model is None:
            available = sorted(m.name for m in col.models.all_names_and_ids())
            raise CollectionToolError(
                f"Unknown note type {n.model_name!r}. Available models: {available}"
            )
        if not allow_duplicates and _is_first_field_duplicate(col, model, n.deck_name, n.fields):
            failures.append(idx)
        resolved.append(model)
    if failures:
        raise CollectionToolError(
            f"Duplicate check failed: note index(es) {failures} would create duplicate cards. "
            "Set allow_duplicates=True if duplicates are intended."
        )

    # Pass 2: create. col.decks.id() resolves OR CREATES the deck, matching
    # AnkiConnect's deckName behavior.
    created: List[int] = []
    for n, model in zip(notes, resolved):
        deck_id = col.decks.id(n.deck_name)
        note = Note(col, model)
        for field_name, value in n.fields.items():
            note[field_name] = value
        note.tags = list(n.tags)
        col.add_note(note, deck_id)
        created.append(int(note.id))
    return {"created_note_ids": created, "count": len(created)}


async def add_notes(
    sync_manager: SyncManager,
    registry,
    username: str,
    notes: List[NoteInput],
    idempotency_key: Optional[str] = None,
    allow_duplicates: bool = False,
) -> MutationResponse:
    """Create one or more notes with duplicate prevention and per-user idempotency."""
    if not notes:
        raise CollectionToolError("At least one note must be provided.")
    logger.info(
        "Executing add_notes for %r with %d notes (idempotency_key=%s)",
        username, len(notes), idempotency_key,
    )

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _add_notes_impl, notes, allow_duplicates)

    return await sync_manager.execute_mutation(
        username,
        operation_name="add_notes",
        mutation_fn=_mutation,
        idempotency_key=idempotency_key,
    )


async def update_note_fields(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_id: int,
    fields: Dict[str, str],
) -> MutationResponse:
    """Update fields of an existing note with pre-verification."""
    logger.info("Executing update_note_fields for %r, note %d", username, note_id)

    def _update(col) -> Dict[str, Any]:
        note = _get_note_or_none(col, note_id)
        if note is None:
            raise CollectionToolError(f"Note with ID {note_id} not found in collection.")
        # Verify all requested fields belong to the note's model: the wheel
        # would otherwise raise a cryptic backend error (or silently skip on
        # the old AnkiConnect path) for a mistyped field name.
        valid_field_names = sorted(name for name, _ in note.items())
        unknown_fields = sorted(name for name in fields if name not in valid_field_names)
        if unknown_fields:
            raise CollectionToolError(
                f"Cannot update note {note_id}: unknown field(s) {unknown_fields}. "
                f"Valid fields for this note: {valid_field_names}."
            )
        for field_name, value in fields.items():
            note[field_name] = value
        col.update_note(note)
        return {
            "note_id": note_id,
            "updated_fields": list(fields.keys()),
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _update)

    return await sync_manager.execute_mutation(
        username,
        operation_name="update_note_fields",
        mutation_fn=_mutation,
    )


async def add_tags(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_ids: List[int],
    tags: List[str],
) -> MutationResponse:
    """Add tags to one or more notes with existence pre-verification."""
    logger.info("Executing add_tags for %r over %d notes: %s", username, len(note_ids), tags)
    tags_string = " ".join([t.strip() for t in tags if t.strip()])

    def _tag(col) -> Dict[str, Any]:
        # Verify note existence up front: bulk_add silently ignores unknown
        # IDs, which would otherwise report a successful no-op.
        missing_ids = [nid for nid in note_ids if _get_note_or_none(col, nid) is None]
        if missing_ids:
            raise CollectionToolError(f"Cannot add tags: note(s) not found: {missing_ids}")
        # Tags are a single space-separated STRING in this API generation.
        col.tags.bulk_add(note_ids, tags_string)
        return {
            "note_ids": note_ids,
            "tags_added": tags,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _tag)

    return await sync_manager.execute_mutation(
        username,
        operation_name="add_tags",
        mutation_fn=_mutation,
    )
