"""Fetch inline Teams images (hostedContents) via a reused delegated token.

Power Automate cannot reach ``/hostedContents`` (connector whitelist) and the
general Entra connector needs admin consent. So the shim fetches the bytes
itself, minting a Graph token from the SAME refresh token the Teams MCP already
uses (delegated, no extra consent). Degrades to no-images on any failure so the
workflow still runs text-only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_REFRESH_MARGIN_SECONDS = 60
IMAGE_EXT_BY_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/tiff": "tiff",
}

# Matches both /v1.0/ and /beta/ hostedContents $value URLs in the message HTML.
_HOSTED_URL_RE = re.compile(
    r"https://graph\.microsoft\.com/(?:v1\.0|beta)/[^\"'\s]*?"
    r"/hostedContents/[^\"'\s/]+/\$value"
)

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0, "refresh_token": None}


@dataclass
class GraphConfig:
    """Delegated OAuth config, reused from the Teams MCP registration."""

    tenant_id: str
    client_id: str
    client_secret: str
    refresh_token: str
    scope: str = DEFAULT_SCOPE

    @property
    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.refresh_token)


def extract_hosted_content_urls(html: str) -> list[str]:
    """Pull inline-image hostedContents ``$value`` URLs out of message HTML."""
    if not html:
        return []
    seen: dict[str, None] = {}
    for url in _HOSTED_URL_RE.findall(html):
        seen.setdefault(url, None)
    return list(seen)


async def get_graph_token(client: httpx.AsyncClient, cfg: GraphConfig) -> str | None:
    """Mint (and cache) a Graph access token from the refresh token grant."""
    if not cfg.is_configured:
        return None
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    # Azure rotates the refresh token on each redemption; prefer the latest we
    # were handed over the original from .env so we do not redeem a stale one.
    refresh_token = _token_cache["refresh_token"] or cfg.refresh_token
    data = {
        "grant_type": "refresh_token",
        "client_id": cfg.client_id,
        "refresh_token": refresh_token,
        "scope": cfg.scope,
    }
    if cfg.client_secret:  # confidential client; omit for public clients
        data["client_secret"] = cfg.client_secret

    resp = await client.post(
        TOKEN_ENDPOINT.format(tenant=cfg.tenant_id), data=data
    )
    if resp.status_code >= 400:
        return None
    body = resp.json()
    token = body.get("access_token")
    if not token:
        return None
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(body.get("expires_in", 3600)) - TOKEN_REFRESH_MARGIN_SECONDS
    if body.get("refresh_token"):
        _token_cache["refresh_token"] = body["refresh_token"]
    return token


async def fetch_teams_images(
    client: httpx.AsyncClient, urls: list[str], token: str
) -> list[dict[str, Any]]:
    """GET each hostedContents ``$value`` with the token; return image bytes."""
    images: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {token}"}
    for index, url in enumerate(urls):
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 300:
            continue
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = IMAGE_EXT_BY_TYPE.get(content_type, "png")
        images.append(
            {
                "content": resp.content,
                "contentType": content_type or "image/png",
                "filename": f"screenshot_{index + 1}.{ext}",
            }
        )
    return images
