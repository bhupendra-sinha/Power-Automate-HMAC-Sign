"""HMAC signing shared by the shim and the media upload path.

Kept in its own module so ``shim.py`` and ``media.py`` can import it without a
circular dependency. Matches MagOneAI's ApiKeyService.verify_signature:
message = f"{timestamp}." + body, HMAC-SHA256, hex digest.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over ``{timestamp}.`` + body, hex - matches MagOneAI."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
