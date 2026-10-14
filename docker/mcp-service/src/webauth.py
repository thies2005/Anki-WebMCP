"""Web-session cookies, CSRF tokens, and login rate limiting.

Pure logic, stdlib only, Starlette-free so it can be unit tested without an app.

Session cookie (stateless, HMAC-signed). The cookie value is::

    base64url(username) | issued_at_unix | expiry_unix | nonce | base64url(hmac_sha256(key, payload))

with ``payload = base64url(username) + "." + issued_at + "." + expiry + "." + nonce``
and the key derived from the deployment's SESSION_SECRET under the
``anki-mcp/web-session/v1`` domain label, so the same secret can never be
reused as a signing key for another purpose (e.g. the MCP side). The random
nonce makes every issued cookie unique, so two logins for the same account are
not recognizable by cookie equality; the nonce must travel with the cookie for
the MAC to be recomputable, hence five segments.

The MAC-covered ``issued_at`` segment records when the cookie was minted so a
per-user revocation epoch can reject stale cookies:
``User.sessions_invalid_before`` (users.py) is bumped on password reset, and
:func:`session_issued_before` parses the issued-at stamp so the caller can
reject cookies issued before the epoch. Cookies without the segment (legacy
four-segment values) fail closed: verify_session rejects them outright, and
the parser returns None.

CSRF tokens are bound to one exact session cookie value: the session's own MAC
(the secret material the cookie already carries) is fed through a second
domain label ``anki-mcp/csrf/v1`` to key an HMAC over the session value. A
token minted for one session never verifies against another, and a cookie the
server never signed yields no usable token. This is not confidential against
whoever holds the cookie (it does not need to be: CSRF defense assumes the
attacker cannot read the cookie, only that they can forge cross-site requests).

LoginRateLimiter implements consecutive-failure lockout in an in-memory dict
along two budget dimensions: per-account ``(username, ip)`` keys (5
consecutive failures) and a per-source aggregate ``("", ip)`` key (30
failures regardless of username, bounding the password-verification work an
attacker can force from one address by rotating usernames). Both share the
same 15-minute lockout window, entries age out with inactivity, and the dict
is bounded (oldest entries evicted) so attacker-controlled key churn cannot
grow memory without limit.
"""

import base64
import hashlib
import hmac
import logging
import math
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


def _sign(key: bytes, username_b64: str, issued_at: str, expiry: str, nonce: str) -> bytes:
    message = MAC_INPUT_SEP.join((username_b64, issued_at, expiry, nonce)).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).digest()


def issue_session(session_secret: str, username: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Issue a signed session cookie value for ``username`` valid for ``ttl_seconds``.

    The signed payload carries the issue time at microsecond resolution so the
    per-user revocation epoch can reject pre-reset cookies — including a login
    completing in the SAME wall-clock second as a password reset, where a
    whole-second stamp would floor below the reset's fractional
    ``User.sessions_invalid_before`` epoch; see :func:`session_issued_before`.
    """
    username_b64 = _b64encode(username.encode("utf-8"))
    now_f = round(time.time(), 6)
    issued_at = f"{now_f:.6f}"
    expiry = str(int(now_f) + int(ttl_seconds))
    nonce = _b64encode(secrets.token_bytes(NONCE_BYTES))
    mac = _sign(derive_session_key(session_secret), username_b64, issued_at, expiry, nonce)
    return FIELD_SEP.join((username_b64, issued_at, expiry, nonce, _b64encode(mac)))


def verify_session(session_secret: str, value: str) -> Optional[str]:
    """Return the username for a valid, unexpired session cookie value, else None.

    The MAC is checked first (constant-time via ``hmac.compare_digest``);
    expired cookies are rejected. Any malformed input decodes to None rather
    than raising. Legacy cookies without the issued-at segment fail closed
    (segment count mismatch), so rotating the format invalidates every
    previously issued value — all cookies are reissued on login.
    """
    if not session_secret or not value:
        return None
    parts = value.split(FIELD_SEP)
    if len(parts) != 5:
        return None
    username_b64, issued_at, expiry_str, nonce, mac_b64 = parts
    try:
        expected = _sign(
            derive_session_key(session_secret), username_b64, issued_at, expiry_str, nonce
        )
        provided_mac = _b64decode(mac_b64)
    except (ValueError, TypeError, UnicodeEncodeError):
        # Attacker-controlled segments may contain non-ASCII; the contract is
        # "malformed decodes to None", never an exception in the auth layer.
        return None
    if not hmac.compare_digest(expected, provided_mac):
        return None
    try:
        issued = float(issued_at)  # integer (legacy) or decimal stamp (MAC covers it)
        expiry = float(expiry_str)
    except ValueError:
        return None
    if not (math.isfinite(issued) and math.isfinite(expiry)):
        return None  # NaN/inf would defeat the expiry comparison — fail closed
    if time.time() >= expiry:
        return None
    try:
        return _b64decode(username_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def session_issued_before(session_value: str) -> Optional[float]:
    """Parse the signed issued-at stamp out of a session cookie value.

    Returns the issue time as epoch seconds (float), or None when the value is
    missing, malformed, or a legacy cookie without the issued-at segment — so
    callers fail closed by treating None as "reject". The returned stamp is
    only trustworthy after :func:`verify_session` accepted the same value (the
    issued-at segment is MAC-covered, but this parser does not re-verify the
    MAC); callers compare it against ``User.sessions_invalid_before`` and
    reject the cookie when ``issued_at < sessions_invalid_before``.
    """
    if not session_value:
        return None
    parts = session_value.split(FIELD_SEP)
    if len(parts) != 5:
        return None
    try:
        issued_at = float(parts[1])  # integer (legacy) or decimal stamp
    except ValueError:
        return None
    if not math.isfinite(issued_at):
        return None  # NaN/inf always compare False below — fail closed instead
    return issued_at


def _session_mac(session_value: str) -> bytes:
    """Extract the MAC segment of a session cookie value; ValueError if malformed."""
    parts = session_value.split(FIELD_SEP)
    if len(parts) != 5:
        raise ValueError("malformed session value")
    return _b64decode(parts[4])


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
    """Consecutive-failure lockout along two budget dimensions.

    Per-account: the ``(username, ip)`` key locks after ``max_failures``
    consecutive failures (protects individual accounts from brute force).
    Per-source aggregate: the ``("", ip)`` key locks after ``max_ip_failures``
    failures from one address regardless of username (bounds the unauthenticated
    password-verification work an attacker can force by rotating usernames —
    each fresh username would otherwise open a fresh per-account key and the
    gate would never engage). Both keys share ``lockout_seconds`` and a success
    clears only the ``(username, ip)`` key: the IP bucket is NOT reset by
    successes, so an attacker cannot revive the aggregate budget by interleaving
    an occasional (guessed) success into a username rotation — it ages out only
    after ``lockout_seconds`` of inactivity.

    NAT tradeoff: every user behind the same source address (home router,
    carrier-grade NAT, office egress) shares the one aggregate bucket. It is
    deliberately sized above the per-account threshold (30 vs 5) so a small
    group of real users each making a few mistakes does not lock the shared
    address; the cost is that a determined attacker from that address also gets
    30 attempts per lockout window. Operators fronting large shared egresses
    should rate-limit at the proxy instead.

    Entries expire ``lockout_seconds`` after their most recent failure, so
    counters age out with inactivity and a lapsed lockout requires a fresh run
    of failures. Entries are pruned lazily on each call and the dict is capped
    at ``max_entries`` (oldest activity evicted first) to bound memory under
    key churn — both key shapes share the one cap.

    ``now_fn`` defaults to ``time.time`` and is injectable for deterministic
    tests of expiry behavior.
    """

    def __init__(
        self,
        max_failures: int = 5,
        lockout_seconds: float = 15 * 60,
        max_entries: int = 1000,
        max_ip_failures: int = 30,
        now_fn: Callable[[], float] = time.time,
    ):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.max_entries = max_entries
        self.max_ip_failures = max_ip_failures
        self._now = now_fn
        self._lock = threading.Lock()
        # key -> (consecutive_failures, locked_until, entry_expires_at), where
        # key is (username, ip) for the per-account budget and ("", ip) for the
        # per-source aggregate budget.
        self._entries: Dict[Tuple[str, str], Tuple[int, float, float]] = {}

    def check(self, username: str, ip: str) -> bool:
        """Return False while either budget for this (username, ip) is locked."""
        now = self._now()
        with self._lock:
            self._prune(now)
            # Aggregate budget first: the IP is the dimension an attacker
            # cannot rotate to reset the counter, so it gates every request
            # from this source before the per-account key is even consulted.
            if not self._key_open(("", ip), now, self.max_ip_failures):
                return False
            return self._key_open((username, ip), now, self.max_failures)

    def record_failure(self, username: str, ip: str) -> None:
        """Count one failed attempt against both the per-account and the per-source budget."""
        now = self._now()
        with self._lock:
            self._prune(now)
            if username:
                # An empty username would collide with the aggregate key; the
                # single ("", ip) bump below already counts those attempts.
                self._bump((username, ip), self.max_failures, now, scope="account")
            self._bump(("", ip), self.max_ip_failures, now, scope="source")
            self._evict_over_cap()

    def record_success(self, username: str, ip: str) -> None:
        """A successful login clears the username key's history entirely.

        Deliberately leaves the per-source aggregate untouched (see the NAT
        tradeoff in the class docstring).
        """
        with self._lock:
            self._entries.pop((username, ip), None)

    # -- internals -----------------------------------------------------------

    def _key_open(self, key: Tuple[str, str], now: float, limit: int) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return True
        failures, locked_until, _expires_at = entry
        return not (failures >= limit and now < locked_until)

    def _bump(self, key: Tuple[str, str], limit: int, now: float, scope: str) -> None:
        entry = self._entries.get(key)
        failures = (entry[0] if entry else 0) + 1
        locked_until = now + self.lockout_seconds if failures >= limit else 0.0
        self._entries.pop(key, None)  # pop+reinsert moves the key to eviction-order end
        self._entries[key] = (failures, locked_until, now + self.lockout_seconds)
        if failures == limit:
            if scope == "source":
                logger.warning(
                    "Login locked out for source %s after %d aggregate failures",
                    key[1], failures,
                )
            else:
                logger.warning(
                    "Login locked out for %r from %s after %d consecutive failures",
                    key[0], key[1], failures,
                )

    def _prune(self, now: float) -> None:
        """Drop entries whose inactivity window has lapsed."""
        for key in [k for k, v in self._entries.items() if now >= v[2]]:
            del self._entries[key]

    def _evict_over_cap(self) -> None:
        """Evict oldest-activity entries (dict order) until within the cap."""
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))
