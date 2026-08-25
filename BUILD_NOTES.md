# Teams -> Issue Intake & Triage: Build Notes

Status: **working end to end (local)** as of 2026-08-17.

A message posted in a Microsoft Teams chat triggers a MagOneAI use case that
classifies it as an **issue** or **noise** using one cheap-LLM call. Only real
issues proceed; noise is dropped. This is the MVP of the "Issue Intake & Triage"
use case (triage/dedup/ticket come later).

---

## 1. End-to-end flow

```
Teams message
  -> Power Automate flow "Intake & Triage"
       trigger:  When a new message is added to a chat or channel
       action 1: Get message details   (fetches the actual text + sender)
       action 2: HTTP POST  ->  ngrok public URL /forward
  -> Signer shim (local FastAPI, _local/teams-trigger-kit/shim.py)
       verify X-Shared-Secret  ->  strip HTML  ->  HMAC-sign
  -> MagOneAI  POST /api/v1/hooks/{api_key_id}
       verify HMAC signature (X-Signature + X-Timestamp)
  -> Use case executes
       START -> classify (LLM: issue/noise) -> issue_ack / noise_ack -> END
```

Nothing on the MagOneAI backend was changed. The trigger reuses the existing
`/hooks` endpoint and the Credentials page.

---

## 2. Components

### 2.1 The use case (data, imported)
- Files (in the main repo):
  - `docs/examples/workflows/issue-intake-triage-mvp.import.json` (import wrapper)
  - `docs/examples/workflows/issue-intake-triage-mvp.workflow.json` (native raw format)
- Shape: `START(message, sender, channel)` -> `CONDITIONAL(condition_type=llm)` ->
  `issue` branch / `noise` branch -> `END`.
- The classifier is the `CONDITIONAL` in **llm mode**: no agent record needed,
  just an `llm_prompt` + `llm_config_id`. `default_branch: noise` fails safe.
- Must be **published** (Save & Publish) so `/hooks` can trigger it. A draft
  returns 404.

### 2.2 Credentials (data)
- Project -> **Credentials** page -> create an API key, scoped to this use case.
- Gives a **Key ID** (used in the URL) and a **secret** (shown once).
- HMAC scheme: `X-Signature = HMAC_SHA256(secret, "{timestamp}." + body)` hex,
  plus `X-Timestamp`. Freshness window: 300s.

### 2.3 The signer shim (the only custom code)
- Location: `_local/teams-trigger-kit/` (gitignored via `_local/`).
- `shim.py`: FastAPI app.
  - `POST /forward`: verifies `X-Shared-Secret`, strips HTML from the message,
    signs the hook body, forwards to `/api/v1/hooks/{key_id}`.
  - `GET /health`: liveness + resolved target.
- Exists only because Power Automate cannot compute an HMAC signature.
- Config in `.env` (see `.env.example`). Never committed.

### 2.4 Power Automate flow
- Portal: make.powerautomate.com (the Teams-embedded "Workflows" app was
  admin-blocked; the web portal was not).
- Environment: MAGURE SOFTWARES.
- Trigger + 2 actions (see section 4).

---

## 3. Key decisions and why

- **Power Automate, not a Graph subscription.** Reading Teams channel messages
  via Graph needs `ChannelMessage.Read.All` admin consent, which a regular org
  member cannot grant. Power Automate's Teams connector is pre-consented, so it
  needs no admin. The org already uses Power Automate (daily check-in/out flows).
- **A signer shim is required.** Power Automate has no native HMAC function, so
  something must sign the `/hooks` request. The shim does it (few lines of
  Python). Verified: the shim's signature is accepted by the platform's real
  `ApiKeyService.verify_signature`, and a tampered body is rejected.
- **Group chat, not a channel.** A Standard channel in the 40-person team would
  notify everyone. A group chat is private. Tradeoff: the group-chat path needs a
  "Get message details" step (see below); a Standard channel would return content
  directly but is public (or, if Private, unsupported by the trigger).
- **This trigger sends only a notification.** "When a new message is added to a
  chat or channel" delivers a Graph change notification (messageId,
  conversationId) with **no text**. A **"Get message details"** action fetches
  the real content + sender.
- **Teams content is HTML.** Message text arrives wrapped in `<p>...</p>`. The
  shim strips tags (`strip_html`) before forwarding so the workflow gets clean
  text.
- **Classifier lives inside the use case.** Simpler and fully audited. Tradeoff:
  every message creates one execution + one cheap LLM call; noise exits instantly.

---

## 4. Power Automate flow config

**Trigger: When a new message is added to a chat or channel**
- Message type: Group chat
- Conversation: the target group chat

**Action 1: Get message details**
- Message (Message ID): `first(triggerBody()?['value'])?['messageId']`
- Message type: Group chat
- Group chat: the target conversation

**Action 2: HTTP**
- Method: `POST`
- URI: `https://<ngrok-host>/forward`  (changes each ngrok restart)
- Headers:
  - `X-Shared-Secret`: value from the shim `.env`
  - `Content-Type`: `application/json`
  - `ngrok-skip-browser-warning`: `1`
- Body:
  ```json
  {
    "message": "@{outputs('Get_message_details')?['body/body/content']}",
    "sender":  "@{outputs('Get_message_details')?['body/from/user/displayName']}",
    "channel": "Power Automate Testing"
  }
  ```

---

## 5. Run it locally

```bash
# 1. Shim
cd _local/teams-trigger-kit
cp .env.example .env            # fill API_KEY_ID, API_KEY_SECRET, USE_CASE_ID, SHARED_SECRET
../../.venv/bin/python -m uvicorn shim:app --app-dir . --host 127.0.0.1 --port 8790

# 2. Tunnel (Power Automate is cloud, cannot reach localhost)
ngrok http 8790                 # copy the https URL into the HTTP action URI

# 3. Post a message in the group chat, e.g. "the export button throws a 500 again"
```

Debug helpers:
- ngrok request inspector: `http://127.0.0.1:4040/api/requests/http`
- shim `GET /health`

---

## 6. Verification (what was proven)

- Direct signed POST to `/hooks`: issue message -> `issue` branch; noise message
  -> `noise` branch. Both completed.
- Shim: valid call -> 200 forwarded; wrong shared secret -> 404; missing message
  -> 400.
- Full path from a real Teams message: reached the workflow, classified
  correctly, and after the HTML-strip fix the text arrives clean.

---

## 7. Limitations (local-only)

- Runs only while the shim + ngrok are up. On ngrok restart the URL changes and
  the HTTP action URI must be updated.
- Secret sits in a plaintext `.env`.
- `shim.py` still has a debug-capture branch (`full` field -> writes
  `/tmp/shim_capture.json`) that can be removed.

---

## 8. Productionization reference

The platform already has this exact shape: **marketplace inbound webhooks**
(`be/marketplace/`). Mirror that instead of self-hosting the shim.

- Receive + verify: `be/marketplace/routes_events.py`, `event_verify.py`.
  Pattern is: verify a shared secret (constant-time `hmac.compare_digest`, read
  from Vault, fail closed) -> act.
- Secret storage: **HashiCorp Vault**, platform-scoped, JSON blob at a flat path
  (e.g. `marketplace/aws` with key `eventbridge_secret`), read via
  `vault.read_secret_internal(path)`. **Provisioned by ops directly into Vault,
  not through a portal UI** (only enable-flags + plan-maps are UI-managed).
- Auth bypass for machine callers: add the route prefix to `EXEMPT_PREFIXES`
  (`be/magauth/route_actions.py`) and `CSRF_EXEMPT_PREFIXES` (`be/auth/csrf.py`).
- Deployment: runs in-process in the main API container (no separate service).
- Separate-service template (if preferred): `services/pdf_extraction/`.

Two options:
- **A (recommended): native route.** Add `POST /api/v1/teams/events` in `be/`,
  verify the shared secret from Vault, and start the use case **directly**
  (like `be/api_keys/hook_routes.py`). This removes the shim and the HMAC step
  entirely, because the receiver is inside the backend.
- **B: separate service.** Move the shim into `services/teams_signer/`, read the
  shared secret from Vault, deploy as its own container behind the ingress.

Per environment (dev/prod): separate instance + separate credentials. Never
share secrets across environments.

---

## 9. Next steps

- **Phase 6: reply-back into Teams.** Register the `microsoft-teams` MCP (not
  currently registered in `be/mcp`) and add a `send_channel_message` step so the
  workflow posts the triaged result back. Needs admin consent to send.
- **Fuller triage.** Extend the MVP: after `issue`, add an intake/triage AGENT
  (priority, SLA, owner) and dedup, producing a real structured ticket.
- **Productionize** using Option A above.

---

## 10. Security

- Rotate the OpenAI key and the `magk_` API key that were visible in a Teams
  chat screenshot during setup.
- Keep the shared secret and API key out of chat and out of source control.
