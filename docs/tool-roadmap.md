# MCP Tool Roadmap

Phased plan for growing the MCP surface beyond the initial 26 tools. Each
phase ships as **its own PR behind a review gate** — no phase lands directly
on `main`, and every phase keeps the standing rules: real per-user
collections (no mocks in tests), fail-closed snapshots before destructive
writes, ledger-backed idempotency for retries, and **raw SQL execution and
arbitrary add-on passthrough remain out of scope** to preserve collection
integrity.

## Phase 1 — Mass note-type change (DONE, this PR)

- [x] `preview_note_type_change` — read-only preview of a note-type change:
      resolved field/template mapping (`{new name: old name or null}`),
      old/new type names, cloze flag, affected note count, and warnings for
      every old field discarded and every old template whose cards would be
      deleted.
- [x] `change_note_type` — the destructive counterpart (confirm-gated,
      `pre-change-note-type` snapshot first, 1–2000 IDs per single backend
      operation, ledger-backed idempotency). Both tools share one map
      resolver so the preview always equals what the mutation would do.

## Phase 2 — Mass-edit pair (DONE)

- [x] `find_and_replace` — bulk search/replace over note fields
      (`col.find_and_replace`: note_ids or query — shared resolver with
      change_note_type minus its single-note-type constraint —, search,
      replacement, regex (Rust `$1` capture references), field_name
      (passed through without existence checks: selections may span note
      types), match_case). Shipped as a plain content edit in the
      update_note_fields class — no confirm gate and no snapshot: nothing is
      deleted or discarded, and reversibility comes from the portal's
      ordinary snapshot machinery. The response reports the backend's changed
      count next to the selection size, so zero-match no-ops are visible.
- [x] `find_duplicates` — duplicate report via `col.find_dupes` (read-only;
      returns duplicate groups for a field so an agent can clean up before
      or after a mass edit; optional Anki search prefilter, paginated
      groups). The wheel's find_dupes is a Python-side implementation whose
      HTML stripping needs `anki.lang.current_i18n`, so the notes tools
      module initializes it once (nothing else in the service needed i18n).

## Phase 3 — Note-type control set (DONE, tools 31–41)

- [x] `get_note_type` — full detail for one note type: field ordinals,
      template `qfmt`/`afmt`, CSS, type (standard/cloze), sort field, and
      use count.
- [x] `update_note_type` — rename / CSS / template edits / sort field via
      `models.update_dict` (no snapshot: rendering-level edits only — the
      roadmap's earlier "confirm + snapshot when the schema changes" was
      resolved as: renames and CSS/template/sort edits delete nothing, so
      they ship in the `create_note_type` class; only the removal tools
      below snapshot). Renames are collision-checked via
      `models.id_for_name` first — `update_dict`'s
      `ensure_name_unique` would otherwise silently mangle the name.
- [x] Field operations — `add_note_type_field` (append or insert at a
      0-based position), `remove_note_type_field` (confirm +
      `pre-remove-note-type-field` snapshot; last remaining field refused),
      `rename_note_type_field` (collision-checked), and
      `reposition_note_type_field` (Python-side bounds check — the wheel's
      `list.insert` would silently clamp).
- [x] Template operations — `add_note_type_template` /
      `remove_note_type_template` (confirm + `pre-remove-note-type-template`
      snapshot; last template refused; response reports the card count the
      removal deletes) / `reposition_note_type_template`.
- [x] `delete_note_type` — only when `use_count == 0` (a used type is
      refused with its count before the snapshot; confirm +
      `pre-delete-note-type` snapshot).
- [x] `restore_note_type_to_stock` — reset a modified stock note type
      (confirm + `pre-restore-note-type-stock` snapshot). `force_kind`
      maps the `anki.stdmodels.StockNotetypeKind` names; probed wheel
      semantics: the field/template LIST is replaced wholesale, so content
      under mismatched stock fields is lost, and the raw backend call does
      not evict the wheel's Python notetype cache — the tool clears it, or
      every later read on the open handle would serve the pre-restore
      schema.

## Phase 4 — Scheduling and organization

- [ ] Deck config preset read/save.
- [ ] `set_card_flags` — mark cards with the flag colors.
- [ ] Bury / unbury cards.
- [ ] Reposition new cards in the new-card queue — **verify the exact
      anki==26.9.3 scheduler API names first** (the scheduling surface moved
      several times across wheel generations).
