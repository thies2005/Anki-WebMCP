"""Tool-disable rules: shared matching helpers + the GLOBAL rule store.

Every MCP tool call is checked against two deny layers:

  * GLOBAL rules — :class:`ToolPolicyStore` at ``<data_dir>/tool_policy.json``,
    set from the admin console, applied to every account (admins included);
  * PER-USER rules — ``User.disabled_tool_patterns`` in the account store,
    set from the portal's Tools page.

A call is refused when EITHER layer matches (deny-only union; there is no
allow-list override, so a user can never re-enable something the admin
disabled).

A rule is either an exact tool name (``delete_notes``) or a trailing-``*``
prefix (``delete_*`` matches delete_deck, delete_notes, delete_note_type,
...). Tool names are flat verb-first snake_case (``list_decks``,
``add_note_type_field``) — there are no dotted namespaces — and substring
matching is deliberately NOT supported: a rule like ``deck`` must be written
``*_deck``-style via explicit prefixes or exact names, never by containment.

Single-instance assumption: the lock is process-internal; run exactly one
mcp-service replica per policy file (same posture as the other JSON stores).
"""

import json
import logging
import os
import re
import tempfile
import threading
from typing import List, Optional

logger = logging.getLogger("anki_mcp.toolpolicy")

# Cap per layer: the store is rewritten whole on every save and rendered into
# a portal page, so both layers need a hard bound.
MAX_RULES = 100

# Exact name ("delete_notes") or trailing-* prefix ("delete_*"). No dots, no
# spaces, no interior wildcards — anything else fails validation so typos
# surface in the UI instead of silently never matching. Length cap: no tool
# name comes close, so an overlong rule is garbage by definition.
_MAX_RULE_LEN = 64
_RULE_RE = re.compile(r"^[a-z0-9_]+\*?$")


def is_valid_pattern(rule: str) -> bool:
    """True when ``rule`` is a well-formed exact-name or prefix rule."""
    return (
        isinstance(rule, str)
        and len(rule) <= _MAX_RULE_LEN
        and _RULE_RE.fullmatch(rule) is not None
    )


def validate_patterns(rules: List[str]) -> List[str]:
    """Normalize + validate a rule list; raises ValueError on a bad entry.

    Strips whitespace, drops empty lines, de-duplicates preserving first
    occurrence, and caps the list at MAX_RULES.
    """
    cleaned: List[str] = []
    seen = set()
    for raw in rules:
        rule = raw.strip() if isinstance(raw, str) else ""
        if not rule:
            continue
        if not is_valid_pattern(rule):
            raise ValueError(
                f"Invalid tool rule {raw!r}: use an exact tool name (delete_notes) "
                "or a trailing-* prefix (delete_*)."
            )
        if rule not in seen:
            seen.add(rule)
            cleaned.append(rule)
    if len(cleaned) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} tool rules are allowed.")
    return cleaned


def parse_pattern_text(text: str) -> List[str]:
    """Portal form input (one rule per line) -> validated rule list."""
    return validate_patterns(text.splitlines())


def rule_matches(rule: str, tool_name: str) -> bool:
    """Exact match, or prefix match when the rule ends with ``*``."""
    if rule.endswith("*"):
        return tool_name.startswith(rule[:-1])
    return rule == tool_name


def matching_rule(tool_name: str, rules: List[str]) -> Optional[str]:
    """The first rule in ``rules`` that matches ``tool_name``, or None."""
    for rule in rules:
        if rule_matches(rule, tool_name):
            return rule
    return None


class ToolPolicyStore:
    """Thread-safe JSON-backed store for the global (admin) rule layer.

    Persistence mirrors :class:`src.users.UserStore` (atomic
    mkstemp -> fsync -> os.replace). A missing file is an empty policy; a
    corrupt file logs and starts empty — an unreadable GLOBAL policy must not
    take the whole service down the way an unreadable account store would
    (that file fails closed at boot by design; this one gates an optional
    feature).
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._patterns: List[str] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("disabled_tool_patterns", []) if isinstance(data, dict) else []
            if isinstance(raw, list):
                # Tolerant: drop anything malformed rather than refusing boot.
                self._patterns = [r for r in raw if isinstance(r, str) and is_valid_pattern(r)]
                logger.info(
                    "Loaded %d global tool rule(s) from %s", len(self._patterns), self.file_path
                )
            else:
                logger.warning("Malformed tool policy at %s; starting with no rules", self.file_path)
        except (OSError, ValueError) as e:
            logger.error("Unreadable tool policy at %s (%s); starting with no rules", self.file_path, e)

    def _save_atomic(self) -> None:
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=dir_name if dir_name else ".", prefix="toolpolicy_", suffix=".tmp"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump({"disabled_tool_patterns": self._patterns}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.file_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to atomically save tool policy to %s: %s", self.file_path, e)
            raise

    def get_patterns(self) -> List[str]:
        """The global rules as a copy (callers never mutate store state)."""
        with self._lock:
            return list(self._patterns)

    def set_patterns(self, patterns: List[str]) -> List[str]:
        """Validate and persist the global rule list; returns the stored list.

        Raises ValueError for malformed rules or a rule count over MAX_RULES.
        """
        validated = validate_patterns(patterns)
        with self._lock:
            self._patterns = validated
            self._save_atomic()
        logger.info("Global tool rules updated: %d pattern(s)", len(validated))
        return list(validated)
