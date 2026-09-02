"""Idempotency store: skip Teams messages we've already processed.

The Teams "new message added" trigger is at-least-once and, for image messages,
commonly fires twice (the message is created, then the hosted image is attached
a beat later). Both deliveries carry the same ``message_id``. We record each
processed id here so the second /forward for it is a no-op instead of a
duplicate execution. Persisted to a JSON file so a shim restart does not forget
in-flight ids. Local-test grade: single process, coarse lock, TTL sweep on
write.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_STORE_PATH = Path(__file__).parent / ".seen_messages.json"
_TTL_SECONDS = 5 * 60  # duplicate trigger fires arrive seconds apart
_lock = threading.Lock()


def _load() -> dict[str, Any]:
    try:
        return json.loads(_STORE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _prune(data: dict[str, Any], now: float) -> dict[str, Any]:
    """Drop ids older than the TTL so the file cannot grow forever."""
    return {
        key: ts
        for key, ts in data.items()
        if isinstance(ts, (int, float)) and now - float(ts) < _TTL_SECONDS
    }


def _save(data: dict[str, Any]) -> None:
    _STORE_PATH.write_text(json.dumps(data, separators=(",", ":")))


def already_seen(message_id: str) -> bool:
    """Return True if this message id was processed within the TTL.

    Atomically records new ids: the first call for an id returns False (and
    remembers it), any repeat within the window returns True. Empty ids are
    never deduped (returns False), so a missing id falls back to normal flow.
    """
    if not message_id:
        return False
    now = time.time()
    with _lock:
        data = _prune(_load(), now)
        if message_id in data:
            return True
        data[message_id] = now
        _save(data)
    return False
