# Teams trigger kit (local, scratch)

Bridges a Teams channel message to the MagOneAI "Issue Intake & Triage" use case.

```
Teams message -> Power Automate (grab + forward) -> shim (HMAC sign) -> /hooks -> workflow
```

The shim exists only because Power Automate cannot compute an HMAC signature.
It authenticates Power Automate with a shared secret, then signs the hook body
with the API key secret and forwards it to MagOneAI. Local-test grade only.

## Setup

1. Publish the use case in MagOneAI (Save & Publish). Note its slug/id.
2. Credentials page -> create an API key, scope it to that use case. Copy the
   Key ID + secret (shown once).
3. `cp .env.example .env` and fill in every value.
4. Install + run:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn shim:app --host 0.0.0.0 --port 8790
   ```

5. Expose it (Power Automate is cloud, cannot reach localhost):

   ```bash
   ngrok http 8790
   ```

   Use the https ngrok URL as `<ngrok>/forward` in the Power Automate HTTP action.

## Test without Teams first

```bash
curl -s -X POST http://localhost:8790/forward \
  -H "Content-Type: application/json" \
  -H "X-Shared-Secret: <your SHARED_SECRET>" \
  -d '{"message":"invoice export throws 500 for enterprise customers","sender":"priya@acme.com","channel":"report-issue"}'
```

Expect `{"forwarded": true, "execution_id": "...", "status": "..."}`. Then check
the execution in MagOneAI - it should route to the `issue` branch. A noise
message ("thanks team") should route to `noise`.

## Power Automate flow

- Trigger: **When a new message is added to a chat or channel** (dedicated chat/channel).
- Action: **Get message details** (the trigger only sends a change notification, not text).
- Action: **HTTP** POST to `<ngrok>/forward`
  - Header `X-Shared-Secret`: your `SHARED_SECRET`
  - Header `Content-Type`: `application/json`
  - Body:
    ```json
    {
      "message_b64": "@{base64(outputs('Get_message_details')?['body/body/content'])}",
      "sender": "@{outputs('Get_message_details')?['body/from/user/displayName']}",
      "conversation_id": "@{first(triggerBody()?['value'])?['conversationId']}",
      "channel": "report-issue"
    }
    ```

Send the body **base64-encoded** as `message_b64`, not as raw `message`. The
Teams Body Content is HTML with `<img src="...">` double quotes that would break
a raw JSON body; base64 is JSON-safe and the shim decodes it. Inline images ride
along as `hostedContents` URLs inside that HTML and the shim needs them, so do
not pre-strip. (`message` still works for plain-text curl tests.)

There is **no** "Send a Microsoft Graph HTTP request" node. That connector's
whitelist rejects `hostedContents`, so delete it: the shim fetches the image
bytes itself (see below). No condition needed either - the classifier lives in
the use case.

## Inline images (pasted screenshots)

When Graph creds are set in `.env` (`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`,
`TEAMS_REFRESH_TOKEN`), the shim:

1. Finds `hostedContents/.../$value` URLs in the message HTML.
2. Mints a Graph token from the reused delegated refresh token (no admin consent).
3. Downloads each image and uploads it to `/hooks/{key}/files`.
4. Passes the `file_id`s as `screenshots` on the START input, so the vision agent
   reads text + images together.

Leave those blank to run **text-only** - inline images are silently skipped, the
workflow still fires. Check `GET /health` -> `graph_images_enabled` to confirm.

> **Deferred: file attachments.** Images other members ADD via the attach button
> (not inline paste) arrive as OneDrive/SharePoint `attachments`, not
> `hostedContents`, and need a `Files.Read.All` (delegated, admin-consented)
> scope on the app to fetch. Parked until that consent lands; `get_refresh_token.py`
> re-mints the token once the scope is granted. Until then, paste screenshots
> inline (Ctrl+V) so they come through as `hostedContents`.
