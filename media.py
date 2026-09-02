"""Decode PA-supplied images/files and upload them to the hook /files endpoint.

Power Automate fetches each inline ``hostedContents`` image (via its delegated
Teams connection) and each attached document (via the SharePoint/OneDrive
connector) and posts them to the shim as base64. This module decodes them and
uploads them: images become ``screenshots`` file_ids for the vision agent,
documents become ``documents`` file_ids for the START node. Degrades to
text-only ([]) on any failure so the workflow always fires.
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
MAX_FILES = 3
MAX_FILE_BYTES = 50 * 1024 * 1024
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
# Allowed document extensions -> the content type we send. Deriving it from the
# extension keeps the /files check happy and enforces the whitelist (unknown
# extensions are skipped).
_FILE_CONTENT_TYPE_BY_EXT = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
}


@dataclass(frozen=True)
class MediaConfig:
    """Hook file endpoint + secret, built once in shim.py."""

    files_url: str
    api_key_secret: str


def _decode_b64(item: dict[str, Any]) -> bytes | None:
    """Decode an item's base64 content, tolerating PA's stray whitespace.

    Returns the bytes, or None if the base64 is malformed.
    """
    raw = "".join((item.get("contentBytes") or item.get("contentBase64") or "").split())
    try:
        return base64.b64decode(raw, validate=True) if raw else b""
    except (ValueError, binascii.Error):
        return None


async def _post_one(
    client: httpx.AsyncClient,
    cfg: MediaConfig,
    filename: str,
    content: bytes,
    content_type: str,
) -> str | None:
    """HMAC-POST one file to the hook /files endpoint; return its file_id.

    Uploads with extract_text=false (the START node / vision agent handle
    extraction). File uploads are HMAC signed over an EMPTY body, per contract.
    """
    timestamp = str(int(time.time()))
    resp = await client.post(
        cfg.files_url,
        params={"extract_text": "false"},
        files={"file": (filename, content, content_type)},
        headers={
            "X-Timestamp": timestamp,
            "X-Signature": sign(cfg.api_key_secret, timestamp, b""),
        },
    )
    if resp.status_code < 300:
        return resp.json().get("file_id")
    return None


async def upload_pa_images(
    client: httpx.AsyncClient, images_b64: Any, cfg: MediaConfig
) -> list[str]:
    """Upload base64 images PA fetched from Graph (hostedContents).

    Each item is ``{filename, contentType, contentBytes}``. We rename by content
    type so the /files extension check passes, skip non-image or oversized
    items, and cap the count. Returns [] on none, so the workflow still fires.
    """
    if not isinstance(images_b64, list):
        return []
    file_ids: list[str] = []
    for item in images_b64[:MAX_IMAGES]:
        if not isinstance(item, dict):
            continue
        content = _decode_b64(item)
        if not content or len(content) > MAX_IMAGE_BYTES:
            continue
        content_type = (item.get("contentType") or "image/png").strip().lower()
        if content_type not in IMAGE_TYPES:
            continue
        filename = f"screenshot.{_EXT_BY_CONTENT_TYPE[content_type]}"
        fid = await _post_one(client, cfg, filename, content, content_type)
        if fid:
            file_ids.append(fid)
    return file_ids


async def upload_pa_files(
    client: httpx.AsyncClient, files_b64: Any, cfg: MediaConfig
) -> list[str]:
    """Upload base64 documents PA fetched from OneDrive/SharePoint.

    Each item is ``{filename, contentBytes}``. We derive the content type from
    the filename extension (matching the /files check and enforcing the
    allowed-type whitelist), skip unknown types and oversized files, and cap the
    count. Returns [] on none, so the workflow still fires text-only.
    """
    if not isinstance(files_b64, list):
        return []
    file_ids: list[str] = []
    for item in files_b64[:MAX_FILES]:
        if not isinstance(item, dict):
            continue
        content = _decode_b64(item)
        if not content or len(content) > MAX_FILE_BYTES:
            continue
        name = (item.get("filename") or "").strip()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        content_type = _FILE_CONTENT_TYPE_BY_EXT.get(ext)
        if not content_type:
            continue
        fid = await _post_one(client, cfg, name, content, content_type)
        if fid:
            file_ids.append(fid)
    return file_ids
