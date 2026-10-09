"""One-shot "flash" messages carried in a short-lived signed cookie.

Used by the portal/admin POST handlers to show a notice on the NEXT GET
(post/redirect/get) without leaking state through query parameters. Kept in a
separate module from webauth.py (which is session/CSRF logic) but follows the
same pattern: Starlette-free pure functions, a key derived from the deployment
SESSION_SECRET under a dedicated domain label — ``anki-mcp/flash/v1`` — so the
same secret can never be reused as a signing key for another purpose, and
malformed input verifying to None instead of raising.

Cookie value (four ``|``-separated segments, mirroring the session cookie)::

    base64url(kind) | base64url(message) | expiry_unix | base64url(hmac_sha256(key, payload))

with ``payload = base64url(kind) + "." + base64url(message) + "." + expiry``.
The flash cookie is integrity-protected but not confidential (whoever holds it
can decode the message); it is HttpOnly and expires in FLASH_TTL_SECONDS, and
the consuming page deletes it after a single render, so messages are shown at
most once. ``kind`` is a rendering hint ("info" | "success" | "error" |
"token"); "token" pages render the message as a copyable one-time secret.
"""

import base64
import hashlib
import hmac
import time
from typing import Optional, Tuple

FLASH_COOKIE_NAME = "anki_flash"
FLASH_KEY_DOMAIN = b"anki-mcp/flash/v1"
FLASH_TTL_SECONDS = 60
FLASH_KINDS = ("info", "success", "error", "token")

# Keep the whole cookie comfortably under the ~4KB browsers tolerate. The only
# large-ish legit message is a raw MCP token (~43 chars), so 800 is generous;
# longer messages are a caller bug, not something to silently truncate.
MAX_FLASH_MESSAGE_CHARS = 800

FIELD_SEP = "|"
MAC_INPUT_SEP = "."


def _b64encode(data: bytes) -> str:
    """base64url without padding, so the value is cookie/URL safe."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    """Inverse of _b64encode; re-adds stripped padding. Raises ValueError on bad input."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _flash_key(session_secret: str) -> bytes:
    """Derive the flash-signing key, domain-separated from every other use of the secret."""
    return hmac.new(session_secret.encode("utf-8"), FLASH_KEY_DOMAIN, hashlib.sha256).digest()


def issue_flash(
    session_secret: str, message: str, kind: str = "info", ttl_seconds: int = FLASH_TTL_SECONDS
) -> str:
    """Mint a signed flash-cookie value carrying ``message`` with a render ``kind``.

    Raises ValueError for an unknown kind or an over-long message (caller bug —
    the cookie must stay small and the renderer only knows the fixed kinds).
    """
    if kind not in FLASH_KINDS:
        raise ValueError(f"Invalid flash kind {kind!r}: must be one of {FLASH_KINDS}")
    if not isinstance(message, str):
        raise ValueError("Flash message must be a string")
    if len(message) > MAX_FLASH_MESSAGE_CHARS:
        raise ValueError(
            f"Flash message too long ({len(message)} > {MAX_FLASH_MESSAGE_CHARS} chars); "
            "render long results on a dedicated page instead"
        )
    kind_b64 = _b64encode(kind.encode("ascii"))
    message_b64 = _b64encode(message.encode("utf-8"))
    expiry = str(int(time.time()) + int(ttl_seconds))
    payload = MAC_INPUT_SEP.join((kind_b64, message_b64, expiry))
    mac = hmac.new(_flash_key(session_secret), payload.encode("ascii"), hashlib.sha256).digest()
    return FIELD_SEP.join((kind_b64, message_b64, expiry, _b64encode(mac)))


def verify_flash(session_secret: str, value: str) -> Optional[Tuple[str, str]]:
    """Return ``(kind, message)`` for a valid, unexpired flash cookie, else None.

    The MAC is checked first (constant-time via ``hmac.compare_digest``), then
    the expiry; an unknown kind is rejected so a renderer can switch on it.
    Any malformed input decodes to None rather than raising.
    """
    if not session_secret or not value:
        return None
    parts = value.split(FIELD_SEP)
    if len(parts) != 4:
        return None
    kind_b64, message_b64, expiry_str, mac_b64 = parts
    try:
        expected = hmac.new(
            _flash_key(session_secret),
            MAC_INPUT_SEP.join((kind_b64, message_b64, expiry_str)).encode("ascii"),
            hashlib.sha256,
        ).digest()
        provided_mac = _b64decode(mac_b64)
    except (ValueError, TypeError, UnicodeEncodeError):
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
        kind = _b64decode(kind_b64).decode("ascii")
        message = _b64decode(message_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if kind not in FLASH_KINDS:
        return None
    return kind, message
