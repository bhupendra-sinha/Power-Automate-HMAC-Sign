"""Tiny durable map of Teams conversation -> paused execution's poll token.

When a triage run parks on the HUMAN_TASK approval node, the trigger webhook
returns 202 with a ``poll_token``. We stash it keyed by the Teams
``conversation_id`` so a later "Approve"/"Reject" message in that same
conversation can be matched back to the paused execution (poll -> task_id ->
respond). Persisted to a JSON file so a shim restart does not lose pending
approvals. Local-test grade: single process, coarse lock, no eviction beyond a
TTL sweep on write.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).parent / ".approvals.json"
_TTL_SECONDS = 24 * 60 * 60  # poll tokens expire server-side after 24h
_lock = threading.Lock()


def _load() -> dict[str, Any]:
    try:
        return json.loads(_STORE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _prune(data: dict[str, Any], now: float) -> dict[str, Any]:
    """Drop entries older than the poll-token TTL so the file cannot grow forever."""
    return {
        key: val
        for key, val in data.items()
        if isinstance(val, dict) and now - float(val.get("ts", 0)) < _TTL_SECONDS
    }


def _save(data: dict[str, Any]) -> None:
    _STORE_PATH.write_text(json.dumps(data, separators=(",", ":")))


def remember(conversation_id: str, poll_token: str) -> None:
    """Record the poll token for a conversation whose run is awaiting approval."""
    if not conversation_id or not poll_token:
        return
    now = time.time()
    with _lock:
        data = _prune(_load(), now)
        data[conversation_id] = {"poll_token": poll_token, "ts": now}
        _save(data)


def lookup(conversation_id: str) -> str | None:
    """Return the poll token for a conversation, or None if nothing is pending."""
    if not conversation_id:
        return None
    with _lock:
        entry = _load().get(conversation_id)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("ts", 0)) >= _TTL_SECONDS:
        return None
    return entry.get("poll_token")


def forget(conversation_id: str) -> None:
    """Drop a conversation's pending approval once it has been resolved."""
    if not conversation_id:
        return
    with _lock:
        data = _load()
        if data.pop(conversation_id, None) is not None:
            _save(data)
