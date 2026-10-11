"""Media file storage, retrieval, and listing tools.

Path safety: ``_validate_media_filename`` only accepts a SINGLE path segment
(no ``/``, no ``\\``, not ``..``, no control characters), so a validated name
joined onto the media directory can never escape it. File bytes are never
logged — log lines carry filenames and sizes only.

Size policy: uploads larger than 4 MiB decoded and downloads larger than
4 MiB are refused (the MCP surface is not a bulk-transfer channel); the same
cap bounds both memory copies of one payload.
"""

import base64
import logging
import os
from typing import Any, Dict

from ..errors import CollectionToolError
from ..models import MutationResponse, SyncStatus, validate_media_filename
from ..sync_manager import SyncManager

logger = logging.getLogger("anki_mcp.tools.media")

_MEDIA_MAX_BYTES = 4 * 1024 * 1024
_FILENAME_MAX_CHARS = 255


def _validate_media_filename(filename: str) -> str:
    """Accept only a safe single-segment media filename; raise CollectionToolError otherwise.

    Delegates the separator/``..``/control-char rule to the shared
    models.validate_media_filename so the rule can never drift between the
    pydantic layer and the tools layer; adds the empty/length checks this
    layer needs and converts to the tools' error type. Rejection happens
    BEFORE any filesystem or media-map access.
    """
    if not filename or not filename.strip():
        raise CollectionToolError("Media filename must not be empty.")
    if len(filename) > _FILENAME_MAX_CHARS:
        raise CollectionToolError(f"Media filename is limited to {_FILENAME_MAX_CHARS} characters.")
    try:
        return validate_media_filename(filename)
    except ValueError as e:
        raise CollectionToolError(str(e)) from e


def _decode_media_payload(data_base64: str) -> bytes:
    """Decode and size-cap a base64 media payload."""
    try:
        data = base64.b64decode(data_base64, validate=True)
    except ValueError as e:
        raise CollectionToolError(f"data_base64 is not valid base64: {e}") from e
    if len(data) > _MEDIA_MAX_BYTES:
        raise CollectionToolError(
            f"Media payload is too large: {len(data)} bytes decoded (limit is {_MEDIA_MAX_BYTES})."
        )
    return data


async def store_media_file(
    sync_manager: SyncManager,
    registry,
    username: str,
    filename: str,
    data_base64: str,
) -> MutationResponse:
    """Store a media file for the user's collection; returns the ACTUAL stored name.

    The wheel sanitizes names on write (e.g. 'a/b.txt' becomes 'ab.txt'), so
    the response carries the post-sanitization filename from
    ``col.media.write_data``. Not destructive — no snapshot.
    """
    _validate_media_filename(filename)  # before the mutation: no I/O on bad input
    data = _decode_media_payload(data_base64)
    logger.info("Executing store_media_file for %r: %r (%d bytes)", username, filename, len(data))

    def _store(col) -> Dict[str, Any]:
        actual_name = col.media.write_data(filename, data)
        return {"filename": actual_name}

    async def _mutation() -> Dict[str, Any]:
        return await registry.run(username, _store)

    return await sync_manager.execute_mutation(
        username,
        operation_name="store_media_file",
        mutation_fn=_mutation,
    )


async def get_media_file(registry, username: str, filename: str) -> Dict[str, Any]:
    """Read a media file back as base64 (files over 4 MiB are refused)."""
    _validate_media_filename(filename)
    logger.info("Executing get_media_file for %r: %r", username, filename)

    def _read(col) -> Dict[str, Any]:
        path = os.path.join(col.media.dir(), filename)
        if not os.path.isfile(path):
            raise CollectionToolError(f"Media file {filename!r} not found in collection media.")
        size = os.path.getsize(path)
        if size > _MEDIA_MAX_BYTES:
            raise CollectionToolError(
                f"Media file {filename!r} is too large to return ({size} bytes; "
                f"limit is {_MEDIA_MAX_BYTES})."
            )
        with open(path, "rb") as f:
            data = f.read()
        return {
            "filename": filename,
            "size_bytes": size,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }

    return await registry.run(username, _read)


async def list_media_files(registry, username: str, limit: int = 200) -> Dict[str, Any]:
    """List the user's media filenames (sorted), capped at ``limit`` entries."""
    limit = max(1, min(limit, 500))
    logger.info("Executing list_media_files for %r: limit=%d", username, limit)

    def _list(col) -> Dict[str, Any]:
        names = sorted(os.listdir(col.media.dir()))
        return {
            "count": min(len(names), limit),
            "total": len(names),
            "truncated": len(names) > limit,
            "files": names[:limit],
        }

    return await registry.run(username, _list)
