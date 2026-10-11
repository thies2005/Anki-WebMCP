"""Data models and input validation schemas for Anki MCP service."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class NoteInput(BaseModel):
    """Specification of a new note to create."""
    deck_name: str = Field(..., description="Name of target deck, e.g. 'Default'")
    model_name: str = Field(..., description="Note type model name, e.g. 'Basic' or 'Cloze'")
    fields: Dict[str, str] = Field(..., description="Field names and their string values")
    tags: List[str] = Field(default_factory=list, description="Tags to assign to the note")

    @field_validator("deck_name", "model_name")
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


class AnswerCardInput(BaseModel):
    """Input parameters for answer_card tool."""
    card_id: int = Field(..., gt=0, description="Unique Anki card ID (must be in the current review queue)")
    rating: int = Field(..., ge=1, le=4, description="1=Again, 2=Hard, 3=Good, 4=Easy")
    idempotency_key: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional unique key to prevent duplicate answering on network retries",
    )


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
