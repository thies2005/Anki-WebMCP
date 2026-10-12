"""Data models and input validation schemas for Anki MCP service."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NoteInput(BaseModel):
    """Specification of a new note to create.

    The note type field is canonically ``note_type`` (consistent with every
    other note-type tool); ``model_name`` is accepted as a legacy alias for
    callers written before the rename. Exactly one of the two must be given,
    or both with the same value.
    """
    deck_name: str = Field(..., description="Name of target deck, e.g. 'Default'")
    note_type: str = Field(..., description="Note type name, e.g. 'Basic' or 'Cloze'")
    fields: Dict[str, str] = Field(..., description="Field names and their string values")
    tags: List[str] = Field(default_factory=list, description="Tags to assign to the note")

    @model_validator(mode="before")
    @classmethod
    def _accept_model_name_alias(cls, data):
        """Fold the legacy ``model_name`` key into canonical ``note_type``."""
        if not isinstance(data, dict):
            return data
        legacy = data.pop("model_name", None)
        if legacy is None:
            return data
        canonical = data.get("note_type")
        if canonical is not None and canonical != legacy:
            raise ValueError(
                "Pass the note type once: 'note_type' is canonical and the legacy "
                "alias 'model_name' must not disagree with it."
            )
        data["note_type"] = legacy
        return data

    @field_validator("deck_name", "note_type")
    @classmethod
    def not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Field must not be empty.")
        return s

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v:
            raise ValueError("Fields dict must contain at least one field.")
        return v


class AddNotesInput(BaseModel):
    """Input parameters for add_notes tool."""
    notes: List[NoteInput] = Field(..., description="List of notes to create")
    # Bound matches ledger.MAX_KEY_LEN (the ledger rejects longer keys): an
    # unbounded key would persist arbitrarily many bytes in ledger.json on the
    # shared /data volume and rewrite them whole on every keyed mutation.
    idempotency_key: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional unique key to prevent duplicate creation on network retries",
    )
    allow_duplicates: bool = Field(
        False,
        description="Whether to permit duplicate notes per Anki duplicate scope checks",
    )

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v: List[NoteInput]) -> List[NoteInput]:
        if not v:
            raise ValueError("At least one note must be provided.")
        if len(v) > 100:
            raise ValueError("Batch note creation is limited to 100 notes per request.")
        return v


class UpdateNoteFieldsInput(BaseModel):
    """Input parameters for update_note_fields tool."""
    note_id: int = Field(..., gt=0, description="Unique Anki note ID to update")
    fields: Dict[str, str] = Field(..., description="Dictionary of field names to new values")

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v:
            raise ValueError("At least one field to update must be provided.")
        return v


class AddTagsInput(BaseModel):
    """Input parameters for add_tags tool."""
    note_ids: List[int] = Field(..., description="List of note IDs to tag")
    tags: List[str] = Field(..., description="List of tag strings to add")

    @field_validator("note_ids")
    @classmethod
    def validate_note_ids(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("At least one note ID must be provided.")
        for nid in v:
            if nid <= 0:
                raise ValueError("Note IDs must be positive integers.")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: List[str]) -> List[str]:
        cleaned = [t.strip() for t in v if t.strip()]
        if not cleaned:
            raise ValueError("At least one non-empty tag must be provided.")
        return cleaned


class SearchNotesInput(BaseModel):
    """Input parameters for search_notes tool."""
    query: str = Field(..., description="Anki search query string, e.g. 'deck:Default' or 'tag:vocab'")
    limit: int = Field(50, ge=1, le=100, description="Max notes to return (1-100, default 50)")
    offset: int = Field(0, ge=0, description="Pagination offset index (default 0)")


class GetNoteInput(BaseModel):
    """Input parameters for get_note tool."""
    note_id: int = Field(..., gt=0, description="Unique Anki note ID")


class SyncStatus(BaseModel):
    """Status details for pre- and post-synchronization stages."""
    pre_sync: str = Field("skipped", description="'ok', 'auth_not_configured', 'failed', or 'skipped'")
    post_sync: str = Field("skipped", description="'ok', 'failed', or 'skipped'")
    pre_sync_error: Optional[str] = None
    post_sync_error: Optional[str] = None


class MutationResponse(BaseModel):
    """Standardized response schema for state-mutating operations."""
    success: bool
    local_status: str = Field(..., description="'committed', 'failed', or 'skipped'")
    sync_status: SyncStatus
    data: Optional[Any] = None
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


# -- shared free-text validators (usable from both pydantic models and tools) ----


def validate_deck_name(name: str) -> str:
    """Validate a deck name against the hierarchy rules; raise ValueError otherwise.

    Anki deck names are '::'-joined hierarchies; the banned shapes here are the
    ones the backend mangles or silently mis-hierarchies: empty, control
    characters, a leading/trailing separator (empty hierarchy component), and
    the ':::' sequence (ambiguous separator run).
    """
    if not name or not name.strip():
        raise ValueError("Deck name must not be empty.")
    if len(name) > 200:
        raise ValueError(f"Deck name is limited to 200 characters (got {len(name)}).")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Deck name must not contain control characters.")
    if name.startswith("::") or name.endswith("::"):
        raise ValueError("Deck name must not start or end with '::'.")
    if ":::" in name:
        raise ValueError("Deck name must not contain ':::'.")
    return name


def _reject_control_chars(value: str, what: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{what} must not contain control characters.")
    return value


def _reject_control_chars_keep_line_breaks(value: str, what: str) -> str:
    """Free-text rule where newlines/tabs are legitimate note content; every
    other control character is refused (companion to _reject_control_chars)."""
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in value):
        raise ValueError(f"{what} must not contain control characters (newlines and tabs are allowed).")
    return value


def _exactly_one_selection(note_ids: Optional[List[int]], query: Optional[str]) -> None:
    """Shared note_ids-XOR-query rule (preview/change_note_type, find_and_replace)."""
    if (note_ids is None) == (query is None):
        raise ValueError("Provide exactly one of note_ids or query.")


# -- deck tools -----------------------------------------------------------------


class CreateDeckInput(BaseModel):
    """Input parameters for create_deck tool."""
    deck_name: str = Field(..., description="Deck name; use 'Parent::Child' for subdecks")

    @field_validator("deck_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_deck_name(v)


class RenameDeckInput(BaseModel):
    """Input parameters for rename_deck tool (destructive: snapshots first)."""
    deck_id: int = Field(..., gt=0, description="Unique Anki deck ID to rename")
    new_name: str = Field(..., description="New deck name; use 'Parent::Child' for subdecks")
    confirm: bool = Field(False, description="Must be true to actually rename (a pre-rename-deck snapshot is taken first)")

    @field_validator("new_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_deck_name(v)


class DeleteDeckInput(BaseModel):
    """Input parameters for delete_deck tool (destructive: snapshots first)."""
    deck_id: int = Field(..., gt=0, description="Unique Anki deck ID to delete (the Default deck, ID 1, cannot be deleted)")
    confirm: bool = Field(False, description="Must be true to actually delete (a pre-delete-deck snapshot is taken first)")


class GetDeckConfigInput(BaseModel):
    """Input parameters for get_deck_config tool (read-only)."""
    deck_id: int = Field(..., gt=0, description="Unique Anki deck ID")


# Keys update_deck_config applies, as a module constant so the whitelist can
# never drift between the input model's rejection message and the tool.
DECK_CONFIG_UPDATE_KEYS = ("new_per_day", "reviews_per_day", "max_answer_secs", "autoplay_audio")


class UpdateDeckConfigInput(BaseModel):
    """Input parameters for update_deck_config tool.

    The ``updates`` object accepts ONLY the whitelisted scheduling keys:
    anything else is rejected HERE (inside the MCP wrapper, so the error
    becomes structured tool output and never reaches the backend mutation).
    """

    # Unknown keys must SURVIVE construction until the validator runs, so they
    # can be named (with the valid keys) in the error instead of pydantic's
    # generic "extra inputs are not permitted".
    model_config = ConfigDict(extra="allow")

    deck_id: int = Field(..., gt=0, description="Unique Anki deck ID whose preset is edited")
    new_per_day: Optional[int] = Field(None, ge=0, le=999, description="New cards per day (0-999)")
    reviews_per_day: Optional[int] = Field(None, ge=0, le=999, description="Maximum reviews per day (0-999)")
    max_answer_secs: Optional[int] = Field(None, ge=10, le=120, description="Max seconds before the answer timer auto-reveals (10-120)")
    autoplay_audio: Optional[bool] = Field(None, description="Whether question/answer audio plays automatically")

    @model_validator(mode="after")
    def whitelist_and_at_least_one(self) -> "UpdateDeckConfigInput":
        unknown = sorted(self.model_extra or {})
        if unknown:
            raise ValueError(
                f"Unknown update key(s) {unknown}. Valid keys: {list(DECK_CONFIG_UPDATE_KEYS)}."
            )
        if all(getattr(self, key) is None for key in DECK_CONFIG_UPDATE_KEYS):
            raise ValueError(f"Provide at least one of {list(DECK_CONFIG_UPDATE_KEYS)}.")
        return self


# -- note tools --------------------------------------------------------------------


class _IdListInput(BaseModel):
    """Shared shape for tools taking a bounded list of positive Anki object IDs."""

    @staticmethod
    def _validate_id_list(v: List[int], what: str) -> List[int]:
        if not v:
            raise ValueError(f"At least one {what} must be provided.")
        if len(v) > 500:
            raise ValueError(f"Batches are limited to 500 {what} per request.")
        for i in v:
            if i <= 0:
                raise ValueError(f"{what} must be positive integers.")
        return v


class DeleteNotesInput(_IdListInput):
    """Input parameters for delete_notes tool (destructive: snapshots first)."""
    note_ids: List[int] = Field(..., description="Note IDs to delete (1-500 per request)")
    confirm: bool = Field(False, description="Must be true to actually delete (a pre-delete-notes snapshot is taken first)")
    idempotency_key: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional unique key to prevent duplicate deletion on network retries",
    )

    @field_validator("note_ids")
    @classmethod
    def validate_ids(cls, v: List[int]) -> List[int]:
        return cls._validate_id_list(v, "note ID")


class RemoveTagsInput(_IdListInput):
    """Input parameters for remove_tags tool."""
    note_ids: List[int] = Field(..., description="Note IDs to strip tags from (1-500 per request)")
    tags: List[str] = Field(..., description="Tags to remove (1-50 tags, no internal whitespace, max 100 chars each)")

    @field_validator("note_ids")
    @classmethod
    def validate_ids(cls, v: List[int]) -> List[int]:
        return cls._validate_id_list(v, "note ID")

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: List[str]) -> List[str]:
        if not v or len(v) > 50:
            raise ValueError("Provide between 1 and 50 tags.")
        for tag in v:
            if not tag or not tag.strip():
                raise ValueError("Tags must not be empty.")
            if len(tag) > 100:
                raise ValueError("Each tag is limited to 100 characters.")
            if any(c.isspace() for c in tag):
                raise ValueError(f"Tag {tag!r} must not contain whitespace (tags are space-separated).")
            _reject_control_chars(tag, "Tags")
        return v


class TemplateInput(BaseModel):
    """One card template of a new note type."""
    name: str = Field(..., min_length=1, max_length=5000, description="Template name, e.g. 'Card 1'")
    front: str = Field(..., min_length=1, max_length=5000, description="Question template (qfmt), e.g. '{{Front}}'")
    back: str = Field(..., min_length=1, max_length=5000, description="Answer template (afmt), e.g. '{{FrontSide}}<hr id=answer>{{Back}}'")

    @field_validator("name", "front", "back")
    @classmethod
    def no_control_chars(cls, v: str) -> str:
        return _reject_control_chars(v, "Template parts")


class CreateNoteTypeInput(BaseModel):
    """Input parameters for create_note_type tool."""
    name: str = Field(..., min_length=1, max_length=100, description="New note type name (must not already exist)")
    fields: List[str] = Field(..., description="Field names in order (1-20 fields, 1-100 chars each, unique)")
    templates: Optional[List[TemplateInput]] = Field(
        None,
        description="Card templates (1-10). Omit for the default single template over the first two fields.",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Note type name must not be empty.")
        return _reject_control_chars(s, "Note type name")

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, v: List[str]) -> List[str]:
        if not v or len(v) > 20:
            raise ValueError("Provide between 1 and 20 fields.")
        seen = set()
        for name in v:
            if not name or not name.strip():
                raise ValueError("Field names must not be empty.")
            if len(name) > 100:
                raise ValueError("Each field name is limited to 100 characters.")
            _reject_control_chars(name, "Field names")
            if name in seen:
                raise ValueError(f"Duplicate field name {name!r}.")
            seen.add(name)
        return v

    @field_validator("templates")
    @classmethod
    def validate_templates(cls, v: Optional[List[TemplateInput]]) -> Optional[List[TemplateInput]]:
        if v is not None and not (1 <= len(v) <= 10):
            raise ValueError("Provide between 1 and 10 templates (or omit for the default).")
        return v


class _NoteTypeChangeInputBase(BaseModel):
    """Shared selection shape for preview_note_type_change / change_note_type.

    Exactly one of ``note_ids`` or ``query`` selects the notes: an explicit ID
    list or an Anki search string, never both and never neither.
    """

    new_note_type: str = Field(..., min_length=1, max_length=100, description="Target note type name (must already exist)")
    note_ids: Optional[List[int]] = Field(None, description="Note IDs to change (1-2000 per request); mutually exclusive with query")
    query: Optional[str] = Field(None, description="Anki search selecting the notes, e.g. 'note:Src3' or 'deck:Default'; mutually exclusive with note_ids")

    @field_validator("new_note_type")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Note type name must not be empty.")
        return _reject_control_chars(s, "Note type name")

    @field_validator("note_ids")
    @classmethod
    def validate_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is None:
            return v
        # Bound deviates from the shared _IdListInput 500 cap on purpose: a
        # note-type change is ONE backend operation over the whole selection
        # (a single change_notetype request), so per-request cost does not
        # grow per ID the way per-item mutations (delete/tag) do; 2000 keeps
        # a runaway client from repricing the whole collection in one call.
        if not (1 <= len(v) <= 2000):
            raise ValueError("Select between 1 and 2000 note IDs per request.")
        for nid in v:
            if nid <= 0:
                raise ValueError("Note IDs must be positive integers.")
        return v

    @model_validator(mode="after")
    def exactly_one_selection(self) -> "_NoteTypeChangeInputBase":
        _exactly_one_selection(self.note_ids, self.query)
        return self


class PreviewNoteTypeChangeInput(_NoteTypeChangeInputBase):
    """Input parameters for preview_note_type_change (read-only)."""


class ChangeNoteTypeInput(_NoteTypeChangeInputBase):
    """Input parameters for change_note_type tool (destructive: snapshots first)."""
    # An empty dict normalizes to None (backend default mapping): "discard
    # every field" is never a useful explicit request, and treating {} as
    # "map positionally" keeps a half-typed call from becoming total data loss.
    field_map: Optional[Dict[str, Optional[str]]] = Field(
        None,
        description="NEW field name -> OLD field name (or null to discard). Omitted: positional default; new fields not mentioned are discarded.",
    )
    template_map: Optional[Dict[str, Optional[str]]] = Field(
        None,
        description="NEW template name -> OLD template name (or null). Omitted: positional default; refused when either note type is a Cloze type.",
    )
    confirm: bool = Field(False, description="Must be true to actually change (run preview_note_type_change first; a pre-change-note-type snapshot is taken first)")
    idempotency_key: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional unique key to prevent duplicate changes on network retries",
    )


class FindAndReplaceInput(BaseModel):
    """Input parameters for find_and_replace tool (bulk content edit, not destructive)."""

    search: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Text to find (a regular expression when regex=true)",
    )
    replacement: str = Field(
        "",
        max_length=10000,
        description="Replacement text; with regex=true, $1, $2, ... reference capture groups (Rust regex syntax). Empty deletes every match",
    )
    note_ids: Optional[List[int]] = Field(
        None,
        description="Note IDs to edit (1-2000 per request); mutually exclusive with query",
    )
    query: Optional[str] = Field(
        None,
        description="Anki search selecting the notes to edit, e.g. 'deck:Default' or 'tag:vocab'; mutually exclusive with note_ids",
    )
    regex: bool = Field(False, description="Treat search (and $N references in replacement) as a regular expression")
    field_name: Optional[str] = Field(
        None,
        description="Restrict the replace to this field name (matched across note types); omit to replace in every field. Passed through WITHOUT existence validation: selections may span note types, and an unknown name simply matches nothing",
    )
    match_case: bool = Field(False, description="Case-sensitive matching (default: case-insensitive)")

    @field_validator("search", "replacement")
    @classmethod
    def validate_text(cls, v: str) -> str:
        # Newlines and tabs are legitimate note content; other control
        # characters are refused (shared free-text rule).
        return _reject_control_chars_keep_line_breaks(v, "Search and replacement text")

    @field_validator("note_ids")
    @classmethod
    def validate_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is None:
            return v
        # Same bound as the note-type change: find_and_replace is ONE backend
        # operation over the whole selection, so per-request cost does not
        # grow per ID the way per-item mutations (delete/tag) do.
        if not (1 <= len(v) <= 2000):
            raise ValueError("Select between 1 and 2000 note IDs per request.")
        for nid in v:
            if nid <= 0:
                raise ValueError("Note IDs must be positive integers.")
        return v

    @model_validator(mode="after")
    def exactly_one_selection(self) -> "FindAndReplaceInput":
        _exactly_one_selection(self.note_ids, self.query)
        return self


class FindDuplicatesInput(BaseModel):
    """Input parameters for find_duplicates tool (read-only)."""

    field_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Field whose identical values define duplicate groups (matched case-insensitively across note types)",
    )
    search: str = Field(
        "",
        description="Optional Anki query prefilter narrowing which notes are examined, e.g. 'deck:Default'",
    )
    limit: int = Field(100, ge=1, le=500, description="Max duplicate groups to return (1-500, default 100)")
    offset: int = Field(0, ge=0, description="Pagination offset over the groups (default 0)")

    @field_validator("field_name")
    @classmethod
    def validate_field_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field name must not be empty.")
        return _reject_control_chars(v, "Field name")


# -- note-type control tools -------------------------------------------------------


def validate_note_type_name(name: str) -> str:
    """Shared note-type-name rule for the note-type control input models."""
    s = name.strip()
    if not s:
        raise ValueError("Note type name must not be empty.")
    return _reject_control_chars(s, "Note type name")


class GetNoteTypeInput(BaseModel):
    """Input parameters for get_note_type tool (read-only)."""
    name: str = Field(..., min_length=1, max_length=100, description="Existing note type name, e.g. 'Basic'")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_note_type_name(v)


class TemplateUpdateInput(BaseModel):
    """One template edit inside update_note_type's template_updates list."""
    template: str = Field(..., min_length=1, max_length=5000, description="Name of the EXISTING template to edit")
    new_name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=5000,
        description="Rename the template (must not collide with another template of the same type)",
    )
    front: Optional[str] = Field(None, min_length=1, max_length=5000, description="New question template (qfmt)")
    back: Optional[str] = Field(None, min_length=1, max_length=5000, description="New answer template (afmt)")

    @field_validator("template", "new_name", "front", "back")
    @classmethod
    def no_control_chars(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _reject_control_chars(v, "Template parts")


class UpdateNoteTypeInput(BaseModel):
    """Input parameters for update_note_type tool."""
    name: str = Field(..., min_length=1, max_length=100, description="Existing note type name to edit")
    new_name: Optional[str] = Field(
        None, min_length=1, max_length=100, description="Rename the note type (must not already exist)"
    )
    css: Optional[str] = Field(
        None,
        max_length=65536,
        description="Replacement styling (CSS) for the note type's cards",
    )
    template_updates: Optional[List[TemplateUpdateInput]] = Field(
        None,
        description="Edits to existing templates (1-20 per request); each names an existing template",
    )
    sort_field: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Field name to sort by (becomes the first field for new-note ordering)",
    )

    @field_validator("name", "new_name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_note_type_name(v)

    @field_validator("sort_field")
    @classmethod
    def validate_sort_field(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("Sort field name must not be empty.")
        return _reject_control_chars(v, "Sort field name")

    @field_validator("css")
    @classmethod
    def validate_css(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Stylesheets are legitimately multi-line: newlines stay allowed.
        return _reject_control_chars_keep_line_breaks(v, "CSS")

    @field_validator("template_updates")
    @classmethod
    def validate_template_updates(cls, v: Optional[List[TemplateUpdateInput]]) -> Optional[List[TemplateUpdateInput]]:
        if v is not None and not (1 <= len(v) <= 20):
            raise ValueError("Provide between 1 and 20 template updates (or omit the list).")
        return v

    @model_validator(mode="after")
    def at_least_one_change(self) -> "UpdateNoteTypeInput":
        if self.new_name is None and self.css is None and self.template_updates is None and self.sort_field is None:
            raise ValueError("Provide at least one of new_name, css, template_updates, or sort_field.")
        return self


class AddNoteTypeFieldInput(BaseModel):
    """Input parameters for add_note_type_field tool."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    field_name: str = Field(..., min_length=1, max_length=100, description="Name of the new field (must not already exist)")
    position: Optional[int] = Field(
        None,
        description="0-based insertion position; omit (or the field count) appends the field at the end",
    )

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)

    @field_validator("field_name")
    @classmethod
    def validate_field_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field name must not be empty.")
        return _reject_control_chars(v, "Field name")


class RemoveNoteTypeFieldInput(BaseModel):
    """Input parameters for remove_note_type_field tool (destructive: snapshots first)."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    field_name: str = Field(..., min_length=1, max_length=100, description="Existing field of the note type")
    confirm: bool = Field(
        False,
        description="Must be true to actually remove (the field's content is deleted on EVERY note of the type; "
        "a pre-remove-note-type-field snapshot is taken first)",
    )

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)

    @field_validator("field_name")
    @classmethod
    def validate_field_name(cls, v: str) -> str:
        return _reject_control_chars(v, "Field name")


class RenameNoteTypeFieldInput(BaseModel):
    """Input parameters for rename_note_type_field tool."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    field_name: str = Field(..., min_length=1, max_length=100, description="Existing field of the note type")
    new_name: str = Field(..., min_length=1, max_length=100, description="New field name (must not collide within the type)")

    @field_validator("note_type", "new_name")
    @classmethod
    def validate_names(cls, v: str) -> str:
        return validate_note_type_name(v)


class RepositionNoteTypeFieldInput(BaseModel):
    """Input parameters for reposition_note_type_field tool."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    field_name: str = Field(..., min_length=1, max_length=100, description="Existing field of the note type")
    index: int = Field(..., description="New 0-based position for the field (0..field count - 1)")

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)


class AddNoteTypeTemplateInput(BaseModel):
    """Input parameters for add_note_type_template tool."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    template: TemplateInput = Field(..., description="The template to add (name must not already exist)")
    position: Optional[int] = Field(
        None,
        description="0-based insertion position; omit (or the template count) appends the template at the end",
    )

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)


class RemoveNoteTypeTemplateInput(BaseModel):
    """Input parameters for remove_note_type_template tool (destructive: snapshots first)."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    template_name: str = Field(..., min_length=1, max_length=5000, description="Existing template of the note type")
    confirm: bool = Field(
        False,
        description="Must be true to actually remove (cards generated by the template are deleted; "
        "a pre-remove-note-type-template snapshot is taken first)",
    )

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)


class RepositionNoteTypeTemplateInput(BaseModel):
    """Input parameters for reposition_note_type_template tool."""
    note_type: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    template_name: str = Field(..., min_length=1, max_length=5000, description="Existing template of the note type")
    index: int = Field(..., description="New 0-based position for the template (0..template count - 1)")

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, v: str) -> str:
        return validate_note_type_name(v)


class DeleteNoteTypeInput(BaseModel):
    """Input parameters for delete_note_type tool (destructive: snapshots first)."""
    name: str = Field(..., min_length=1, max_length=100, description="Existing note type name (must have zero notes)")
    confirm: bool = Field(
        False,
        description="Must be true to actually delete (a pre-delete-note-type snapshot is taken first)",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_note_type_name(v)


class RestoreNoteTypeToStockInput(BaseModel):
    """Input parameters for restore_note_type_to_stock tool (destructive: snapshots first)."""
    name: str = Field(..., min_length=1, max_length=100, description="Existing note type name")
    force_kind: Optional[str] = Field(
        None,
        max_length=100,
        description="Which stock type to restore to ('basic', 'basic_and_reversed', 'basic_optional_reversed', "
        "'basic_typing', 'cloze', 'image_occlusion'); omit to use the type's original stock kind",
    )
    confirm: bool = Field(
        False,
        description="Must be true to actually restore (fields/templates/CSS are replaced by the stock defaults; "
        "a pre-restore-note-type-stock snapshot is taken first)",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_note_type_name(v)

    @field_validator("force_kind")
    @classmethod
    def validate_force_kind(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        s = v.strip().lower()
        if not s:
            raise ValueError("force_kind must not be empty (omit it to use the original stock kind).")
        return _reject_control_chars(s, "force_kind")


# -- card tools ----------------------------------------------------------------------


class FindCardsInput(BaseModel):
    """Input parameters for find_cards tool."""
    query: str = Field(..., description="Anki card search query, e.g. 'deck:Default' or 'is:due'")
    limit: int = Field(50, ge=1, le=100, description="Max cards to return (1-100, default 50)")
    offset: int = Field(0, ge=0, description="Pagination offset index (default 0)")


class GetCardInput(BaseModel):
    """Input parameters for get_card tool."""
    card_id: int = Field(..., gt=0, description="Unique Anki card ID")


class SuspendCardsInput(_IdListInput):
    """Input parameters for suspend_cards / unsuspend_cards tools."""
    card_ids: List[int] = Field(..., description="Card IDs (1-500 per request)")

    @field_validator("card_ids")
    @classmethod
    def validate_ids(cls, v: List[int]) -> List[int]:
        return cls._validate_id_list(v, "card ID")


class SetDueDateInput(_IdListInput):
    """Input parameters for set_due_date tool."""
    card_ids: List[int] = Field(..., description="Card IDs (1-500 per request)")
    days: str = Field(
        ...,
        pattern=r"^[0-9]+(-[0-9]+)?$",
        description="Days from today, e.g. '3' or a range '0-2' (the scheduler picks randomly)",
    )

    @field_validator("card_ids")
    @classmethod
    def validate_ids(cls, v: List[int]) -> List[int]:
        return cls._validate_id_list(v, "card ID")


class MoveCardsInput(_IdListInput):
    """Input parameters for move_cards tool."""
    card_ids: List[int] = Field(..., description="Card IDs (1-500 per request)")
    deck_id: int = Field(..., gt=0, description="Target deck ID")

    @field_validator("card_ids")
    @classmethod
    def validate_ids(cls, v: List[int]) -> List[int]:
        return cls._validate_id_list(v, "card ID")


class ReviewQueueInput(BaseModel):
    """Input parameters for get_review_queue tool."""
    limit: int = Field(10, ge=1, le=50, description="Max queued cards to fetch (1-50, default 10)")
    deck: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Deck name whose queue to fetch (e.g. 'Korean::Vocab'; subdecks included). "
            "Defaults to the collection's currently selected deck, which on a fresh "
            "collection is 'Default' and usually empty."
        ),
    )


class AnswerCardInput(BaseModel):
    """Input parameters for answer_card tool."""
    card_id: int = Field(..., gt=0, description="Unique Anki card ID (must be currently queued in its deck)")
    rating: int = Field(..., ge=1, le=4, description="1=Again, 2=Hard, 3=Good, 4=Easy")
    deck: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Require the card to be queued in this deck (subdecks included). "
            "By default the card's OWN deck's queue is used, so any due card can be answered."
        ),
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional unique key to prevent duplicate answering on network retries",
    )


class _CardSelectionInput(BaseModel):
    """Shared card_ids-XOR-query selection shape for the phase-4 card tools
    (set_card_flags, bury/unbury, reposition_new_cards).

    Exactly one of ``card_ids`` or ``query`` selects the cards: an explicit ID
    list or an Anki search string, never both and never neither.
    """

    card_ids: Optional[List[int]] = Field(None, description="Card IDs to affect (1-2000 per request); mutually exclusive with query")
    query: Optional[str] = Field(None, description="Anki search selecting the cards, e.g. 'deck:Default' or 'is:due'; mutually exclusive with card_ids")

    @field_validator("card_ids")
    @classmethod
    def validate_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is None:
            return v
        # Same bound and rationale as the note-type change (2000, not the
        # shared _IdListInput 500 cap): each of these tools is ONE backend
        # operation over the whole selection, so per-request cost does not
        # grow per ID the way per-item mutations (delete/tag) do; 2000 keeps
        # a runaway client from repricing the whole collection in one call.
        if not (1 <= len(v) <= 2000):
            raise ValueError("Select between 1 and 2000 card IDs per request.")
        for cid in v:
            if cid <= 0:
                raise ValueError("Card IDs must be positive integers.")
        return v

    @model_validator(mode="after")
    def exactly_one_selection(self) -> "_CardSelectionInput":
        _exactly_one_selection(self.card_ids, self.query)
        return self


class SetCardFlagsInput(_CardSelectionInput):
    """Input parameters for set_card_flags tool."""
    flag: int = Field(
        ...,
        ge=0,
        le=7,
        description="0 clears the flag; 1-7 set it (1-4 are the classic red/orange/green/blue flags, 5-7 the extra user-renameable ones)",
    )


class BuryCardsInput(_CardSelectionInput):
    """Input parameters for bury_cards tool."""


class UnburyCardsInput(_CardSelectionInput):
    """Input parameters for unbury_cards tool."""


class RepositionNewCardsInput(_CardSelectionInput):
    """Input parameters for reposition_new_cards tool."""
    start: int = Field(..., ge=0, description="Queue position the renumbering starts from (becomes new cards' due)")
    step: int = Field(..., ge=1, le=9999, description="Position increment between consecutive new cards")
    randomize: bool = Field(False, description="Assign the positions in random order instead of selection order")
    shift_existing: bool = Field(False, description="Also shift other new cards that occupy the target positions")


# -- media tools -----------------------------------------------------------------------


def validate_media_filename(name: str) -> str:
    """Shared single-segment media-filename rule (like validate_deck_name).

    One implementation serves both media input models and the tools layer, so
    the traversal rule can never drift between them.
    """
    if "/" in name or "\\" in name:
        raise ValueError("Media filename must be a single path segment (no '/' or '\\').")
    if name == "..":
        raise ValueError("Media filename must not be '..'.")
    return _reject_control_chars(name, "Media filename")


class StoreMediaInput(BaseModel):
    """Input parameters for store_media_file tool."""
    filename: str = Field(..., min_length=1, max_length=255, description="Media filename (single path segment, no '/' or '\\', not '..')")
    # 4 MiB of decoded bytes need at most ceil(4194304/3)*4 = 5592408 base64
    # chars; the cap bounds memory before any decoding happens.
    data_base64: str = Field(..., min_length=1, max_length=5_600_000, description="File content, standard base64 (decodes to at most 4 MiB)")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        return validate_media_filename(v)


class GetMediaInput(BaseModel):
    """Input parameters for get_media_file tool."""
    filename: str = Field(..., min_length=1, max_length=255, description="Media filename (single path segment, no '/' or '\\', not '..')")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        return validate_media_filename(v)


class ListMediaInput(BaseModel):
    """Input parameters for list_media_files tool."""
    limit: int = Field(200, ge=1, le=500, description="Max filenames to return (1-500, default 200)")
