"""Web-session cookies, CSRF tokens, and login rate limiting.

Pure logic, stdlib only, Starlette-free so it can be unit tested without an app.

Session cookie (stateless, HMAC-signed). The cookie value is::

    base64url(username) | expiry_unix | nonce | base64url(hmac_sha256(key, payload))

with ``payload = base64url(username) + "." + expiry + "." + nonce`` and the key
derived from the deployment's SESSION_SECRET under the
``anki-mcp/web-session/v1`` domain label, so the same secret can never be
reused as a signing key for another purpose (e.g. the MCP side). The random
nonce makes every issued cookie unique, so two logins for the same account are
not recognizable by cookie equality. The nonce must travel with the cookie for
the MAC to be recomputable, hence four segments where the format sketch in the
task brief lists three.

CSRF tokens are bound to one exact session cookie value: the session's own MAC
(the secret material the cookie already carries) is fed through a second
domain label ``anki-mcp/csrf/v1`` to key an HMAC over the session value. A
token minted for one session never verifies against another, and a cookie the
server never signed yields no usable token. This is not confidential against
whoever holds the cookie (it does not need to be: CSRF defense assumes the
attacker cannot read the cookie, only that they can forge cross-site requests).

LoginRateLimiter implements (username, ip)-scoped consecutive-failure lockout
in an in-memory dict: 5 consecutive failures trigger a 15-minute lockout,
a success clears the record, entries age out of the dict when their lockout
window lapses, and the dict is bounded (oldest entries evicted) so attacker
-controlled key churn cannot grow memory without limit.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("anki_mcp.webauth")

SESSION_KEY_DOMAIN = b"anki-mcp/web-session/v1"
CSRF_KEY_DOMAIN = b"anki-mcp/csrf/v1"
SESSION_COOKIE_NAME = "anki_session"
SESSION_TTL_SECONDS = 12 * 3600
FORCE_SECURE_COOKIES_ENV = "FORCE_SECURE_COOKIES"

FIELD_SEP = "|"
MAC_INPUT_SEP = "."
NONCE_BYTES = 16


def _b64encode(data: bytes) -> str:
    """base64url without padding, so the value is cookie/URL safe."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    """Inverse of _b64encode; re-adds stripped padding. Raises ValueError on bad input."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def derive_session_key(session_secret: str) -> bytes:
    """Derive the cookie-signing key, domain-separated from every other use of the secret."""
    return hmac.new(session_secret.encode("utf-8"), SESSION_KEY_DOMAIN, hashlib.sha256).digest()


def _sign(key: bytes, username_b64: str, expiry: str, nonce: str) -> bytes:
    message = MAC_INPUT_SEP.join((username_b64, expiry, nonce)).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).digest()


def issue_session(session_secret: str, username: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Issue a signed session cookie value for ``username`` valid for ``ttl_seconds``."""
    username_b64 = _b64encode(username.encode("utf-8"))
    expiry = str(int(time.time()) + int(ttl_seconds))
    nonce = _b64encode(secrets.token_bytes(NONCE_BYTES))
    mac = _sign(derive_session_key(session_secret), username_b64, expiry, nonce)
    return FIELD_SEP.join((username_b64, expiry, nonce, _b64encode(mac)))


def verify_session(session_secret: str, value: str) -> Optional[str]:
    """Return the username for a valid, unexpired session cookie value, else None.

    The MAC is checked first (constant-time via ``hmac.compare_digest``);
    expired cookies are rejected. Any malformed input decodes to None rather
    than raising.
    """
    if not session_secret or not value:
        return None
    parts = value.split(FIELD_SEP)
    if len(parts) != 4:
        return None
    username_b64, expiry_str, nonce, mac_b64 = parts
    try:
        expected = _sign(derive_session_key(session_secret), username_b64, expiry_str, nonce)
        provided_mac = _b64decode(mac_b64)
    except (ValueError, TypeError, UnicodeEncodeError):
        # Attacker-controlled segments may contain non-ASCII; the contract is
        # "malformed decodes to None", never an exception in the auth layer.
        return None
    if not hmac.compare_digest(expected, provided_mac):
        return None
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() >= expiry:
        return None
    try:
        return _b64decode(username_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _session_mac(session_value: str) -> bytes:
    """Extract the MAC segment of a session cookie value; ValueError if malformed."""
    parts = session_value.split(FIELD_SEP)
    if len(parts) != 4:
        raise ValueError("malformed session value")
    return _b64decode(parts[3])


def issue_csrf(session_value: str) -> str:
    """Mint the CSRF token bound to this exact session cookie value."""
    csrf_key = hmac.new(_session_mac(session_value), CSRF_KEY_DOMAIN, hashlib.sha256).digest()
    return hmac.new(csrf_key, session_value.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session_value: str, token: str) -> bool:
    """Check a CSRF token against the session it claims to belong to."""
    if not session_value or not isinstance(token, str) or not token:
        return False
    try:
        expected = issue_csrf(session_value)
    except (ValueError, TypeError):
        return False
    # Compare on bytes: str compare_digest rejects non-ASCII with TypeError.
    return hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8"))


def secure_cookies_enabled() -> bool:
    """Whether session cookies should carry the Secure attribute (FORCE_SECURE_COOKIES=true)."""
    return os.getenv(FORCE_SECURE_COOKIES_ENV, "").strip().lower() == "true"


class LoginRateLimiter:
    """Consecutive-failure lockout, keyed by (username, ip).

    Policy: ``max_failures`` consecutive failures lock the key for
    ``lockout_seconds``; a successful login clears the key immediately. Every
    entry also expires ``lockout_seconds`` after its most recent failure, so
    counters age out with inactivity and a lapsed lockout requires a fresh run
    of failures. Entries are pruned lazily on each call and the dict is capped
    at ``max_entries`` (oldest activity evicted first) to bound memory under
    key churn.

    ``now_fn`` defaults to ``time.time`` and is injectable for deterministic
    tests of expiry behavior.
    """

    def __init__(
        self,
        max_failures: int = 5,
        lockout_seconds: float = 15 * 60,
        max_entries: int = 1000,
        now_fn: Callable[[], float] = time.time,
    ):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.max_entries = max_entries
        self._now = now_fn
        self._lock = threading.Lock()
        # (username, ip) -> (consecutive_failures, locked_until, entry_expires_at)
        self._entries: Dict[Tuple[str, str], Tuple[int, float, float]] = {}

    def check(self, username: str, ip: str) -> bool:
        """Return False while the key is locked out, True otherwise."""
        now = self._now()
        with self._lock:
            self._prune(now)
            entry = self._entries.get((username, ip))
            if entry is None:
                return True
            failures, locked_until, _expires_at = entry
            return not (failures >= self.max_failures and now < locked_until)

    def record_failure(self, username: str, ip: str) -> None:
        now = self._now()
        key = (username, ip)
        with self._lock:
            self._prune(now)
            entry = self._entries.get(key)
            failures = (entry[0] if entry else 0) + 1
            locked_until = now + self.lockout_seconds if failures >= self.max_failures else 0.0
            self._entries.pop(key, None)  # pop+reinsert moves the key to eviction-order end
            self._entries[key] = (failures, locked_until, now + self.lockout_seconds)
            self._evict_over_cap()
            if failures == self.max_failures:
                logger.warning(
                    "Login locked out for %r from %s after %d consecutive failures",
                    username, ip, failures,
                )

    def record_success(self, username: str, ip: str) -> None:
        """A successful login clears the key's failure history entirely."""
        with self._lock:
            self._entries.pop((username, ip), None)

    def _prune(self, now: float) -> None:
        """Drop entries whose inactivity window has lapsed."""
        for key in [k for k, v in self._entries.items() if now >= v[2]]:
            del self._entries[key]

    def _evict_over_cap(self) -> None:
        """Evict oldest-activity entries (dict order) until within the cap."""
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))
