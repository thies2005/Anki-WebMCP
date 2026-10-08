"""Note searching, retrieval, creation, and updating tools."""

import logging
from typing import Any, Dict, List, Optional
from ..anki_client import AnkiClient, AnkiConnectError
from ..models import MutationResponse, NoteInput
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.notes")


async def search_notes(
    client: AnkiClient,
    query: str,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search notes in the collection with bounded pagination."""
    # Clamp pagination bounds
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    logger.info("Executing search_notes: query='%s', limit=%d, offset=%d", query, limit, offset)
    note_ids = await client.invoke("findNotes", {"query": query}) or []
    total = len(note_ids)

    # Slice IDs for bounded response
    sliced_ids = note_ids[offset : offset + limit]
    if not sliced_ids:
        return {
            "total_matches": total,
            "returned_count": 0,
            "offset": offset,
            "limit": limit,
            "notes": [],
        }

    raw_notes = await client.invoke("notesInfo", {"notes": sliced_ids}) or []
    notes: List[Dict[str, Any]] = []
    for raw in raw_notes:
        if not isinstance(raw, dict) or not raw:
            # AnkiConnect returns {} for IDs that vanished between findNotes and notesInfo
            continue
        fields_dict = {
            f_name: f_info.get("value", "")
            for f_name, f_info in raw.get("fields", {}).items()
            if isinstance(f_info, dict)
        }
        notes.append({
            "note_id": raw.get("noteId"),
            "model_name": raw.get("modelName"),
            "tags": raw.get("tags", []),
            "fields": fields_dict,
        })

    return {
        "total_matches": total,
        "returned_count": len(notes),
        "offset": offset,
        "limit": limit,
        "notes": notes,
    }


async def get_note(client: AnkiClient, note_id: int) -> Dict[str, Any]:
    """Retrieve full details of a specific note by its note ID."""
    logger.info("Executing get_note for id %d", note_id)
    raw_notes = await client.invoke("notesInfo", {"notes": [note_id]})
    if not raw_notes or not isinstance(raw_notes, list) or not raw_notes[0] or not isinstance(raw_notes[0], dict):
        raise AnkiConnectError(f"Note with ID {note_id} not found in collection.")

    note_data = raw_notes[0]
    fields_dict = {
        f_name: f_info.get("value", "")
        for f_name, f_info in note_data.get("fields", {}).items()
        if isinstance(f_info, dict)
    }

    return {
        "note_id": note_data.get("noteId"),
        "model_name": note_data.get("modelName"),
        "tags": note_data.get("tags", []),
        "fields": fields_dict,
    }


async def add_notes(
    sync_manager: SyncManager,
    notes: List[NoteInput],
    idempotency_key: Optional[str] = None,
    allow_duplicates: bool = False,
) -> MutationResponse:
    """Create one or more notes under write-safety lock with duplicate prevention."""
    if not notes:
        raise AnkiConnectError("At least one note must be provided.")
    logger.info("Executing add_notes with %d notes (idempotency_key=%s)", len(notes), idempotency_key)

    formatted_notes = [
        {
            "deckName": n.deck_name,
            "modelName": n.model_name,
            "fields": n.fields,
            "tags": n.tags,
            "options": {
                "allowDuplicate": allow_duplicates,
                "duplicateScope": "deck",
            },
        }
        for n in notes
    ]

    async def _apply_add_notes() -> Dict[str, Any]:
        # Pre-check duplicates if not allowed
        if not allow_duplicates:
            can_add = await sync_manager.client.invoke("canAddNotes", {"notes": formatted_notes})
            if isinstance(can_add, list):
                failures = [i for i, ok in enumerate(can_add) if not ok]
                if failures:
                    raise AnkiConnectError(
                        f"Duplicate check failed: note index(es) {failures} would create duplicate cards. "
                        "Set allow_duplicates=True if duplicates are intended."
                    )

        created_ids = await sync_manager.client.invoke("addNotes", {"notes": formatted_notes})
        return {
            "created_note_ids": created_ids,
            "count": len(created_ids) if isinstance(created_ids, list) else 0,
        }

    return await sync_manager.execute_mutation(
        operation_name="add_notes",
        mutation_fn=_apply_add_notes,
        idempotency_key=idempotency_key,
    )


async def update_note_fields(
    sync_manager: SyncManager,
    note_id: int,
    fields: Dict[str, str],
) -> MutationResponse:
    """Update fields of an existing note under write-safety lock."""
    logger.info("Executing update_note_fields for note %d", note_id)

    async def _apply_update() -> Dict[str, Any]:
        # Verify the note exists and all requested fields belong to its model:
        # AnkiConnect silently skips unknown fields, which would otherwise be
        # reported as a successful no-op update.
        existing = await sync_manager.client.invoke("notesInfo", {"notes": [note_id]})
        existing_fields: Dict[str, Any] = {}
        if isinstance(existing, list) and existing and isinstance(existing[0], dict):
            existing_fields = existing[0].get("fields", {})
        if not existing_fields:
            raise AnkiConnectError(f"Note with ID {note_id} not found in collection.")
        unknown_fields = sorted(name for name in fields if name not in existing_fields)
        if unknown_fields:
            raise AnkiConnectError(
                f"Cannot update note {note_id}: unknown field(s) {unknown_fields}. "
                f"Valid fields for this note: {sorted(existing_fields.keys())}."
            )

        await sync_manager.client.invoke("updateNoteFields", {
            "note": {
                "id": note_id,
                "fields": fields,
            }
        })
        return {
            "note_id": note_id,
            "updated_fields": list(fields.keys()),
        }

    return await sync_manager.execute_mutation(
        operation_name="update_note_fields",
        mutation_fn=_apply_update,
    )


async def add_tags(
    sync_manager: SyncManager,
    note_ids: List[int],
    tags: List[str],
) -> MutationResponse:
    """Add tags to one or more notes under write-safety lock."""
    logger.info("Executing add_tags for %d notes: %s", len(note_ids), tags)
    tags_string = " ".join([t.strip() for t in tags if t.strip()])

    async def _apply_tags() -> Dict[str, Any]:
        # Verify note existence up front: AnkiConnect's addTags silently
        # ignores unknown IDs and would otherwise report a successful no-op.
        infos = await sync_manager.client.invoke("notesInfo", {"notes": note_ids})
        found_ids = {
            info.get("noteId")
            for info in (infos or [])
            if isinstance(info, dict) and info
        }
        missing_ids = [nid for nid in note_ids if nid not in found_ids]
        if missing_ids:
            raise AnkiConnectError(f"Cannot add tags: note(s) not found: {missing_ids}")

        await sync_manager.client.invoke("addTags", {
            "notes": note_ids,
            "tags": tags_string,
        })
        return {
            "note_ids": note_ids,
            "tags_added": tags,
        }

    return await sync_manager.execute_mutation(
        operation_name="add_tags",
        mutation_fn=_apply_tags,
    )
