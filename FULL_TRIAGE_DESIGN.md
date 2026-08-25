# Issue Intake & Triage - Full Triage Workflow (Design)

A Teams message reporting a problem is automatically triaged into a structured,
prioritized, owned ticket, and the result is posted back into the chat. This
document describes the end-to-end flow, how the workflow executes, how it
decides whether a report is valid, an optional GitHub-grounded phase, and a
worked example.

---

## 1. What it does (in one line)

Raw problem reports posted in a Teams chat go in; clean, classified,
prioritized tickets with an owner and an SLA come out. Casual chatter is
ignored. Vague reports are asked to clarify rather than becoming bad tickets.

---

## 2. High-level architecture

```
Teams chat message
   -> Power Automate (trigger + guard)          # notices the message, skips the bot's own replies
   -> Signer/receiver                            # authenticates + forwards to the platform
   -> MagOneAI use case (the triage workflow)    # classify -> triage -> reply
   -> reply posted back into the Teams chat
```

- The **trigger** fires on every new message.
- A **guard** stops the bot's own replies from re-triggering the flow (no loops).
- The **workflow** does all the intelligence: classify, verify, triage, reply.

---

## 3. The full triage workflow - how it executes

### Phase 1: core triage (no external systems)

```
START(message, sender, channel, conversation_id, screenshots?)
  |
  v
AGENT "triage"  (LLM, vision-ready)
   Reads the message (and screenshots when enabled).
   Outputs a STRUCTURED verdict:
     verdict:       issue | noise | needs_info
     confidence:    0.0 - 1.0
     is_actionable: true only if there is enough detail to act
     title:         short ticket title
     description:   cleaned-up problem statement
     category:      bug | incident | feature_request | question
     severity:      critical | high | medium | low
     affected_area: e.g. "billing/export"
     reason:        one line explaining the verdict (auditable)
  |
  v
CONDITIONAL "route"
   +-- noise                                  -> END (silent)
   +-- needs_info  OR low confidence          -> reply asking for specifics (NO ticket)
   +-- issue + actionable + confident         -> continue
  |
  v
TOOL "reply_thinking"  (posts "Triaging your report...")   # loading indicator, issues only
  |
  v
CODE "assign"  (deterministic business rules, no LLM)
   severity -> priority + SLA hours
   affected_area/category -> owner/team
  |
  v
CODE "build_ticket"  (assemble the formatted ticket text)
  |
  v
TOOL "reply_ticket"  (posts the full ticket back into the chat)
  |
  v
END
```

### Step by step

1. **START** - inputs arrive (text now; screenshots when media is enabled).
2. **triage (AGENT)** - the LLM does the understanding: is it an issue, how
   confident, a clean title, category, severity, and the affected area. This is
   also where screenshots are read (vision) when enabled.
3. **route (CONDITIONAL)** - three outcomes:
   - **noise** -> stay silent.
   - **needs_info / low confidence** -> reply asking for the missing detail
     (which system, what you saw, steps). No ticket is created.
   - **issue** (genuine, specific, confident) -> continue to triage.
4. **reply_thinking (TOOL)** - posts a quick "Triaging your report..." message.
   This only happens for issues, so noise is never acknowledged.
5. **assign (CODE)** - deterministic rules (kept in code so they are
   consistent, not left to the LLM):
   - severity -> priority + SLA (table below)
   - affected_area/category -> owner/team (table below)
6. **build_ticket (CODE)** - formats the final ticket text.
7. **reply_ticket (TOOL)** - posts the ticket back into the chat.
8. **END.**

---

## 4. How it decides whether an issue is "valid"

Important distinction:

- The agent can reliably judge **"is this a genuine, specific, actionable
  problem report?"** (versus chat, opinion, or a vague one-liner).
- The agent **cannot** prove the bug is physically real or reproducible - it is
  reading a message, not running the app. True proof needs the owning team (or
  the GitHub-grounded phase below).

Validity is enforced with four levers, so a ticket is only cut when the report
is genuine, specific, and confident:

1. **Explicit criteria** in the prompt (what is an issue vs noise vs needs_info).
2. **Specificity gate** - a real-sounding but detail-free report ("it is broken
   again") becomes `needs_info` and gets a clarify reply, not a ticket.
3. **Confidence threshold** - low confidence never silently becomes a ticket.
4. **Fail-safe default** - anything ambiguous defaults to `noise`.

Optional extra rigor (add when false positives matter):

- **Second-pass verify (adversarial):** for borderline or high-severity items, a
  second skeptical LLM check; create the ticket only if both agree.
- **Human approval:** for critical/high severity, route to a human approval step
  before the ticket is finalized.

---

## 5. The reply (ticket format)

```
ISSUE logged - INC
Title:    Invoice export returns 500 for enterprise users
Category: bug   |   Severity: high   |   Priority: P2   |   SLA: 4h
Owner:    Export / Billing squad
Summary:  Export button 500s; enterprise-impacting.
```

A vague message instead gets:

```
This looks like it might be an issue, but I need a bit more detail to log it:
which system, what you saw (error text/screenshot), and steps to reproduce.
```

---

## 6. Business rules (defaults - adjust to your teams)

**Severity -> Priority + SLA**

| Severity | Priority | SLA |
|----------|----------|-----|
| critical | P1 | 2h |
| high | P2 | 4h |
| medium | P3 | 24h |
| low | P4 | 72h |

**Affected area / category -> Owner**

| Signal | Owner |
|--------|-------|
| export, billing, payment, invoice | Billing squad |
| auth, login, sso | Platform squad |
| ui, render, frontend | Frontend squad |
| (default) | Triage / Support |

---

## 7. Phase 2 (optional): GitHub-grounded triage

Giving the workflow a **GitHub MCP** with scoped repo access upgrades triage
from "the LLM judges the message" to "the agent investigates the real repo."

```
AGENT triage -> verdict, category, severity, affected_area
   |  (issue)
   v
TOOL github.search_issues   # dedup: is this already reported?
TOOL github.search_code     # validity: does the affected area/error path exist?
TOOL github.list_commits    # already fixed recently?
   v
AGENT investigate  # valid? duplicate? likely owner (CODEOWNERS)?
   +-- duplicate    -> link/comment on the existing issue  -> reply "linked to #123"
   +-- new + valid  -> github.create_issue (labels, assignee) -> reply with the link
   +-- needs_info   -> clarify (no ticket)
```

What it adds:

- **Real dedup** against actual GitHub issues (not guesswork).
- **Code-grounded validity** - confirm the affected area exists / has a plausible
  error path.
- **Already-fixed check** via recent commits and PRs.
- **Real owner** from CODEOWNERS / file history.
- **A tracked ticket** created in GitHub with severity/priority labels.

Honest limits:

- It **investigates, it does not reproduce.** It reads code, issues, and history;
  it cannot run the app to prove the bug.
- Needs a **GitHub MCP registered** and a **fine-grained token scoped to one
  repo** (read code + read/write issues only - least privilege).
- Searches are **capped** (top-N) and only run for confirmed issues, not noise,
  to control API and token cost.

---

## 8. Worked example

**Input (Teams):** "the export button throws a 500 again, third time this week"

**Phase 1:**
- triage -> `verdict=issue, confidence=0.9, severity=high, category=bug,
  affected_area="billing/export", title="Export button returns 500"`.
- route -> issue.
- reply_thinking -> posts "Triaging your report...".
- assign -> high -> P2 / 4h; export -> Billing squad.
- build_ticket + reply_ticket -> posts the ticket above.

**Phase 2 (with GitHub):**
- search_issues finds an open issue #482 for the same 500.
- investigate -> duplicate.
- reply -> "Linked to existing issue #482 (now reported 3x, enterprise-impacting).
  Owner: @export-team. Possibly touched in PR #479."

**Noise example:** "thanks team" -> verdict=noise -> silent, no reply.

**Vague example:** "it is broken again" -> verdict=needs_info -> clarify reply,
no ticket.

---

## 9. Prerequisites / integrations

- **Trigger:** a Teams message source into the workflow (via an automation flow
  or a direct integration).
- **Reply:** the workflow posts back into the chat (via a Teams tool/MCP).
- **LLM:** a vision-capable model (so screenshots can be read).
- **Phase 2:** a GitHub MCP + a repo-scoped token.

---

## 10. Build order

1. **Phase 1 - core triage** (verdict/confidence/severity -> priority/SLA +
   clarify path + posted ticket). No external systems.
2. **Phase 2 - GitHub-grounded** (register the GitHub MCP + scoped token; add
   search_issues dedup, search_code validity, create_issue).
3. **Media** - screenshots read by the vision agent (independent add-on).

Phase 1 gives real triaged tickets first; Phase 2 adds real grounding and dedup.

---

## 11. Example workflow (importable)

A validated Phase 1 workflow is provided alongside this document:
`issue-intake-triage-full.import.json`.

It contains:

- `START` with `message, sender, channel, conversation_id, screenshots`.
- `triage` (agent) producing `verdict, confidence, title, description, category,
  severity, affected_area`.
- `route` (conditional) -> `issue` / `needs_info` / `noise`.
- `issue` path: `reply_thinking` -> `assign` (priority/SLA/owner) ->
  `build_ticket` -> `reply_ticket`.
- `needs_info` path: `clarify` (asks for detail).
- `noise` path: silent.

**Bot-message marker + loop guard.** Every message the workflow posts back
begins with the marker `🤖` (thinking, ticket, and clarify). The upstream
trigger guard must therefore skip any incoming message whose content contains
`🤖`, so the bot's own replies never re-trigger the flow.

Shape:

```
START -> triage(agent) -> route
   issue      -> reply_thinking -> assign -> build_ticket -> reply_ticket -> END
   needs_info -> clarify -> END
   noise      -> END
```

