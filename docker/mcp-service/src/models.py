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
