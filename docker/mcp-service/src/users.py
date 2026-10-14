"""Multi-user account store for the headless Anki MCP service.

Accounts live in a single JSON file (default ``${DATA_DIR}/users.json``). Raw
passwords are never persisted (scrypt hashes only). MCP tokens are persisted
twice by design:

- ``token_hash`` (sha256): what ``authenticate_token`` verifies against — the
  constant-time auth path never touches plaintext.
- ``token_plaintext`` (standard base64 of the raw token): the token portal must
  RE-display the token on demand, so hash-only storage is not an option; a
  must-redisplay secret cannot get one-way/scrypt-grade protection. Storing it
  reversibly in the store file is the accepted trade-off: the file is already
  0600, lives on the service's data volume, and its bytes are therefore
  equivalent in sensitivity to the raw tokens themselves. Records created
  before ``token_plaintext`` existed simply leave it empty; the portal shows
  "regenerate to view" for them.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .toolpolicy import is_valid_pattern, validate_patterns

logger = logging.getLogger("anki_mcp.users")

# Path-safety guard: usernames become directory names under DATA_DIR in later
# tasks, so anything outside this strict alphabet is rejected outright.
# Matched with fullmatch() so `$`-before-trailing-newline quirks cannot slip
# a name like "alice\n" through.
USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
HASH_PREFIX = "scrypt"
TOKEN_BYTES = 32
ROLES = ("admin", "user")

# Lazily-built throwaway hash so verifying a password for an unknown username
# costs roughly the same scrypt work as for a real one (no timing oracle for
# account enumeration).
_dummy_password_hash: Optional[str] = None


def _validate_username(username: str) -> str:
    """Return the username if path-safe, else raise ValueError."""
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ValueError(
            f"Invalid username {username!r}: must match ^[a-z0-9_-]{{1,32}}$ "
            "(usernames are used as directory names and must be path-safe)"
        )
    return username


def hash_password(password: str) -> str:
    """Hash a password with scrypt; returns ``scrypt$N$r$p$salt_hex$hash_hex``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"{HASH_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password_hash(password: str, encoded: str) -> bool:
    """Constant-time verification of a password against a stored hash string.

    Malformed or foreign-format encodings verify to False instead of raising,
    so a corrupted store degrades to "no login" rather than a crash.
    """
    try:
        prefix, n_str, r_str, p_str, salt_hex, hash_hex = encoded.split("$")
        if prefix != HASH_PREFIX:
            return False
        # Only the parameters we issue are accepted; a tampered store must not be
        # able to demand an arbitrarily expensive scrypt run (memory DoS).
        if int(n_str) != SCRYPT_N or int(r_str) != SCRYPT_R or int(p_str) != SCRYPT_P:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def hash_token(raw_token: str) -> str:
    """Derive the storable form of an MCP token (sha256 hex of the raw value)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _encode_token(raw_token: str) -> str:
    """base64 form of a raw MCP token for the ``token_plaintext`` field (see module docstring)."""
    return base64.b64encode(raw_token.encode("utf-8")).decode("ascii")


def _decode_token(encoded: str) -> Optional[str]:
    """Inverse of _encode_token; None when the stored value is absent/corrupt."""
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        return None


def _burn_scrypt(password: str) -> None:
    """scrypt work against a throwaway hash to flatten timing for unknown users."""
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = hash_password("timing-equalizer")
    verify_password_hash(password, _dummy_password_hash)


def _load_tool_patterns(record: Dict) -> List[str]:
    """Tolerant read of a record's disabled_tool_patterns (absent/garbage -> [])."""
    raw = record.get("disabled_tool_patterns", [])
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str) and is_valid_pattern(r)]


@dataclass
class User:
    """A single account record. Passwords are stored only in derived form.

    ``token_plaintext`` is the reversible base64 form of the MCP token (see the
    module docstring for why it exists); empty for records created before the
    field was introduced.
    """

    username: str
    password_hash: str  # format: scrypt$N$r$p$salt_hex$hash_hex
    token_hash: str  # sha256 hex of the raw MCP token
    role: str  # "admin" | "user"
    token_plaintext: str = ""  # base64 of the raw MCP token; "" for legacy records
    ankiweb_email: str = ""
    ankiweb_password: str = ""
    created_at: float = field(default_factory=time.time)
    # Session-revocation epoch: web session cookies carry a signed issued-at
    # stamp (src/webauth.py), and a cookie issued BEFORE this value is
    # rejected. Bumped by set_password so a password reset evicts outstanding
    # sessions. 0.0 (legacy records, no reset yet) accepts every cookie.
    sessions_invalid_before: float = 0.0
    # Token/grant-revocation epoch: OAuth refresh grants (src/oauth.py) issued
    # BEFORE this value are rejected at rotation. Bumped by regenerate_token
    # so regenerating the MCP token also evicts outstanding connector grants
    # (they would otherwise keep minting the fresh token forever). Password
    # resets evict grants too, via sessions_invalid_before above. 0.0 accepts
    # every grant (legacy records).
    tokens_invalid_before: float = 0.0
    # Per-user layer of the tool-disable gate: exact tool names or trailing-*
    # prefixes (see src/toolpolicy.py). Enforced by the _tool_guard wrapper in
    # src.main on every MCP tool call; edited on the portal's Tools page.
    # Empty for records written before the field existed (no rules).
    disabled_tool_patterns: List[str] = field(default_factory=list)


class UserStore:
    """Thread-safe JSON-backed account store with atomic persistence.

    Persistence mirrors :class:`src.ledger.IdempotencyLedger`: write to a
    ``mkstemp`` file in the same directory (created 0o600 by ``mkstemp``),
    fsync, then ``os.replace`` so a crash can never leave a torn file behind.

    Single-instance assumption: the lock is process-internal; run exactly one
    mcp-service replica per users file.

    A missing file is an empty store (first start). An unreadable/corrupt file
    raises: for an auth store, silently starting from an empty account set
    would let a subsequent bootstrap overwrite every existing account.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._users: Dict[str, User] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.file_path):
            logger.info("No user store at %s; starting with an empty account set", self.file_path)
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f"User store at {self.file_path} is unreadable or corrupt: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(
                f"User store at {self.file_path} has unexpected format (expected a JSON object)"
            )
        users: Dict[str, User] = {}
        for key, record in data.items():
            try:
                user = User(
                    username=record["username"],
                    password_hash=record["password_hash"],
                    token_hash=record["token_hash"],
                    role=record["role"],
                    token_plaintext=record.get("token_plaintext", ""),
                    ankiweb_email=record.get("ankiweb_email", ""),
                    ankiweb_password=record.get("ankiweb_password", ""),
                    created_at=float(record.get("created_at", 0.0)),
                    # Tolerant load: records written before the session epoch
                    # existed simply start with "no revocation" (0.0).
                    sessions_invalid_before=float(
                        record.get("sessions_invalid_before", 0.0)
                    ),
                    tokens_invalid_before=float(
                        record.get("tokens_invalid_before", 0.0)
                    ),
                    # Tolerant load of the per-user tool-disable rules: absent
                    # (legacy records) or malformed values both mean "no rules"
                    # rather than a fatal store, matching the global policy
                    # store's corrupt-file posture.
                    disabled_tool_patterns=_load_tool_patterns(record),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"User store at {self.file_path} has a malformed record for {key!r}: {e}"
                ) from e
            if user.username != key:
                raise ValueError(
                    f"User store at {self.file_path}: record key {key!r} does not match "
                    f"recorded username {user.username!r}"
                )
            users[key] = user
        self._users = users
        logger.info("Loaded %d user accounts from %s", len(users), self.file_path)

    def _save_atomic(self) -> None:
        """Persist accounts atomically (mkstemp in the same dir + os.replace)."""
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(
            dir=dir_name if dir_name else ".", prefix="users_", suffix=".tmp"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(
                    {user.username: asdict(user) for user in self._users.values()}, f, indent=2
                )
                f.flush()
                os.fsync(f.fileno())
            # mkstemp already creates the file 0o600; re-assert in case of umask oddities.
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.file_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to atomically save user store to %s: %s", self.file_path, e)
            raise

    # -- queries -------------------------------------------------------------

    def list_users(self) -> List[User]:
        """All account records, as copies. Raw passwords are never stored; the
        copies carry ``token_plaintext`` if present, so callers must treat them
        as secret-bearing."""
        with self._lock:
            return [replace(user) for user in self._users.values()]

    def get(self, username: str) -> Optional[User]:
        """One account record as a copy, or None when the username is unknown.

        Copy semantics mirror list_users: callers can never mutate the store's
        internal state through the returned record.
        """
        with self._lock:
            user = self._users.get(username)
            return replace(user) if user is not None else None

    def get_user(self, username: str) -> Optional[User]:
        """One account record as a copy, or None (additive alias of :meth:`get`).

        The Task 5 sync manager names this accessor when it reads a user's
        AnkiWeb credentials; it exists so call sites read symmetrically with
        ``authenticate_token`` rather than reaching for the terser ``get``.
        """
        return self.get(username)

    def peek_token(self, username: str) -> Optional[str]:
        """Return the account's RAW MCP token, or None when unavailable.

        Reads the reversibly-stored ``token_plaintext`` field (see module
        docstring). Returns None for unknown usernames and for legacy records
        created before the field existed — the portal renders "regenerate to
        view" for those. Authentication NEVER depends on this method.
        """
        with self._lock:
            user = self._users.get(username)
            encoded = user.token_plaintext if user is not None else ""
        return _decode_token(encoded)

    def verify_password(self, username: str, password: str) -> bool:
        with self._lock:
            user = self._users.get(username)
        if user is None:
            _burn_scrypt(password)
            return False
        return verify_password_hash(password, user.password_hash)

    def authenticate_token(self, raw_token: str) -> Optional[User]:
        """Resolve a raw MCP token to its account, or None.

        The candidate hash is compared against every account via
        ``hmac.compare_digest`` — linear scan, no early exit, no prefix or
        length shortcut — so response timing does not leak which account (if
        any) is closest to matching.
        """
        if not raw_token:
            return None
        candidate = hash_token(raw_token)
        match: Optional[User] = None
        with self._lock:
            for user in self._users.values():
                try:
                    matched = hmac.compare_digest(candidate, user.token_hash)
                except TypeError:
                    matched = False
                if matched:
                    match = replace(user)
        return match

    # -- mutations -----------------------------------------------------------

    def create_user(self, username: str, password: str, role: str = "user") -> Tuple[User, str]:
        """Create an account; returns ``(user, raw_token)``.

        The raw token is returned exactly once by this method's return value,
        but it IS stored (base64 in ``token_plaintext``) so the portal can
        re-display it; only its sha256 is used for authentication.
        Raises ValueError on duplicate username, invalid username, or invalid role.
        """
        _validate_username(username)
        if role not in ROLES:
            raise ValueError(f"Invalid role {role!r}: must be one of {ROLES}")
        with self._lock:
            if username in self._users:
                raise ValueError(f"Username already exists: {username!r}")
            raw_token = secrets.token_urlsafe(TOKEN_BYTES)
            # Audit F-01: a fresh record starts with BOTH revocation epochs AT
            # its creation instant, so cookies, OAuth grants and codes minted
            # for a previous account that reused this username predate the new
            # account's epochs and are rejected. Whole-second resolution is
            # merely conservative: a lower floor can only accept MORE cookies
            # (never a legitimate pre-creation one, which still predates it),
            # and the account's very first login always postdates it.
            # set_password and regenerate_token keep stamping sub-second epochs.
            now = float(int(time.time()))
            user = User(
                username=username,
                password_hash=hash_password(password),
                token_hash=hash_token(raw_token),
                role=role,
                token_plaintext=_encode_token(raw_token),
                created_at=now,
                sessions_invalid_before=now,
                tokens_invalid_before=now,
            )
            self._users[username] = user
            self._save_atomic()
        logger.info("Created %s account %r", role, username)
        return user, raw_token

    def regenerate_token(self, username: str) -> str:
        """Issue a new raw MCP token for the account, invalidating the old one.

        The raw value is returned for the one-time reveal and stored reversibly
        (token_plaintext) for later portal display; auth keeps using token_hash.
        Regeneration also stamps the token epoch: OAuth refresh grants issued
        before now are rejected at their next rotation, so a leaked connector
        grant cannot keep minting the fresh token.
        """
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            user = self._get_locked(username)
            user.token_hash = hash_token(raw_token)
            user.token_plaintext = _encode_token(raw_token)
            user.tokens_invalid_before = time.time()
            self._save_atomic()
        logger.info("Regenerated MCP token for %r (older OAuth grants invalidated)", username)
        return raw_token

    def set_password(self, username: str, new_password: str) -> None:
        with self._lock:
            user = self._get_locked(username)
            user.password_hash = hash_password(new_password)
            # Session revocation: stamp the epoch so web cookies issued before
            # now stop verifying (main.get_session_user consults this via
            # webauth.session_issued_before). This is the eviction step that
            # makes a password reset actually terminate outstanding sessions.
            user.sessions_invalid_before = time.time()
            self._save_atomic()
            # Audit F-12: the generated first-boot admin credentials file
            # (initial-admin-password.txt, written by main next to this store)
            # is a stale plaintext secret once the admin has set their own
            # password — remove it. missing_ok keeps the common cases (explicit
            # ADMIN_PASSWORD, non-admin users, already deleted) silent, and an
            # OSError other than absence is logged rather than raised so a
            # wedged volume cannot turn a password change into an error.
            if user.role == "admin":
                bootstrap_path = Path(self.file_path).parent / "initial-admin-password.txt"
                if bootstrap_path.exists():
                    try:
                        bootstrap_path.unlink(missing_ok=True)
                        logger.info(
                            "Removed bootstrap credentials file %s after password change", bootstrap_path
                        )
                    except OSError as e:
                        logger.warning(
                            "Could not remove bootstrap credentials file %s after "
                            "password change: %s",
                            bootstrap_path,
                            e,
                        )
        logger.info("Updated password for %r (older web sessions invalidated)", username)

    def bump_sessions_invalid_before(self, username: str) -> None:
        """Stamp the account's session-revocation epoch NOW and save atomically.

        Audit F-13: logout calls this so a session cookie copied before the
        logout dies server-side — cookie deletion alone only helps the one
        browser that performed it. Same eviction step a password reset takes
        (set_password stamps the same epoch), without touching the password
        hash or the MCP token. Raises the same ValueError as the other
        mutators for an unknown username (callers resolve the account first).
        """
        with self._lock:
            user = self._get_locked(username)
            user.sessions_invalid_before = time.time()
            self._save_atomic()
        logger.info("Bumped session-revocation epoch for %r (older web sessions invalidated)", username)

    def set_ankiweb_credentials(self, username: str, email: str, password: str) -> None:
        with self._lock:
            user = self._get_locked(username)
            user.ankiweb_email = email
            user.ankiweb_password = password
            self._save_atomic()
        logger.info("Updated stored AnkiWeb credentials for %r", username)

    def set_disabled_tool_patterns(self, username: str, patterns: List[str]) -> List[str]:
        """Set the account's tool-disable rules (per-user gate layer).

        Validates through the shared rule grammar (exact name or trailing-*
        prefix; raises ValueError on malformed entries), stores the normalized
        list, and returns it.
        """
        validated = validate_patterns(patterns)
        with self._lock:
            user = self._get_locked(username)
            user.disabled_tool_patterns = validated
            self._save_atomic()
        logger.info("Updated disabled-tool rules for %r: %d pattern(s)", username, len(validated))
        return list(validated)

    def set_role(self, username: str, role: str) -> None:
        """Promote/demote an account between ``"admin"`` and ``"user"``.

        Raises ValueError for unknown usernames or an invalid role, and when a
        demotion would leave zero admin accounts. The last-admin guard runs
        under the same lock acquisition that commits the mutation, so
        concurrent demotions cannot interleave check and commit and strip
        every admin (handlers may keep a friendlier pre-check outside).
        """
        if role not in ROLES:
            raise ValueError(f"Invalid role {role!r}: must be one of {ROLES}")
        with self._lock:
            user = self._get_locked(username)
            if (
                user.role == "admin"
                and role != "admin"
                and sum(1 for u in self._users.values() if u.role == "admin") <= 1
            ):
                raise ValueError(
                    f"Cannot demote {username!r}: it is the last admin account. "
                    "Promote another account to admin first."
                )
            user.role = role
            self._save_atomic()
        logger.info("Set role of %r to %r", username, role)

    def delete_user(self, username: str) -> bool:
        """Remove the account record only; data directories are NOT touched here.

        Raises ValueError when deleting would leave zero admin accounts (the
        guard and the deletion commit atomically under the store lock).
        """
        with self._lock:
            user = self._users.get(username)
            if user is None:
                return False
            if user.role == "admin" and sum(
                1 for u in self._users.values() if u.role == "admin"
            ) <= 1:
                raise ValueError(
                    f"Cannot delete {username!r}: it is the last admin account."
                )
            del self._users[username]
            self._save_atomic()
        logger.info("Deleted account %r (per-user data directories are untouched)", username)
        return True

    def bootstrap_admin(self, username: str, password: str) -> bool:
        """Create the first admin account; no-op (returns False) if ANY user exists.

        The guard and the creation run under the store lock, so concurrent
        callers cannot both bootstrap. The generated raw token is intentionally
        discarded: the admin authenticates by password (Task 2 web login) and
        can issue themselves a token via regenerate_token.
        """
        with self._lock:
            if self._users:
                return False
            # RLock is re-entrant: create_user re-acquires the lock, validates
            # the username/role, persists, and logs (username only).
            self.create_user(username, password, role="admin")
            return True

    # -- internals -----------------------------------------------------------

    def _get_locked(self, username: str) -> User:
        """Caller must hold the lock. Raises ValueError for unknown usernames."""
        user = self._users.get(username)
        if user is None:
            raise ValueError(f"Unknown username: {username!r}")
        return user
