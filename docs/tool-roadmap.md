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

## Phase 2 — Mass-edit pair

- [ ] `find_and_replace` — bulk search/replace over note fields
      (`col.find_and_replace`: note_ids or query, search, replacement,
      regex, field_name, match_case). Confirm-gated with a fail-closed
      snapshot when the scope exceeds a trivial size.
- [ ] `find_duplicates` — duplicate report via `col.find_dupes` (read-only;
      returns duplicate groups for a field so an agent can clean up before
      or after a mass edit).

## Phase 3 — Note-type control set

- [ ] `get_note_type` — full detail for one note type: field ordinals,
      template `qfmt`/`afmt`, CSS, type (standard/cloze), sort field, and
      use count.
- [ ] `update_note_type` — rename / CSS / template edits / sort field via
      `models.update_dict` (confirm + snapshot when the schema changes).
- [ ] Field operations — add / remove / rename / reposition fields.
- [ ] Template operations — add / remove / reposition card templates.
- [ ] `delete_note_type` — only when `use_count == 0` (confirm + snapshot).
- [ ] `restore_notetype_to_stock` — reset a modified stock note type
      (confirm + snapshot).

## Phase 4 — Scheduling and organization

- [ ] Deck config preset read/save.
- [ ] `set_card_flags` — mark cards with the flag colors.
- [ ] Bury / unbury cards.
- [ ] Reposition new cards in the new-card queue — **verify the exact
      anki==26.9.3 scheduler API names first** (the scheduling surface moved
      several times across wheel generations).
