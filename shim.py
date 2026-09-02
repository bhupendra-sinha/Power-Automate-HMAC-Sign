"""Signer shim: Power Automate -> (HMAC sign) -> MagOneAI /hooks.

Power Automate cannot compute HMAC, so it POSTs the raw Teams message here with
a shared-secret header. This shim builds the hook body, signs it exactly as
MagOneAI expects, and forwards it to the /hooks endpoint.

Inline Teams images (pasted screenshots) are fetched by Power Automate itself
(its delegated Teams connection) and posted here as base64; the shim decodes and
uploads them as ``screenshots`` before firing the workflow. Local-test grade:
run behind ngrok, never ship to production as-is.

Run:  uvicorn shim:app --host 0.0.0.0 --port ${PORT:-8790}
"""

from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status

import approvals
import seen_messages
from hitl import HookAuth, await_triage_outcome, respond_to_pending
from mapping import build_input, message_html, parse_decision, strip_html
from media import MediaConfig, upload_pa_files, upload_pa_images
from signing import sign

load_dotenv(Path(__file__).parent / ".env")

MAGONE_BASE_URL = os.environ["MAGONE_BASE_URL"].rstrip("/")
MAGONE_HOOK_PATH = os.getenv("MAGONE_HOOK_PATH", "/api/v1/hooks")
API_KEY_ID = os.environ["API_KEY_ID"]
API_KEY_SECRET = os.environ["API_KEY_SECRET"]
USE_CASE_ID = os.environ["USE_CASE_ID"]
SHARED_SECRET = os.environ["SHARED_SECRET"]

HOOK_URL = f"{MAGONE_BASE_URL}{MAGONE_HOOK_PATH}/{API_KEY_ID}"
HOOK_AUTH = HookAuth(
    hook_url=HOOK_URL,
    poll_base=f"{MAGONE_BASE_URL}/api/v1/poll",
    api_key_secret=API_KEY_SECRET,
)
MEDIA_CONFIG = MediaConfig(
    files_url=f"{HOOK_URL}/files",
    api_key_secret=API_KEY_SECRET,
)

# Every message the use case posts to Teams is prefixed with this marker; both
# endpoints skip inbound messages containing it so the bot's own draft / created
# / discarded posts never re-trigger triage or self-answer the approval task.
BOT_MARKER = "\U0001F916"  # robot emoji

app = FastAPI(title="teams-trigger-signer-shim")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the resolved hook target (no secrets)."""
    return {
        "status": "ok",
        "hook_url": HOOK_URL,
        "use_case_id": USE_CASE_ID,
    }


@app.post("/forward")
async def forward(
    request: Request,
    x_shared_secret: str = Header(default="", alias="X-Shared-Secret"),
) -> dict[str, Any]:
    """Authenticate the caller, fetch inline images, sign, forward to MagOneAI."""
    if not hmac.compare_digest(x_shared_secret, SHARED_SECRET):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")  # 404: hide existence

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON")

    html = message_html(payload) if isinstance(payload, dict) else ""
    if not html:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message or message_b64 is required")

    if BOT_MARKER in html:
        return {"status": "ignored_bot"}

    # The approval card PA re-fires this trigger with no plain text. Skip such
    # empty messages, but NOT media-only ones (a file/image with no caption is
    # still a real message), so check the payload for attached media first.
    has_media = bool(payload.get("images") or payload.get("files"))
    if not strip_html(html) and not has_media:
        return {"status": "ignored_empty"}

    # A decision reply for a conversation with a pending draft is handled by
    # /approve, so skip triage here (avoids a second run on "approve"/"reject").
    conversation_id = payload.get("conversation_id", "")
    if conversation_id and parse_decision(html) and approvals.lookup(conversation_id):
        return {"status": "approval_reply_skipped"}

    # Teams delivers each message 2x (image messages); dedup by message_id.
    if seen_messages.already_seen(payload.get("message_id", "")):
        return {"status": "duplicate_skipped"}

    trigger_input = build_input(payload)

    async with httpx.AsyncClient(timeout=60.0) as client:
        file_ids = await upload_pa_images(client, payload.get("images") or [], MEDIA_CONFIG)
        if file_ids:
            trigger_input["screenshots"] = file_ids
        doc_ids = await upload_pa_files(client, payload.get("files") or [], MEDIA_CONFIG)
        if doc_ids:
            trigger_input["documents"] = doc_ids

        # Serialize ONCE and sign those exact bytes (re-serializing breaks the sig).
        body = json.dumps(
            {"use_case_id": USE_CASE_ID, "input": trigger_input}, separators=(",", ":")
        ).encode()
        timestamp = str(int(time.time()))
        # Trigger WITHOUT wait: we poll ourselves so the card posts soon after the
        # run parks (wait=true would block the full 60s sync timeout).
        resp = await client.post(
            HOOK_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": timestamp,
                "X-Signature": sign(API_KEY_SECRET, timestamp, body),
            },
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"hook returned {resp.status_code}: {resp.text[:300]}",
            )

        poll_token = resp.json().get("poll_token")
        if not poll_token:
            return {"status": "no_poll_token"}

        # awaiting_approval (+ draft) when the issue path parks, else a terminal
        # status. Remember the token so /approve can resolve the parked task.
        outcome = await await_triage_outcome(client, HOOK_AUTH, poll_token, conversation_id)
        if outcome.get("status") == "awaiting_approval" and conversation_id:
            approvals.remember(conversation_id, poll_token)
        return outcome


@app.post("/approve")
async def approve(
    request: Request,
    x_shared_secret: str = Header(default="", alias="X-Shared-Secret"),
) -> dict[str, Any]:
    """Match a Teams Approve/Reject reply to its paused run and answer the task.

    Power Automate fires this on any channel message that reads as a decision.
    We look up the conversation's poll token, poll for the pending task id, then
    HMAC-respond to the human-task webhook. The workflow resumes and posts the
    created/discarded message back into Teams itself, so we post nothing here.
    """
    if not hmac.compare_digest(x_shared_secret, SHARED_SECRET):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be a JSON object")

    conversation_id = payload.get("conversation_id", "")
    if not conversation_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "conversation_id is required")

    message_text = message_html(payload) or payload.get("message", "")
    if BOT_MARKER in message_text:
        # Never answer the approval task from the bot's own draft, which contains
        # the words "Approve"/"Reject" and would otherwise auto-approve itself.
        return {"status": "ignored_bot"}

    decision = payload.get("decision") or parse_decision(message_text)
    if decision not in ("Approve", "Reject"):
        return {"status": "not_a_decision"}

    async with httpx.AsyncClient(timeout=90.0) as client:
        return await respond_to_pending(
            client, HOOK_AUTH, {"conversation_id": conversation_id, "decision": decision}
        )
