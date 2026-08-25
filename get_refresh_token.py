"""Mint a fresh delegated refresh token via device-code flow, store it in .env.

Run this AFTER adding Files.Read.All (delegated) to the app and granting admin
consent, so the new token carries the scope. It updates TEAMS_REFRESH_TOKEN in
.env in place and never prints the token itself. Restart the shim afterwards.

Usage:  python get_refresh_token.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

TENANT = os.environ["TENANT_ID"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
# offline_access -> refresh token; .default -> every scope the app is consented
# for, so the freshly-added Files.Read.All rides along automatically.
SCOPE = "offline_access https://graph.microsoft.com/.default"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"


def _write_env(refresh_token: str) -> None:
    """Replace (or append) TEAMS_REFRESH_TOKEN in .env, leaving other lines intact."""
    lines = ENV_PATH.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("TEAMS_REFRESH_TOKEN="):
            out.append(f"TEAMS_REFRESH_TOKEN={refresh_token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"TEAMS_REFRESH_TOKEN={refresh_token}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def main() -> int:
    with httpx.Client(timeout=30) as client:
        start = client.post(
            f"{BASE}/devicecode", data={"client_id": CLIENT_ID, "scope": SCOPE}
        )
        if start.status_code >= 400:
            body = start.json()
            print("devicecode request failed:", start.status_code, body.get("error"))
            print("If it mentions public client: App -> Authentication -> "
                  "Allow public client flows -> Yes.")
            return 1
        dc = start.json()
        print("\n" + "=" * 64)
        print(dc["message"])  # "To sign in, open ... and enter code XXXX"
        print("=" * 64 + "\n")

        interval = int(dc.get("interval", 5))
        deadline = time.time() + int(dc.get("expires_in", 900))
        poll = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
            "device_code": dc["device_code"],
        }
        if CLIENT_SECRET:
            poll["client_secret"] = CLIENT_SECRET

        while time.time() < deadline:
            time.sleep(interval)
            resp = client.post(f"{BASE}/token", data=poll)
            body = resp.json()
            if resp.status_code < 300:
                refresh_token = body.get("refresh_token")
                if not refresh_token:
                    print("No refresh_token returned - is 'offline_access' consented?")
                    return 1
                _write_env(refresh_token)
                scopes = body.get("scope", "")
                print("SUCCESS: TEAMS_REFRESH_TOKEN updated in .env")
                print("granted scopes:", scopes or "(none returned)")
                if "Files.Read" not in scopes:
                    print("WARNING: no Files.Read* scope - was admin consent granted?")
                print("Next: restart the shim.")
                return 0
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            print("token poll failed:", error, "-",
                  str(body.get("error_description", ""))[:160])
            return 1

        print("Device code expired before you authorized. Re-run the script.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
