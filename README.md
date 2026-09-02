# Teams trigger kit (local, scratch)

Bridges a Teams channel message to the MagOneAI "Issue Intake & Triage" use case,
with a human-in-the-loop approval step before a ticket is "created".

```
Report:   Teams message -> Power Automate -> shim /forward (HMAC) -> /hooks -> workflow
              triage -> (issue) post DRAFT to Teams -> HUMAN_TASK approval PARKS
                                                          (shim stores conv -> poll_token)
Approve:  "Approve"/"Reject" in Teams -> Power Automate -> shim /approve
              -> poll -> respond webhook -> workflow RESUMES
                 Approve -> create (placeholder) -> post "Created" to Teams
                 Reject  -> post "Discarded" to Teams
```

The shim exists only because Power Automate cannot compute an HMAC signature.
It authenticates Power Automate with a shared secret, then signs the hook body
with the API key secret and forwards it to MagOneAI. Local-test grade only.

The approval step uses the platform's native human-in-the-loop: the `HUMAN_TASK`
approval node pauses the run (execution -> `WAITING_FOR_INPUT`), the trigger
returns a `poll_token`, and the shim later reads `pending_human_task_ids` from
the poll endpoint and answers the task via the HMAC-signed `respond` webhook.
The `create` step is a **placeholder** for now (posts the approved payload); swap
it for a real Jira MCP `create_issue` TOOL once a Jira connection exists.

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

**Loop guard.** Every message the bot posts starts with `🤖`. The trigger must
skip any incoming message whose content contains `🤖`, so the bot's own draft,
"Created", and "Discarded" replies never re-fire the flow. Your plain
"Approve"/"Reject" reply has no `🤖`, so it fires normally.

## Human-in-the-loop approval (Approve/Reject in Teams)

When triage decides `issue`, the workflow posts a **draft ticket** to Teams and
then pauses on a `HUMAN_TASK` approval node. To approve or reject from Teams, add
a **second HTTP action branch** in the same Power Automate flow (or a second
flow) on the same message trigger:

- Condition: the message text is a decision (e.g. contains `Approve` or `Reject`)
  and does **not** contain `🤖`.
- Action: **HTTP** POST to `<ngrok>/approve`
  - Header `X-Shared-Secret`: your `SHARED_SECRET`
  - Header `Content-Type`: `application/json`
  - Body:
    ```json
    {
      "conversation_id": "@{first(triggerBody()?['value'])?['conversationId']}",
      "message_b64": "@{base64(outputs('Get_message_details')?['body/body/content'])}"
    }
    ```

The shim maps the reply to the paused run by `conversation_id` (it stored the
`poll_token` when the run parked), polls for the pending task id, and answers the
`respond` webhook with `{"answers":{"approval":"Approve"}}`. The workflow resumes
and posts the outcome to Teams itself, so this branch needs no reply action.

You can also send an explicit decision instead of the raw message:
`{"conversation_id":"...","decision":"Approve"}`. Test without Teams:

```bash
curl -s -X POST http://localhost:8790/approve \
  -H "Content-Type: application/json" -H "X-Shared-Secret: <SHARED_SECRET>" \
  -d '{"conversation_id":"<same id you triggered with>","decision":"Approve"}'
```

Expect `{"status":"responded","decision":"Approve",...}`. If nothing is pending
you get `{"status":"no_pending_approval"}` (safe no-op).

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
