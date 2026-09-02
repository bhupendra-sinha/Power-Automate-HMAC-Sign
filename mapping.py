"""Pure translation between the Power Automate payload and the use case I/O.

No network or env dependencies here, so these stay easy to unit test:
``build_input`` maps the inbound Teams fields onto the START input, and
``interpret_result`` turns the /hooks response into a reply decision.
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Teams returns message content as HTML; reduce it to plain text."""
    return html_lib.unescape(_TAG_RE.sub("", text)).strip()


def message_html(payload: dict[str, Any]) -> str:
    """Return the raw Teams message HTML.

    Power Automate sends the body base64-encoded as ``message_b64`` so its inline
    ``<img src="...">`` quotes cannot break the JSON body. Falls back to a plain
    ``message`` field for text-only callers and manual curl tests.
    """
    encoded = payload.get("message_b64")
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            return ""
    return payload.get("message", "")


_APPROVE_WORDS = {"approve", "approved", "yes", "ok", "okay", "confirm", "✅"}
_REJECT_WORDS = {"reject", "rejected", "no", "cancel", "discard", "deny", "❌"}


def parse_decision(text: str) -> str | None:
    """Map a Teams reply to an approval answer.

    Returns "Approve" or "Reject" (the labels the HUMAN_TASK approval branches
    match on), or None when the message is not an approval command. Matches the
    first decision word found, so "approve please" or a leading emoji both work.
    """
    words = strip_html(text or "").lower().split()
    for word in words:
        token = word.strip(".,!:;()")
        if token in _APPROVE_WORDS:
            return "Approve"
        if token in _REJECT_WORDS:
            return "Reject"
    return None


def build_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the Power Automate payload onto the use case's START input."""
    return {
        "message": strip_html(message_html(payload)),
        "sender": payload.get("sender", ""),
        "channel": payload.get("channel", ""),
        "conversation_id": payload.get("conversation_id", ""),
    }
