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


def build_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the Power Automate payload onto the use case's START input."""
    return {
        "message": strip_html(message_html(payload)),
        "sender": payload.get("sender", ""),
        "channel": payload.get("channel", ""),
        "conversation_id": payload.get("conversation_id", ""),
    }


def interpret_result(data: dict[str, Any]) -> dict[str, Any]:
    """Turn the /hooks response into a reply decision for Power Automate.

    ``is_issue`` drives whether Power Automate posts back into Teams; ``reply``
    is the text to post. On a sync-wait timeout the status is not "completed"
    and ``is_issue`` stays False so we do not reply to an unknown result.
    """
    output = data.get("output") or {}
    reply = str(output.get("response", "")) if isinstance(output, dict) else ""
    completed = data.get("status") == "completed"
    is_issue = completed and "Classified as ISSUE" in reply
    return {
        "is_issue": is_issue,
        "classification": "issue" if is_issue else "noise",
        "reply": reply,
        "status": data.get("status"),
        "execution_id": data.get("execution_id"),
    }
