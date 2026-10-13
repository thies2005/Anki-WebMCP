"""Note searching, retrieval, creation, updating, and note-type changing tools.

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
from typing import Any, Dict, List, Optional, Tuple

import anki.lang
from anki.consts import MODEL_CLOZE
from anki.errors import NotFoundError
from anki.notes import Note

from ..errors import CollectionToolError
from ..models import MutationResponse, NoteInput, SyncStatus
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.notes")

# Every other tool here is a pure backend call, so the service never needed
# the wheel's Python-side i18n — but find_dupes is implemented in Python and
# routes its HTML stripping through the module-global ``anki.lang.current_i18n``
# (None unless anki.lang.set_lang ran; probed on the wheel: find_dupes raises
# ``AttributeError`` without it). Initialize once at import, idempotently.
if anki.lang.current_i18n is None:
    anki.lang.set_lang("en")


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

    # Client-controlled values are logged %r (repr escapes newlines/control
    # chars) so one tool call can never author multiple log lines (CWE-117).
    logger.info("Executing search_notes for %r: query=%r, limit=%d, offset=%d", username, query, limit, offset)

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
    # Pass 1: resolve models, validate field names, and run duplicate detection
    # for the WHOLE batch up front (same all-or-nothing reporting as the legacy
    # canAddNotes gate). Field-name validation (mirrors update_note_fields)
    # happens here — BEFORE any write — so a mistyped field name can never fail
    # mid-pass-2 after earlier notes of the batch were already persisted (a
    # backend KeyError raised there would leave partial state behind a 'failed'
    # ledger record).
    resolved = []
    failures: List[int] = []
    # Intra-batch duplicate keys: (note type, deck, first field name, stripped
    # first-field value). The pre-existing-collection check below only sees
    # notes already IN the collection, so two identical notes inside ONE batch
    # would both pass it and be silently created; the seen-set closes that gap.
    seen: set = set()
    for idx, n in enumerate(notes):
        model = col.models.by_name(n.note_type)
        if model is None:
            available = sorted(m.name for m in col.models.all_names_and_ids())
            raise CollectionToolError(
                f"Unknown note type {n.note_type!r}. Available models: {available}"
            )
        valid_fields = sorted(f["name"] for f in model["flds"])
        unknown_fields = sorted(name for name in n.fields if name not in valid_fields)
        if unknown_fields:
            raise CollectionToolError(
                f"Note index {idx}: unknown field(s) {unknown_fields} for note type "
                f"{n.note_type!r}. Valid fields for this note type: {valid_fields}."
            )
        # Mirror _is_first_field_duplicate's key shape, including its empty-
        # value early return: notes whose stripped first field is empty are
        # exempt, so two blank-first-field notes are not intra-batch duplicates.
        try:
            first_field_name = model["flds"][0]["name"]
        except (KeyError, IndexError, TypeError):
            first_field_name = None
        if first_field_name is not None:
            value = (n.fields.get(first_field_name) or "").strip()
            if value:
                key = (n.note_type, n.deck_name, first_field_name, value)
                if not allow_duplicates and key in seen:
                    failures.append(idx)
                # Recorded even when the gate is off, so the set stays warm;
                # it is only consulted when allow_duplicates is False.
                seen.add(key)
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
        "Executing add_notes for %r with %d notes (idempotency_key=%r)",
        username, len(notes), idempotency_key[:64] if idempotency_key else idempotency_key,
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


async def delete_notes(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_ids: List[int],
    confirm: bool = False,
    idempotency_key: Optional[str] = None,
) -> MutationResponse:
    """Delete existing notes after a fail-closed pre-delete-notes snapshot.

    Missing IDs never fail the batch: only the notes that exist are removed
    and the response reports the missing ones. A request whose IDs are ALL
    unknown is a domain error (nothing to delete, no snapshot, no ledger
    commit).
    """
    if not confirm:
        logger.info("delete_notes for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently deletes notes and their cards; pass confirm=true "
            "to proceed (a pre-delete-notes snapshot backup is taken first).",
        )
    logger.info("Executing delete_notes for %r over %d note(s)", username, len(note_ids))

    def _delete(col) -> Dict[str, Any]:
        existing = []
        missing = []
        for nid in note_ids:
            (existing if _get_note_or_none(col, nid) is not None else missing).append(nid)
        if not existing:
            raise CollectionToolError(f"Cannot delete notes: none of the requested IDs exist: {missing}")
        # Fail-closed snapshot FIRST: if it raises, nothing is deleted.
        registry.snapshot_locked(col, username, "pre-delete-notes")
        col.remove_notes(existing)
        return {"requested": len(note_ids), "removed": len(existing), "missing": missing}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _delete)

    return await sync_manager.execute_mutation(
        username,
        operation_name="delete_notes",
        mutation_fn=_mutation,
        idempotency_key=idempotency_key,
    )


async def remove_tags(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_ids: List[int],
    tags: List[str],
) -> MutationResponse:
    """Remove tags from one or more notes with existence pre-verification."""
    logger.info("Executing remove_tags for %r over %d notes: %s", username, len(note_ids), tags)
    tags_string = " ".join(tags)

    def _untag(col) -> Dict[str, Any]:
        # Verify note existence up front: bulk_remove silently ignores
        # unknown IDs, which would otherwise report a successful no-op.
        missing_ids = [nid for nid in note_ids if _get_note_or_none(col, nid) is None]
        if missing_ids:
            raise CollectionToolError(f"Cannot remove tags: note(s) not found: {missing_ids}")
        col.tags.bulk_remove(note_ids, tags_string)
        return {
            "note_ids": note_ids,
            "tags_removed": tags,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _untag)

    return await sync_manager.execute_mutation(
        username,
        operation_name="remove_tags",
        mutation_fn=_mutation,
    )


async def list_note_types(registry, username: str) -> Dict[str, Any]:
    """List note types with their field names and template names."""
    logger.info("Executing list_note_types for %r", username)

    def _list(col) -> Dict[str, Any]:
        models = []
        for entry in col.models.all_names_and_ids():
            # all_names_and_ids carries only name+id; the notetype DICT (with
            # flds/tmpls) comes from the by-name lookup (verified wheel API).
            nt = col.models.by_name(entry.name)
            models.append(
                {
                    "name": entry.name,
                    "id": int(entry.id),
                    "fields": list(col.models.field_names(nt)),
                    "templates": [t["name"] for t in nt["tmpls"]],
                }
            )
        return {"count": len(models), "note_types": models}

    return await registry.run(username, _list)


async def create_note_type(
    sync_manager: SyncManager,
    registry,
    username: str,
    name: str,
    fields: List[str],
    templates: Optional[List[Any]] = None,
) -> MutationResponse:
    """Create a new note type; refuses an existing name.

    Not destructive, so no snapshot. With ``templates=None`` a single default
    template over the first two fields is created (which requires >= 2
    fields); explicit templates are used verbatim.
    """
    logger.info("Executing create_note_type for %r: %r (%d field(s))", username, name, len(fields))
    if templates is None and len(fields) < 2:
        raise CollectionToolError(
            "The default template renders the first two fields; provide at least 2 fields "
            "or pass explicit templates."
        )

    def _create(col) -> Dict[str, Any]:
        if col.models.id_for_name(name) is not None:
            raise CollectionToolError(f"Cannot create note type {name!r}: note type already exists.")
        mm = col.models
        nt = mm.new(name=name)
        for field_name in fields:
            mm.add_field(nt, mm.new_field(field_name))
        if templates is None:
            template = mm.new_template("Card 1")
            template["qfmt"] = "{{" + fields[0] + "}}"
            template["afmt"] = "{{FrontSide}}\n<hr id=answer>\n{{" + fields[1] + "}}"
            mm.add_template(nt, template)
        else:
            for t in templates:
                template = mm.new_template(t.name)
                template["qfmt"] = t.front
                template["afmt"] = t.back
                mm.add_template(nt, template)
        mm.add(nt)
        # mm.add assigns the final id; resolve it from the registry of names
        # rather than trusting the in-memory dict's id field.
        return {
            "name": name,
            "id": int(mm.id_for_name(name)),
            "fields": list(fields),
            "template_count": len(templates) if templates is not None else 1,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _create)

    return await sync_manager.execute_mutation(
        username,
        operation_name="create_note_type",
        mutation_fn=_mutation,
    )


# -- note-type changing -------------------------------------------------------------


# Chunk size for the query path's IN-queries: find_notes results are
# unbounded and SQLite caps host parameters (999 on old builds), so the
# selection is checked in bounded batches — still one indexed pass, never a
# per-ID get_note.
_QUERY_CHUNK = 500


def _raise_if_selection_is_mixed(col, mids: set) -> None:
    """Refuse a selection spanning several note types (both selection paths).

    Python-side, BEFORE any backend call: a mixed selection makes
    ``get_single_notetype_of_notes`` raise ``InvalidInput`` with EMPTY args —
    the exact signature ``CollectionRegistry._is_stale`` reads as a closed
    handle (it would close/reopen the collection and re-run the closure once)
    — and the empty message tells the caller nothing.
    """
    if len(mids) <= 1:
        return
    names = sorted(col.models.get(mid)["name"] for mid in mids)
    raise CollectionToolError(
        f"Selection spans multiple note types ({', '.join(names)}); "
        "change each note type in a separate call."
    )


def _resolve_note_selection(
    col,
    note_ids: Optional[List[int]],
    query: Optional[str],
    missing_prefix: str,
) -> Tuple[List[int], set]:
    """Resolve a note_ids-XOR-query selection to deduplicated note IDs.

    Shared by change_note_type (which adds the one-note-type rule on top) and
    find_and_replace (which allows a selection spanning note types). Both
    branches verify existence BEFORE any backend mutation: a bad selection
    must fail the request in Python, never as a backend error (the registry
    would mistake an ``InvalidInput`` for a stale handle and re-run the
    closure once). Returns ``(nids, mids)`` — the note-type ids collected
    along the way, so a caller enforcing a per-type rule never re-queries the
    notes table.
    """
    if query is not None:
        # Client-controlled search string: %r-escape in errors/logs (CWE-117).
        nids = list(col.find_notes(query))
        if not nids:
            raise CollectionToolError(f"No notes matched query {query!r}; nothing to change.")
        # Collect note.mid in bounded IN-chunks (SQLite caps host parameters,
        # and there is no per-ID get_note for large query results).
        mids: set = set()
        for start in range(0, len(nids), _QUERY_CHUNK):
            chunk = nids[start : start + _QUERY_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            mids.update(
                col.db.list(f"SELECT DISTINCT mid FROM notes WHERE id IN ({placeholders})", *chunk)
            )
        return nids, mids
    # Dedupe preserving order: a repeated ID would inflate the changed count
    # and reappear twice in reports.
    nids = list(dict.fromkeys(int(nid) for nid in note_ids))
    placeholders = ",".join("?" * len(nids))
    rows = col.db.all(f"SELECT id, mid FROM notes WHERE id IN ({placeholders})", *nids)
    found = {row[0]: row[1] for row in rows}
    missing = [nid for nid in nids if nid not in found]
    if missing:
        raise CollectionToolError(f"{missing_prefix}: note(s) not found: {missing}")
    return nids, set(found.values())


def _resolve_change_selection(col, note_ids: Optional[List[int]], query: Optional[str]) -> List[int]:
    """Resolve the selection to deduplicated note IDs of ONE note type.

    ``_resolve_note_selection`` does the shared resolution; both of its
    branches are checked against the one-note-type rule via
    ``_raise_if_selection_is_mixed`` before returning here.
    """
    nids, mids = _resolve_note_selection(col, note_ids, query, "Cannot change note type")
    _raise_if_selection_is_mixed(col, mids)
    return nids


def _resolve_change_maps(
    col,
    old_ntid,
    new_note_type: str,
    note_ids: List[int],
    field_map: Optional[Dict[str, Optional[str]]],
    template_map: Optional[Dict[str, Optional[str]]],
):
    """Validate both maps and build the backend request (shared by preview/mutation).

    Returns ``(request, old_name, new_name, is_cloze, resolved_field_map,
    resolved_template_map, warnings)``. ``request`` is the wheel's prefilled
    ``ChangeNotetypeRequest`` with the selection extended and both maps
    applied; fetching it fresh per call keeps ``current_schema`` current at
    apply time. All validation raises ``CollectionToolError`` BEFORE anything
    is written — the registry would mistake a backend ``InvalidInput`` for a
    stale handle and re-run the whole closure once.

    Map semantics (wheel ``change_notetype_of_notes``): ``new_fields`` /
    ``new_templates`` are lists indexed by NEW position, each value an index
    into the OLD type (-1 discards). With cloze on either side the request
    carries ``is_cloze`` and an EMPTY template list, so a template_map is
    refused outright.
    """
    new_nt = col.models.by_name(new_note_type)
    if new_nt is None:
        available = sorted(m.name for m in col.models.all_names_and_ids())
        raise CollectionToolError(
            f"Unknown note type {new_note_type!r}. Available models: {available}"
        )
    info = col.models.change_notetype_info(old_notetype_id=old_ntid, new_notetype_id=new_nt["id"])
    old_nt = col.models.get(old_ntid)
    old_name = info.old_notetype_name
    new_name = new_nt["name"]
    is_cloze = old_nt["type"] == MODEL_CLOZE or new_nt["type"] == MODEL_CLOZE

    old_fields = list(info.old_field_names)
    new_fields = list(info.new_field_names)
    old_tmpls = [t["name"] for t in old_nt["tmpls"]]
    new_tmpls = [t["name"] for t in new_nt["tmpls"]]

    def _map_side(what, mapping, new_names, old_names, default_ords):
        """Turn name->name-or-None into NEW-position -> OLD-index (-1 discards).

        ``mapping=None`` keeps the backend's positional default; an explicit
        mapping must name every key/value in the right type and never target
        one old name twice (the desktop dialog forbids duplicates too — both
        sources would collapse into one field).
        """
        if mapping is None:
            resolved = {}
            for pos, old_ord in enumerate(default_ords):
                resolved[new_names[pos]] = old_names[old_ord] if old_ord >= 0 else None
            return list(default_ords), resolved
        bad_keys = sorted(k for k in mapping if k not in new_names)
        if bad_keys:
            raise CollectionToolError(
                f"Unknown {what} name(s) {bad_keys} of the NEW note type {new_name!r}. "
                f"Valid {what} names for {new_name!r}: {sorted(new_names)}."
            )
        bad_vals = sorted(
            v for v in mapping.values() if v is not None and v not in old_names
        )
        if bad_vals:
            raise CollectionToolError(
                f"Unknown {what} name(s) {bad_vals} of the OLD note type {old_name!r}. "
                f"Valid {what} names for {old_name!r}: {sorted(old_names)} (use null to discard)."
            )
        targets = [v for v in mapping.values() if v is not None]
        duplicated = sorted({v for v in targets if targets.count(v) > 1})
        if duplicated:
            raise CollectionToolError(
                f"{what}_map maps multiple NEW {what}s to the same OLD "
                f"{what} {duplicated}; each old {what} may feed at most one new "
                f"{what} (mapping two onto one would silently destroy content)."
            )
        ords = []
        resolved = {}
        for pos, name in enumerate(new_names):
            target = mapping.get(name)
            resolved[name] = target
            ords.append(old_names.index(target) if target is not None else -1)
        return ords, resolved

    field_ords, resolved_fields = _map_side(
        "field", field_map, new_fields, old_fields, list(info.input.new_fields)
    )
    if is_cloze:
        if template_map:
            raise CollectionToolError(
                "template_map is not supported when either note type is a Cloze type: "
                "cloze cards all render through the single cloze template, so the "
                "wheel requires an empty template list."
            )
        template_ords = []
        resolved_tmpls = {}
    else:
        template_ords, resolved_tmpls = _map_side(
            "template", template_map, new_tmpls, old_tmpls, list(info.input.new_templates)
        )

    warnings = []
    discarded_fields = [f for f in old_fields if f not in set(resolved_fields.values()) - {None}]
    for name in discarded_fields:
        warnings.append(
            f"Field {name!r} of {old_name!r} is not mapped: its content is discarded in every changed note."
        )
    if not is_cloze:
        for name in old_tmpls:
            if name not in set(resolved_tmpls.values()) - {None}:
                warnings.append(
                    f"Template {name!r} of {old_name!r} has no mapping: cards generated from it are deleted."
                )

    request = info.input
    request.note_ids.extend(note_ids)
    request.new_fields[:] = field_ords
    request.new_templates[:] = template_ords
    return request, old_name, new_name, is_cloze, resolved_fields, resolved_tmpls, warnings


async def preview_note_type_change(
    registry,
    username: str,
    new_note_type: str,
    note_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
    field_map: Optional[Dict[str, Optional[str]]] = None,
    template_map: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    """Preview a note-type change (read-only): resolved maps and discard warnings.

    The exact resolver ``change_note_type`` runs, so what the preview shows is
    what the mutation would do — including which old content is discarded.
    Explicit maps (when given) resolve through the same resolver the mutation
    passes them to; without them the backend's default mapping is shown.
    """
    field_map = field_map or None
    template_map = template_map or None
    logger.info(
        "Executing preview_note_type_change for %r: new_note_type=%r (%d note id(s), query=%r)",
        username, new_note_type, len(note_ids) if note_ids else 0, query,
    )

    def _preview(col) -> Dict[str, Any]:
        nids = _resolve_change_selection(col, note_ids, query)
        # The maps given here (None when omitted) are exactly the ones
        # change_note_type would receive for the same arguments, so the
        # preview shows the change AS THE MUTATION WOULD APPLY IT.
        _request, old_name, new_name, is_cloze, fields, tmpls, warnings = _resolve_change_maps(
            col, col.models.get_single_notetype_of_notes(nids),
            new_note_type, nids, field_map, template_map,
        )
        return {
            "old_note_type": old_name,
            "new_note_type": new_name,
            "is_cloze": is_cloze,
            "note_count": len(nids),
            "field_map": fields,
            "template_map": tmpls,
            "warnings": warnings,
        }

    return await registry.run(username, _preview)


async def change_note_type(
    sync_manager: SyncManager,
    registry,
    username: str,
    new_note_type: str,
    note_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
    field_map: Optional[Dict[str, Optional[str]]] = None,
    template_map: Optional[Dict[str, Optional[str]]] = None,
    confirm: bool = False,
    idempotency_key: Optional[str] = None,
) -> MutationResponse:
    """Change the note type of selected notes after a fail-closed pre-mutation snapshot.

    Unmapped old content is discarded and cards regenerate on the new note
    type (cards of unmapped old templates are deleted), so this sits behind
    the confirm gate like delete_notes. Validation runs before the snapshot:
    a bad map must fail the request without ever reaching the backend.
    """
    field_map = field_map or None
    template_map = template_map or None
    if not confirm:
        logger.info("change_note_type for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently changes the note type of the selected notes, "
            "discarding content that is not mapped onto the new type; run "
            "preview_note_type_change first, then pass confirm=true to proceed "
            "(a pre-change-note-type snapshot backup is taken first).",
        )
    logger.info(
        "Executing change_note_type for %r: new_note_type=%r (%d note id(s), query=%r)",
        username, new_note_type, len(note_ids) if note_ids else 0, query,
    )

    def _change(col) -> Dict[str, Any]:
        # Re-resolve and validate INSIDE the closure: the schema may have moved
        # since the request was built, and current_schema must be current at
        # apply time (info is fetched fresh here).
        nids = _resolve_change_selection(col, note_ids, query)
        request, old_name, new_name, is_cloze, fields, tmpls, _warnings = _resolve_change_maps(
            col, col.models.get_single_notetype_of_notes(nids),
            new_note_type, nids, field_map, template_map,
        )
        # Fail-closed snapshot FIRST: if it raises, nothing is changed.
        registry.snapshot_locked(col, username, "pre-change-note-type")
        col.models.change_notetype_of_notes(request)
        return {
            "old_note_type": old_name,
            "new_note_type": new_name,
            "is_cloze": is_cloze,
            "changed": len(nids),
            "field_map": fields,
            "template_map": tmpls,
            "snapshot_reason": "pre-change-note-type",
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _change)

    return await sync_manager.execute_mutation(
        username,
        operation_name="change_note_type",
        mutation_fn=_mutation,
        idempotency_key=idempotency_key,
    )


# -- mass editing -------------------------------------------------------------------


async def find_and_replace(
    sync_manager: SyncManager,
    registry,
    username: str,
    search: str,
    replacement: str,
    note_ids: Optional[List[int]] = None,
    query: Optional[str] = None,
    regex: bool = False,
    field_name: Optional[str] = None,
    match_case: bool = False,
) -> MutationResponse:
    """Bulk find-and-replace over the selected notes' fields.

    Same class as update_note_fields: a plain content edit of notes the user
    already owns, reversible through the portal's ordinary snapshot/restore
    machinery, so — unlike the structural, content-discarding change_note_type
    — no confirm gate and no dedicated snapshot precede it. The selection may
    span note types, and ``field_name`` is therefore passed to the backend
    unvalidated (a selection-wide name check is impossible; an unknown name
    simply matches nothing).
    """
    logger.info(
        "Executing find_and_replace for %r: search=%r, replacement=%r, regex=%r, "
        "field_name=%r, match_case=%r (%d note id(s), query=%r)",
        username, search, replacement, regex, field_name, match_case,
        len(note_ids) if note_ids else 0, query,
    )

    def _replace(col) -> Dict[str, Any]:
        # Existence + dedupe only (shared resolver): mixed note types are the
        # point of a mass edit, so no single-notetype check here.
        nids, _mids = _resolve_note_selection(col, note_ids, query, "Cannot find and replace")
        # One backend operation over the whole selection; ``count`` is the
        # number of CHANGED notes (verified wheel API), so reporting the
        # selection size next to it keeps a zero-match no-op visible as
        # changed=0 rather than looking like a lost request.
        result = col.find_and_replace(
            note_ids=nids,
            search=search,
            replacement=replacement,
            regex=regex,
            field_name=field_name,
            match_case=match_case,
        )
        return {
            "changed": int(result.count),
            "selected": len(nids),
            "field_name": field_name,
            "regex": regex,
            "match_case": match_case,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _replace)

    return await sync_manager.execute_mutation(
        username,
        operation_name="find_and_replace",
        mutation_fn=_mutation,
    )


async def find_duplicates(
    registry,
    username: str,
    field_name: str,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Report notes sharing a value in ``field_name`` (read-only duplicate report).

    The wheel's ``find_dupes`` matches the field name case-insensitively
    across note types, compares HTML-stripped values, and skips empty values;
    groups come back as ``(value, [note_ids])`` with the full member list.
    ``search`` is an optional Anki query prefilter narrowing which notes are
    examined.
    """
    # Client-controlled values are logged %r (repr escapes newlines/control
    # chars) so one tool call can never author multiple log lines (CWE-117).
    logger.info(
        "Executing find_duplicates for %r: field_name=%r, search=%r, limit=%d, offset=%d",
        username, field_name, search, limit, offset,
    )

    def _dupes(col) -> Dict[str, Any]:
        groups = col.find_dupes(field_name, search)
        return {
            "total_groups": len(groups),
            "duplicates": [
                {"field_value": value, "note_ids": [int(nid) for nid in nids]}
                for value, nids in groups[offset : offset + limit]
            ],
        }

    return await registry.run(username, _dupes)
