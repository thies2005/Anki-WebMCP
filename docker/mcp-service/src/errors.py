"""Shared exception types for the MCP tool layer.

The deleted AnkiConnect client (``AnkiConnectError`` et al.) was the previous
carrier for "expected" tool failures; with tools talking to the per-user
collection registry directly, this module owns that role instead. Tool
wrappers in ``src/main.py`` convert these into the structured
``{"success": False, "error": ..., "error_type": ...}`` response, so the
MCP SDK's generic "Error executing tool" mask never hides the root cause.
"""


class CollectionToolError(Exception):
    """An expected, client-facing failure of a collection operation.

    Raised for validation that only the collection layer can perform:
    missing notes, unknown note types, unknown fields, duplicate detection,
    empty mutation batches. Never raised for transport problems (the registry
    talks to a local SQLite collection; there is no connection layer left).
    """
