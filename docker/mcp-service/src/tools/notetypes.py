"""Note-type control tools: detail, update, field/template operations, delete,
restore-to-stock.

All operations run against the calling user's collection via the
``CollectionRegistry``. Mutations load the notetype dict via
``col.models.by_name``, change it with the wheel's ModelManager helpers, and
persist with ``col.models.update_dict(nt)`` (the backend marks the schema
modified itself). Two wheel behaviors shape this module (both probed on
anki==26.9.3):

* ``update_dict`` runs ``ensure_name_unique``, which would SILENTLY mangle a
  colliding note-type name (``Name-1a2b3``) instead of failing — so renames
  are collision-checked via ``col.models.id_for_name`` first.
* ``restore_notetype_to_stock`` talks to the backend directly and does NOT
  evict the wheel's Python-side notetype cache (only update_dict/remove/close
  do), so it clears the cache itself before the response re-reads state.
"""

import logging
from typing import Any, Dict, List, Optional

from anki.consts import MODEL_CLOZE
from anki.stdmodels import StockNotetypeKind

from ..errors import CollectionToolError
from ..models import MutationResponse, SyncStatus, TemplateInput
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.notetypes")

# force_kind string -> proto enum value, built FROM the wheel's enum so the
# accepted names can never drift from the pinned wheel ("KIND_CLOZE" ->
# "cloze"). Client-controlled strings are logged %r (CWE-117).
STOCK_KIND_NAMES: Dict[str, int] = {
    name.removeprefix("KIND_").lower(): value for name, value in StockNotetypeKind.items()
}


def _resolve_note_type(col, name: str):
    """The notetype dict for ``name``, or a CollectionToolError listing types.

    Every tool resolves through here so an unknown name fails in Python with
    the available types — never as a bare backend error.
    """
    nt = col.models.by_name(name)
    if nt is None:
        available = sorted(m.name for m in col.models.all_names_and_ids())
        raise CollectionToolError(f"Unknown note type {name!r}. Available models: {available}")
    return nt


def _type_label(nt) -> str:
    return "cloze" if nt["type"] == MODEL_CLOZE else "standard"


def _note_type_summary(col, nt) -> Dict[str, Any]:
    """The get_note_type detail shape: fields, templates, css, sort, use count."""
    mm = col.models
    field_names = mm.field_names(nt)
    return {
        "id": int(nt["id"]),
        "name": nt["name"],
        "type": _type_label(nt),
        "fields": [{"name": f["name"], "ordinal": int(f["ord"])} for f in nt["flds"]],
        "templates": [
            {"name": t["name"], "ordinal": int(t["ord"]), "front": t["qfmt"], "back": t["afmt"]}
            for t in nt["tmpls"]
        ],
        "css": nt["css"],
        "sort_field": field_names[nt["sortf"]],
        "use_count": mm.use_count(nt),
    }


async def get_note_type(registry, username: str, name: str) -> Dict[str, Any]:
    """Full detail for one note type (read-only)."""
    logger.info("Executing get_note_type for %r: %r", username, name)

    def _get(col) -> Dict[str, Any]:
        return _note_type_summary(col, _resolve_note_type(col, name))

    return await registry.run(username, _get)


async def update_note_type(
    sync_manager: SyncManager,
    registry,
    username: str,
    name: str,
    new_name: Optional[str] = None,
    css: Optional[str] = None,
    template_updates: Optional[List[Any]] = None,
    sort_field: Optional[str] = None,
) -> MutationResponse:
    """Rename a note type and/or edit its CSS, templates, and sort field.

    Rendering-level edits only: no field or template is added or removed, so —
    like create_note_type — no confirm gate and no snapshot. All validation
    (unknown templates/fields, name collisions) runs BEFORE update_dict, whose
    ensure_name_unique would otherwise silently mangle a colliding rename.
    """
    if new_name is None and css is None and not template_updates and sort_field is None:
        raise CollectionToolError(
            "Nothing to update: provide at least one of new_name, css, template_updates, "
            "sort_field."
        )
    logger.info(
        "Executing update_note_type for %r: %r (new_name=%r, css=%r, %d template update(s), sort_field=%r)",
        username, name, new_name, css is not None, len(template_updates or []), sort_field,
    )

    def _update(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, name)
        changed: List[str] = []

        if new_name is not None and new_name != nt["name"]:
            existing_id = mm.id_for_name(new_name)
            if existing_id is not None and existing_id != nt["id"]:
                raise CollectionToolError(
                    f"Cannot rename note type {name!r} to {new_name!r}: a note type with that "
                    "name already exists."
                )
            nt["name"] = new_name
            changed.append("name")

        if css is not None and css != nt["css"]:
            nt["css"] = css
            changed.append("css")

        if template_updates:
            by_name = {t["name"]: t for t in nt["tmpls"]}
            requested = [u.template for u in template_updates]
            unknown = sorted(set(requested) - set(by_name))
            if unknown:
                raise CollectionToolError(
                    f"Unknown template name(s) {unknown} of note type {nt['name']!r}. "
                    f"Valid templates for this note type: {sorted(by_name)}."
                )
            # A repeated template name would let later entries silently
            # overwrite earlier ones (the collision check below sees only the
            # first entry per template): refuse the batch before any write.
            duplicates = sorted({t for t in requested if requested.count(t) > 1})
            if duplicates:
                raise CollectionToolError(
                    f"template_updates names the same template twice: {duplicates}; "
                    "combine the edits into one entry."
                )
            # Final template names after every rename in this batch: a rename
            # must not collide with the other templates, with a sibling rename
            # target, or with an unrenamed template.
            finals: List[str] = []
            for t in nt["tmpls"]:
                upd = next((u for u in template_updates if u.template == t["name"]), None)
                final = upd.new_name if upd is not None and upd.new_name else t["name"]
                if final in finals:
                    raise CollectionToolError(
                        f"Two templates of {nt['name']!r} would end up named {final!r}; "
                        "template names must stay unique within the note type."
                    )
                finals.append(final)
            templates_changed = False
            for upd in template_updates:
                template = by_name[upd.template]
                if upd.new_name:
                    template["name"] = upd.new_name
                if upd.front is not None and upd.front != template["qfmt"]:
                    template["qfmt"] = upd.front
                    templates_changed = True
                if upd.back is not None and upd.back != template["afmt"]:
                    template["afmt"] = upd.back
                    templates_changed = True
            if templates_changed:
                changed.append("templates")

        if sort_field is not None:
            field_names = mm.field_names(nt)
            if sort_field not in field_names:
                raise CollectionToolError(
                    f"Unknown sort field {sort_field!r} of note type {nt['name']!r}. "
                    f"Valid fields for this note type: {sorted(field_names)}."
                )
            target_idx = field_names.index(sort_field)
            if target_idx != nt["sortf"]:
                mm.set_sort_index(nt, target_idx)
                changed.append("sort_field")

        mm.update_dict(nt)
        fresh = mm.by_name(nt["name"])  # update_dict evicted the cache: fresh read
        return {"id": int(fresh["id"]), "name": fresh["name"], "changed": changed}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _update)

    return await sync_manager.execute_mutation(
        username,
        operation_name="update_note_type",
        mutation_fn=_mutation,
    )


async def add_note_type_field(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    field_name: str,
    position: Optional[int] = None,
) -> MutationResponse:
    """Append (or insert at ``position``) a new empty field; not destructive."""
    logger.info(
        "Executing add_note_type_field for %r: %r field=%r position=%r",
        username, note_type, field_name, position,
    )

    def _add(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = mm.field_names(nt)
        if field_name in names:
            raise CollectionToolError(
                f"Field {field_name!r} already exists in note type {note_type!r}. "
                f"Existing fields: {sorted(names)}."
            )
        if position is not None and not 0 <= position <= len(names):
            raise CollectionToolError(
                f"position must be between 0 and {len(names)} for note type {note_type!r} "
                f"({len(names)} = appended last)."
            )
        field = mm.new_field(field_name)
        mm.add_field(nt, field)
        if position is not None and position < len(nt["flds"]) - 1:
            mm.reposition_field(nt, field, position)
        mm.update_dict(nt)
        return {
            "note_type": note_type,
            "field": field_name,
            "position": position if position is not None else len(names),
            "field_count": len(names) + 1,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _add)

    return await sync_manager.execute_mutation(
        username,
        operation_name="add_note_type_field",
        mutation_fn=_mutation,
    )


async def remove_note_type_field(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    field_name: str,
    confirm: bool = False,
) -> MutationResponse:
    """Remove a field from a note type after a fail-closed snapshot.

    DESTRUCTIVE: the field's content is deleted on EVERY note of the type. The
    last remaining field cannot be removed (a note type needs at least one).
    """
    if not confirm:
        logger.info("remove_note_type_field for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently deletes that field's content on every note of the "
            "type; pass confirm=true to proceed (a pre-remove-note-type-field snapshot backup "
            "is taken first).",
        )
    logger.info("Executing remove_note_type_field for %r: %r field=%r", username, note_type, field_name)

    def _remove(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = mm.field_names(nt)
        if field_name not in names:
            raise CollectionToolError(
                f"Unknown field {field_name!r} of note type {note_type!r}. "
                f"Valid fields for this note type: {sorted(names)}."
            )
        if len(names) == 1:
            raise CollectionToolError(
                f"Cannot remove field {field_name!r}: it is the last remaining field of note "
                f"type {note_type!r}; a note type needs at least one field."
            )
        # Fail-closed snapshot FIRST: if it raises, nothing is removed.
        registry.snapshot_locked(col, username, "pre-remove-note-type-field")
        mm.remove_field(nt, nt["flds"][names.index(field_name)])
        mm.update_dict(nt)
        remaining = mm.field_names(mm.by_name(note_type))
        return {
            "note_type": note_type,
            "removed_field": field_name,
            "remaining_fields": remaining,
            "snapshot_reason": "pre-remove-note-type-field",
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _remove)

    return await sync_manager.execute_mutation(
        username,
        operation_name="remove_note_type_field",
        mutation_fn=_mutation,
    )


async def rename_note_type_field(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    field_name: str,
    new_name: str,
) -> MutationResponse:
    """Rename a field within a note type; not destructive, no snapshot."""
    logger.info(
        "Executing rename_note_type_field for %r: %r field=%r new_name=%r",
        username, note_type, field_name, new_name,
    )

    def _rename(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = mm.field_names(nt)
        if field_name not in names:
            raise CollectionToolError(
                f"Unknown field {field_name!r} of note type {note_type!r}. "
                f"Valid fields for this note type: {sorted(names)}."
            )
        # Collision within the type; renaming a field to its own name is a no-op.
        if new_name != field_name and new_name in names:
            raise CollectionToolError(
                f"Cannot rename field {field_name!r} to {new_name!r}: that field name already "
                f"exists in note type {note_type!r}. Existing fields: {sorted(names)}."
            )
        mm.rename_field(nt, nt["flds"][names.index(field_name)], new_name)
        mm.update_dict(nt)
        return {"note_type": note_type, "old_name": field_name, "new_name": new_name}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _rename)

    return await sync_manager.execute_mutation(
        username,
        operation_name="rename_note_type_field",
        mutation_fn=_mutation,
    )


async def reposition_note_type_field(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    field_name: str,
    index: int,
) -> MutationResponse:
    """Move a field to a new 0-based position; not destructive, no snapshot."""
    logger.info(
        "Executing reposition_note_type_field for %r: %r field=%r index=%r",
        username, note_type, field_name, index,
    )

    def _reposition(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = mm.field_names(nt)
        if field_name not in names:
            raise CollectionToolError(
                f"Unknown field {field_name!r} of note type {note_type!r}. "
                f"Valid fields for this note type: {sorted(names)}."
            )
        # Python-side bounds check: list.insert would silently clamp an
        # out-of-range index instead of failing.
        if not 0 <= index < len(names):
            raise CollectionToolError(
                f"index must be between 0 and {len(names) - 1} for note type {note_type!r} "
                f"({len(names)} field(s))."
            )
        mm.reposition_field(nt, nt["flds"][names.index(field_name)], index)
        mm.update_dict(nt)
        return {
            "note_type": note_type,
            "moved_field": field_name,
            "index": index,
            "fields": mm.field_names(mm.by_name(note_type)),
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _reposition)

    return await sync_manager.execute_mutation(
        username,
        operation_name="reposition_note_type_field",
        mutation_fn=_mutation,
    )


async def add_note_type_template(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    template: TemplateInput,
    position: Optional[int] = None,
) -> MutationResponse:
    """Append (or insert at ``position``) a new card template; not destructive."""
    logger.info(
        "Executing add_note_type_template for %r: %r template=%r position=%r",
        username, note_type, template.name, position,
    )

    def _add(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = [t["name"] for t in nt["tmpls"]]
        if template.name in names:
            raise CollectionToolError(
                f"Template {template.name!r} already exists in note type {note_type!r}. "
                f"Existing templates: {sorted(names)}."
            )
        if position is not None and not 0 <= position <= len(names):
            raise CollectionToolError(
                f"position must be between 0 and {len(names)} for note type {note_type!r} "
                f"({len(names)} = appended last)."
            )
        new_template = mm.new_template(template.name)
        new_template["qfmt"] = template.front
        new_template["afmt"] = template.back
        mm.add_template(nt, new_template)
        if position is not None and position < len(nt["tmpls"]) - 1:
            mm.reposition_template(nt, new_template, position)
        mm.update_dict(nt)
        return {
            "note_type": note_type,
            "template": template.name,
            "position": position if position is not None else len(names),
            "template_count": len(names) + 1,
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _add)

    return await sync_manager.execute_mutation(
        username,
        operation_name="add_note_type_template",
        mutation_fn=_mutation,
    )


async def remove_note_type_template(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    template_name: str,
    confirm: bool = False,
) -> MutationResponse:
    """Remove a card template after a fail-closed snapshot.

    DESTRUCTIVE: cards generated by the template are deleted. The last
    remaining template cannot be removed.
    """
    if not confirm:
        logger.info("remove_note_type_template for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently deletes the cards generated by that template; "
            "pass confirm=true to proceed (a pre-remove-note-type-template snapshot backup "
            "is taken first).",
        )
    logger.info(
        "Executing remove_note_type_template for %r: %r template=%r",
        username, note_type, template_name,
    )

    def _remove(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = [t["name"] for t in nt["tmpls"]]
        if template_name not in names:
            raise CollectionToolError(
                f"Unknown template {template_name!r} of note type {note_type!r}. "
                f"Valid templates for this note type: {sorted(names)}."
            )
        if len(names) == 1:
            raise CollectionToolError(
                f"Cannot remove template {template_name!r}: it is the last remaining template "
                f"of note type {note_type!r}; a note type needs at least one template."
            )
        ordinal = names.index(template_name)
        # Cards on this template's ordinal are the ones the removal deletes.
        cards_removed = mm.template_use_count(nt["id"], ordinal)
        # Fail-closed snapshot FIRST: if it raises, nothing is removed.
        registry.snapshot_locked(col, username, "pre-remove-note-type-template")
        mm.remove_template(nt, nt["tmpls"][ordinal])
        mm.update_dict(nt)
        return {
            "note_type": note_type,
            "removed_template": template_name,
            "cards_removed": cards_removed,
            "remaining_templates": [t["name"] for t in mm.by_name(note_type)["tmpls"]],
            "snapshot_reason": "pre-remove-note-type-template",
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _remove)

    return await sync_manager.execute_mutation(
        username,
        operation_name="remove_note_type_template",
        mutation_fn=_mutation,
    )


async def reposition_note_type_template(
    sync_manager: SyncManager,
    registry,
    username: str,
    note_type: str,
    template_name: str,
    index: int,
) -> MutationResponse:
    """Move a template to a new 0-based position; not destructive, no snapshot."""
    logger.info(
        "Executing reposition_note_type_template for %r: %r template=%r index=%r",
        username, note_type, template_name, index,
    )

    def _reposition(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, note_type)
        names = [t["name"] for t in nt["tmpls"]]
        if template_name not in names:
            raise CollectionToolError(
                f"Unknown template {template_name!r} of note type {note_type!r}. "
                f"Valid templates for this note type: {sorted(names)}."
            )
        if not 0 <= index < len(names):
            raise CollectionToolError(
                f"index must be between 0 and {len(names) - 1} for note type {note_type!r} "
                f"({len(names)} template(s))."
            )
        mm.reposition_template(nt, nt["tmpls"][names.index(template_name)], index)
        mm.update_dict(nt)
        return {
            "note_type": note_type,
            "moved_template": template_name,
            "index": index,
            "templates": [t["name"] for t in mm.by_name(note_type)["tmpls"]],
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _reposition)

    return await sync_manager.execute_mutation(
        username,
        operation_name="reposition_note_type_template",
        mutation_fn=_mutation,
    )


async def delete_note_type(
    sync_manager: SyncManager,
    registry,
    username: str,
    name: str,
    confirm: bool = False,
) -> MutationResponse:
    """Delete an UNUSED note type (use_count == 0) after a fail-closed snapshot.

    A note type with notes on it is refused first, with the count, so the
    caller can move or delete those notes; only then does the confirm gate and
    the pre-delete snapshot apply.
    """
    if not confirm:
        logger.info("delete_note_type for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently deletes the note type; pass confirm=true to "
            "proceed (a pre-delete-note-type snapshot backup is taken first).",
        )
    logger.info("Executing delete_note_type for %r: %r", username, name)

    def _delete(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, name)
        uses = mm.use_count(nt)
        if uses:
            raise CollectionToolError(
                f"Cannot delete note type {name!r}: {uses} note(s) still use it. Change or "
                "delete those notes first (the use count must be 0)."
            )
        # Fail-closed snapshot FIRST: if it raises, nothing is deleted.
        registry.snapshot_locked(col, username, "pre-delete-note-type")
        mm.remove(nt["id"])
        return {
            "name": name,
            "id": int(nt["id"]),
            "deleted": True,
            "snapshot_reason": "pre-delete-note-type",
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _delete)

    return await sync_manager.execute_mutation(
        username,
        operation_name="delete_note_type",
        mutation_fn=_mutation,
    )


async def restore_note_type_to_stock(
    sync_manager: SyncManager,
    registry,
    username: str,
    name: str,
    force_kind: Optional[str] = None,
    confirm: bool = False,
) -> MutationResponse:
    """Reset a modified note type's fields/templates/CSS to the stock defaults.

    DESTRUCTIVE: the field and template LIST is replaced wholesale with the
    stock type's (probed wheel behavior), so notes keep values only where a
    stock field lines up and content under mismatched stock fields can be
    lost — that is what the pre-restore snapshot is for. ``force_kind``
    selects WHICH stock type is applied; without it the wheel falls back to
    the type's recorded original stock kind (refused here, before the
    snapshot, when the type does not record one — the backend's InvalidInput
    for that case would be mistaken for a stale collection handle).
    """
    # Map the client string BEFORE the confirm gate so a typo surfaces even
    # when the call is skipped.
    kind = None
    if force_kind is not None:
        kind = STOCK_KIND_NAMES.get(force_kind)
        if kind is None:
            raise CollectionToolError(
                f"Unknown force_kind {force_kind!r}. Valid kinds: {sorted(STOCK_KIND_NAMES)}."
            )
    if not confirm:
        logger.info("restore_note_type_to_stock for %r skipped: confirm=False", username)
        return MutationResponse(
            success=False,
            local_status="skipped",
            sync_status=SyncStatus(),
            error="This operation permanently replaces the note type's fields, templates, and "
            "CSS with the stock defaults, discarding content under mismatched stock fields; "
            "pass confirm=true to proceed (a pre-restore-note-type-stock snapshot backup is "
            "taken first).",
        )
    logger.info(
        "Executing restore_note_type_to_stock for %r: %r force_kind=%r",
        username, name, force_kind,
    )

    def _restore(col) -> Dict[str, Any]:
        mm = col.models
        nt = _resolve_note_type(col, name)
        if kind is None and not nt.get("originalStockKind"):
            raise CollectionToolError(
                f"Cannot restore note type {name!r} to stock without force_kind: it does not "
                "record which stock type it came from (originalStockKind is unknown). Pass "
                f"force_kind explicitly: {sorted(STOCK_KIND_NAMES)}."
            )
        # Fail-closed snapshot FIRST: if it raises, nothing is restored.
        registry.snapshot_locked(col, username, "pre-restore-note-type-stock")
        mm.restore_notetype_to_stock(nt["id"], kind)
        # The raw backend call bypasses the wheel's cache eviction (only
        # update_dict/remove/close clear it), so stale reads would serve the
        # pre-restore schema for the life of the open handle.
        mm._clear_cache()
        fresh = mm.by_name(nt["name"])
        return {
            "name": name,
            "force_kind": force_kind,
            "restored": True,
            "type": _type_label(fresh),
            "fields": [f["name"] for f in fresh["flds"]],
            "templates": [t["name"] for t in fresh["tmpls"]],
            "snapshot_reason": "pre-restore-note-type-stock",
        }

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _restore)

    return await sync_manager.execute_mutation(
        username,
        operation_name="restore_note_type_to_stock",
        mutation_fn=_mutation,
    )
