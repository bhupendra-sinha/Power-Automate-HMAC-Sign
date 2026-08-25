"""Signer shim: Power Automate -> (HMAC sign) -> MagOneAI /hooks.

Power Automate cannot compute HMAC, so it POSTs the raw Teams message here with
a shared-secret header. This shim builds the hook body, signs it exactly as
MagOneAI expects, and forwards it to the /hooks endpoint.

Inline Teams images (pasted screenshots) arrive in the message body as
hostedContents URLs. Power Automate cannot fetch those (connector whitelist), so
the shim fetches the bytes itself with a reused delegated Graph token and
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

from graph import (
    GraphConfig,
    extract_hosted_content_urls,
    fetch_teams_images,
    get_graph_token,
)
from mapping import build_input, interpret_result, message_html
from signing import sign

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif", "image/tiff"}

load_dotenv(Path(__file__).parent / ".env")

MAGONE_BASE_URL = os.environ["MAGONE_BASE_URL"].rstrip("/")
MAGONE_HOOK_PATH = os.getenv("MAGONE_HOOK_PATH", "/api/v1/hooks")
API_KEY_ID = os.environ["API_KEY_ID"]
API_KEY_SECRET = os.environ["API_KEY_SECRET"]
USE_CASE_ID = os.environ["USE_CASE_ID"]
SHARED_SECRET = os.environ["SHARED_SECRET"]

GRAPH_CONFIG = GraphConfig(
    tenant_id=os.getenv("TENANT_ID", ""),
    client_id=os.getenv("CLIENT_ID", ""),
    client_secret=os.getenv("CLIENT_SECRET", ""),
    refresh_token=os.getenv("TEAMS_REFRESH_TOKEN", ""),
    scope=os.getenv("GRAPH_SCOPE", "https://graph.microsoft.com/.default"),
)

HOOK_URL = f"{MAGONE_BASE_URL}{MAGONE_HOOK_PATH}/{API_KEY_ID}"
FILES_URL = f"{HOOK_URL}/files"

app = FastAPI(title="teams-trigger-signer-shim")


async def upload_images(client: httpx.AsyncClient, images: list[dict[str, Any]]) -> list[str]:
    """Upload image bytes to the hook file endpoint; return file_ids.

    Skips non-image content types and anything over the size cap, and caps the
    count. The vision agent reads the image at runtime, so we upload with
    extract_text=false (no upload-time OCR). File uploads are HMAC signed over an
    EMPTY body, per the /files endpoint contract.
    """
    file_ids: list[str] = []
    for img in images[:MAX_IMAGES]:
        content = img.get("content") or b""
        content_type = (img.get("contentType") or "").lower()
        if content_type not in IMAGE_TYPES or not content or len(content) > MAX_IMAGE_BYTES:
            continue
        timestamp = str(int(time.time()))
        resp = await client.post(
            FILES_URL,
            params={"extract_text": "false"},
            files={"file": (img.get("filename") or "screenshot.png", content, content_type)},
            headers={"X-Timestamp": timestamp, "X-Signature": sign(API_KEY_SECRET, timestamp, b"")},
        )
        if resp.status_code < 300:
            fid = resp.json().get("file_id")
            if fid:
                file_ids.append(fid)
    return file_ids


async def resolve_screenshots(client: httpx.AsyncClient, html: str) -> list[str]:
    """Fetch inline Teams images from the message HTML and upload them.

    Returns [] (text-only) when there are no inline images or Graph creds are
    not configured, so the workflow always runs.
    """
    urls = extract_hosted_content_urls(html)
    if not urls:
        return []
    token = await get_graph_token(client, GRAPH_CONFIG)
    if not token:
        return []
    images = await fetch_teams_images(client, urls[:MAX_IMAGES], token)
    return await upload_images(client, images)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the resolved hook target (no secrets)."""
    return {
        "status": "ok",
        "hook_url": HOOK_URL,
        "use_case_id": USE_CASE_ID,
        "graph_images_enabled": GRAPH_CONFIG.is_configured,
    }


@app.post("/forward")
async def forward(
    request: Request,
    x_shared_secret: str = Header(default="", alias="X-Shared-Secret"),
) -> dict[str, Any]:
    """Authenticate the caller, fetch inline images, sign, forward to MagOneAI."""
    if not hmac.compare_digest(x_shared_secret, SHARED_SECRET):
        # Collapse to 404 so the endpoint does not confirm it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON")

    html = message_html(payload) if isinstance(payload, dict) else ""
    if not html:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message or message_b64 is required")

    trigger_input = build_input(payload)

    # wait=true blocks until the workflow finishes and returns its output, so we
    # can tell Power Automate whether to reply. Falls back to 202 on timeout.
    async with httpx.AsyncClient(timeout=90.0) as client:
        file_ids = await resolve_screenshots(client, html)
        if file_ids:
            trigger_input["screenshots"] = file_ids

        # Serialize ONCE and sign the exact bytes we send - re-serializing would
        # change whitespace/key order and break the signature.
        body = json.dumps(
            {"use_case_id": USE_CASE_ID, "input": trigger_input}, separators=(",", ":")
        ).encode()
        timestamp = str(int(time.time()))
        resp = await client.post(
            HOOK_URL,
            params={"wait": "true"},
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

    return interpret_result(resp.json())
