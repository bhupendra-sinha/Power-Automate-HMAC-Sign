"""Decode PA-supplied images and upload them to the hook /files endpoint.

Power Automate fetches each inline ``hostedContents`` image from Graph itself
(its own delegated Teams connection) and posts them to the shim as base64. This
module decodes them and uploads them as ``screenshots`` file_ids for the vision
agent. Degrades to text-only ([]) on any failure so the workflow always fires.
"""

from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from typing import Any

import httpx

from signing import sign

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/tiff",
}
# The /files endpoint rejects a filename whose extension mismatches the content
# type (e.g. image/png bytes named .jpg -> 400), so we name each upload by type.
_EXT_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/tiff": "tiff",
}


@dataclass(frozen=True)
class MediaConfig:
    """Hook file endpoint + secret, built once in shim.py."""

    files_url: str
    api_key_secret: str


async def _upload_images(
    client: httpx.AsyncClient, images: list[dict[str, Any]], cfg: MediaConfig
) -> list[str]:
    """Upload image bytes to the hook file endpoint; return file_ids.

    Skips non-image content types and anything over the size cap, and caps the
    count. The vision agent reads the image at runtime, so we upload with
    extract_text=false. File uploads are HMAC signed over an EMPTY body, per the
    /files endpoint contract.
    """
    file_ids: list[str] = []
    for img in images[:MAX_IMAGES]:
        content = img.get("content") or b""
        content_type = (img.get("contentType") or "").lower()
        if content_type not in IMAGE_TYPES or not content or len(content) > MAX_IMAGE_BYTES:
            continue
        timestamp = str(int(time.time()))
        resp = await client.post(
            cfg.files_url,
            params={"extract_text": "false"},
            files={"file": (img.get("filename") or "screenshot.png", content, content_type)},
            headers={
                "X-Timestamp": timestamp,
                "X-Signature": sign(cfg.api_key_secret, timestamp, b""),
            },
        )
        if resp.status_code < 300:
            fid = resp.json().get("file_id")
            if fid:
                file_ids.append(fid)
    return file_ids


async def upload_pa_images(
    client: httpx.AsyncClient, images_b64: Any, cfg: MediaConfig
) -> list[str]:
    """Upload base64 images that Power Automate fetched from Graph itself.

    PA GETs each inline ``hostedContents/$value`` via its own delegated Teams
    connection (no shim token) and posts them as a list of
    ``{filename, contentType, contentBytes}`` where ``contentBytes`` is base64.
    We decode and hand them to the same /files uploader. Returns [] on none or
    on any malformed item, so the workflow still fires text-only.
    """
    if not isinstance(images_b64, list):
        return []
    decoded: list[dict[str, Any]] = []
    for item in images_b64[:MAX_IMAGES]:
        if not isinstance(item, dict):
            continue
        # Strip whitespace PA's JSON formatting can leave around the tokens:
        # a stray space breaks base64 decode and content-type matching.
        raw = "".join((item.get("contentBytes") or item.get("contentBase64") or "").split())
        try:
            content = base64.b64decode(raw, validate=True) if raw else b""
        except (ValueError, binascii.Error):
            continue
        if not content:
            continue
        content_type = (item.get("contentType") or "image/png").strip().lower()
        decoded.append(
            {
                "content": content,
                "contentType": content_type,
                # Name by content type so the /files extension check passes.
                "filename": f"screenshot.{_EXT_BY_CONTENT_TYPE.get(content_type, 'png')}",
            }
        )
    return await _upload_images(client, decoded, cfg)
