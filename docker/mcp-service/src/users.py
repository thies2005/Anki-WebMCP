"""Multi-user account store for the headless Anki MCP service.

Accounts live in a single JSON file (default ``${DATA_DIR}/users.json``). Only
derived secret material is persisted: scrypt password hashes and sha256 token
hashes. Raw MCP tokens and raw passwords are surfaced once at creation time
and never stored, so the file alone grants nothing to an attacker.
"""

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
from typing import Dict, List, Optional, Tuple

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


def _burn_scrypt(password: str) -> None:
    """Do scrypt work against a throwaway hash to flatten timing for unknown users."""
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = hash_password("timing-equalizer")
    verify_password_hash(password, _dummy_password_hash)


@dataclass
class User:
    """A single account record. Secrets are stored only in derived form."""

    username: str
    password_hash: str  # format: scrypt$N$r$p$salt_hex$hash_hex
    token_hash: str  # sha256 hex of the raw MCP token
    role: str  # "admin" | "user"
    ankiweb_email: str = ""
    ankiweb_password: str = ""
    created_at: float = field(default_factory=time.time)


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
                    ankiweb_email=record.get("ankiweb_email", ""),
                    ankiweb_password=record.get("ankiweb_password", ""),
                    created_at=float(record.get("created_at", 0.0)),
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
        """All account records, as copies. Raw tokens are never stored, so none are exposed."""
        with self._lock:
            return [replace(user) for user in self._users.values()]

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

        The raw token is returned exactly once and only its sha256 is stored.
        Raises ValueError on duplicate username, invalid username, or invalid role.
        """
        _validate_username(username)
        if role not in ROLES:
            raise ValueError(f"Invalid role {role!r}: must be one of {ROLES}")
        with self._lock:
            if username in self._users:
                raise ValueError(f"Username already exists: {username!r}")
            raw_token = secrets.token_urlsafe(TOKEN_BYTES)
            user = User(
                username=username,
                password_hash=hash_password(password),
                token_hash=hash_token(raw_token),
                role=role,
            )
            self._users[username] = user
            self._save_atomic()
        logger.info("Created %s account %r", role, username)
        return user, raw_token

    def regenerate_token(self, username: str) -> str:
        """Issue a new raw MCP token for the account, invalidating the old one."""
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            user = self._get_locked(username)
            user.token_hash = hash_token(raw_token)
            self._save_atomic()
        logger.info("Regenerated MCP token for %r", username)
        return raw_token

    def set_password(self, username: str, new_password: str) -> None:
        with self._lock:
            user = self._get_locked(username)
            user.password_hash = hash_password(new_password)
            self._save_atomic()
        logger.info("Updated password for %r", username)

    def set_ankiweb_credentials(self, username: str, email: str, password: str) -> None:
        with self._lock:
            user = self._get_locked(username)
            user.ankiweb_email = email
            user.ankiweb_password = password
            self._save_atomic()
        logger.info("Updated stored AnkiWeb credentials for %r", username)

    def delete_user(self, username: str) -> bool:
        """Remove the account record only; data directories are NOT touched here."""
        with self._lock:
            if username not in self._users:
                return False
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
