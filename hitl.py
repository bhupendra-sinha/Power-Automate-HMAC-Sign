"""Resume a paused approval run by answering its human task via the hook webhook.

Flow: a conversation's poll token (stashed at trigger time) -> poll for the
pending human-task id -> HMAC-respond with the Approve/Reject answer. The
workflow resumes and posts the created/discarded message into Teams itself.
Kept out of shim.py so the route handler stays a thin auth+parse shell.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

import approvals
from signing import sign

# States in which the execution is finished; the use case has already posted any
# needs_info / noise reply itself, so the shim has nothing left to do.
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
_POLL_INTERVAL_SECONDS = 1.5
_MAX_POLL_SECONDS = 45.0


@dataclass(frozen=True)
class HookAuth:
    """Resolved hook endpoints + secret, built once from env in shim.py."""

    hook_url: str
    poll_base: str
    api_key_secret: str


# The individual ticket fields the HUMAN_TASK carries via context_fields, so PA
# can render them as an Adaptive Card FactSet (no multi-line escaping issues).
_TASK_FIELD_KEYS = (
    "draft", "title", "summary", "category", "severity", "priority", "sla", "owner",
)


async def _fetch_task_fields(
    client: httpx.AsyncClient, auth: HookAuth, task_id: str
) -> dict[str, str]:
    """Read the proposed-ticket fields off a parked approval task.

    The HUMAN_TASK node carries them via context_fields; the webhook GET is
    HMAC-signed over an empty body. Falls back to the task description for draft.
    """
    timestamp = str(int(time.time()))
    resp = await client.get(
        f"{auth.hook_url}/human-tasks/{task_id}",
        headers={
            "X-Timestamp": timestamp,
            "X-Signature": sign(auth.api_key_secret, timestamp, b""),
        },
    )
    if resp.status_code >= 300:
        return {}
    task = resp.json()
    resolved = (task.get("context_data") or {}).get("_resolved_fields") or {}
    out = {k: str((resolved.get(k) or {}).get("value") or "") for k in _TASK_FIELD_KEYS}
    if not out.get("draft"):
        out["draft"] = task.get("description") or ""
    return out


async def await_triage_outcome(
    client: httpx.AsyncClient, auth: HookAuth, poll_token: str, conversation_id: str
) -> dict[str, Any]:
    """Poll the fresh triage run until it parks on approval or finishes.

    Returns ``awaiting_approval`` (with the draft) when the issue path parks, a
    terminal status when noise/needs_info completes (the use case already
    replied), or ``running`` if it is still working past the poll budget.
    """
    waited = 0.0
    while waited < _MAX_POLL_SECONDS:
        resp = await client.get(f"{auth.poll_base}/{poll_token}")
        if resp.status_code < 300:
            data = resp.json()
            pending = data.get("pending_human_task_ids") or []
            if pending:
                fields = await _fetch_task_fields(client, auth, pending[0])
                return {
                    "status": "awaiting_approval",
                    "conversation_id": conversation_id,
                    "task_id": pending[0],
                    **fields,
                }
            if data.get("status") in _TERMINAL_STATUSES:
                return {
                    "status": data.get("status"),
                    "execution_id": data.get("execution_id"),
                }
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
    return {"status": "running", "conversation_id": conversation_id}


async def respond_to_pending(
    client: httpx.AsyncClient,
    auth: HookAuth,
    decision_request: dict[str, str],
) -> dict[str, Any]:
    """Answer the pending approval task for a conversation.

    ``decision_request`` carries ``conversation_id`` and a normalized
    ``decision`` ("Approve"/"Reject"). Returns a status dict; never raises for
    the expected no-pending cases so the caller can report them plainly.
    """
    conversation_id = decision_request["conversation_id"]
    decision = decision_request["decision"]

    poll_token = approvals.lookup(conversation_id)
    if not poll_token:
        return {"status": "no_pending_approval"}

    poll = await client.get(f"{auth.poll_base}/{poll_token}")
    pending = (
        poll.json().get("pending_human_task_ids") or [] if poll.status_code < 300 else []
    )
    if not pending:
        approvals.forget(conversation_id)
        return {"status": "no_pending_task"}
    task_id = pending[0]

    body = json.dumps({"answers": {"approval": decision}}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    resp = await client.post(
        f"{auth.hook_url}/human-tasks/{task_id}/respond",
        params={"wait": "true"},
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Signature": sign(auth.api_key_secret, timestamp, body),
        },
    )
    if resp.status_code >= 400:
        return {
            "status": "error",
            "code": resp.status_code,
            "detail": resp.text[:300],
        }

    approvals.forget(conversation_id)
    data = resp.json()
    return {
        "status": "responded",
        "decision": decision,
        "task_id": task_id,
        "execution_status": data.get("execution_status"),
        "execution_id": data.get("execution_id"),
    }
