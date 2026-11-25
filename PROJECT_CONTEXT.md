# VaaniDesk — Project Context (Single Source of Truth)

**Purpose of this document:** This is the one reference for everything about
VaaniDesk — what it is, why we built it, how it works, what is finished, and
what is still open. Use it as the source material for the project report and
the presentation. If anything elsewhere disagrees with this file, this file
wins. Last updated: 25 November 2026 (bridge v13.1).

**Team:** Raj Gajjar, Niyati Thacker, Pradip — B.Tech (CSE), 7th semester,
Indrashil University. VaaniDesk is our college project.

**Repository:** github.com/raj-30/vaanidesk (bridge code under `bridge/`).

---

## 1. What VaaniDesk is

VaaniDesk is a customer-support helpdesk that unifies **chat, email, and AI
voice phone calls** in one screen, built for small Indian businesses.

A support agent works inside one inbox. Customers can reach the business on
Telegram or by email, and — the part that makes VaaniDesk different — the
agent can place an **AI voice call** to the customer straight from the chat,
and customers can also **call the business** and talk to the same AI agent.
Every call, in or out, lands back in the same conversation as a written
summary, a full transcript, and a playable recording, so the human agent
always has the complete picture without listening to anything.

The AI voice agent is called **Vaani**.

### The problem it solves

Small businesses talk to customers across disconnected tools: WhatsApp or
Telegram for chat, a separate mailbox for email, and a personal phone for
calls. Calls leave no written record, follow-ups depend on memory, and a
customer who was promised a callback has to hope someone remembers. VaaniDesk
puts all three channels in one place and makes the phone channel automatic,
recorded, transcribed, summarised, and schedulable.

### What it is built on

VaaniDesk is not written from scratch. It is a working system assembled from
proven parts, with one custom-built component (the bridge) in the middle:

| Layer | What we use | Role |
|---|---|---|
| Helpdesk core | **Chatwoot v4.19.1** (open source), self-hosted in Docker | Inboxes, conversations, contacts, labels, notes, dashboard apps. Rebranded to VaaniDesk (name + custom logos). |
| The bridge | **Custom Python (FastAPI) service we wrote** (~1,270 lines, `bridge/main.py`) | Connects Chatwoot to the telephony and AI providers; holds all the call logic. |
| Voice telephony | **Sarvam Samvaad** (Sarvam AI's voice-agent platform) | Runs the voice agent "Vaani", places and receives real phone calls. Managed number **+91 79658 61269** (carrier underneath: Vobiz). |
| AI summaries | **Mercury 3** (Inception Labs), via an OpenAI-compatible API | Turns call transcripts into structured summaries, extracts action items and callback requests. Provider-swappable through environment variables. |
| Email | **Hostinger** mailbox `hello@rajgajjar.in` | IMAP (receive) + SMTP (send) connected to the Chatwoot email inbox. |
| Chat channel | **Telegram** bot inbox | Two-way customer chat inside VaaniDesk. |
| Public URLs | **ngrok** (Chatwoot) + **Cloudflare Tunnel** (bridge) | The system runs on a laptop, so tunnels expose it to Telegram, Sarvam and Chatwoot webhooks. |

Key design decision: **we never modified Chatwoot's own code.** The bridge
talks to Chatwoot only through its public REST API, so Chatwoot can be
upgraded without breaking our work, and everything custom lives in one
auditable Python file.

---

## 2. Architecture

```
                        ┌────────────────────  Raj's laptop (WSL2 / Docker)  ────────────────────┐
                        │                                                                        │
  Customer              │   ┌──────────────┐        ┌──────────────────┐        ┌──────────────┐   │
  on Telegram ──ngrok──►│   │  Chatwoot    │◄──────►│     BRIDGE       │◄──────►│ Sarvam       │   │──► Customer's phone
  Customer              │   │  (VaaniDesk) │  REST  │  FastAPI :8000   │ HTTPS  │ Samvaad      │   │    (+91 79658 61269)
  by email ──IMAP──────►│   │  Docker :3000│  API   │  bridge/main.py  │        │ voice agent  │   │
                        │   └──────────────┘        └────────┬─────────┘        │ "Vaani"      │   │
                        │                                  │                  └──────────────┘   │
                        │                                  ▼                                     │
                        │                        Mercury 3 (AI summaries,                        │
                        │                        callbacks) via HTTPS API                        │
                        └────────────────────────────────────────────────────────────────────────┘
```

### The bridge's HTTP endpoints

| Endpoint | Called by | What it does |
|---|---|---|
| `GET /panel` | Chatwoot (Dashboard App iframe) | The "AI Voice Call" side panel inside every conversation. |
| `POST /trigger-call` | The panel | Starts an outbound call to a phone number for the open conversation. |
| `GET /call-status` | The panel (polls every 2 s during a call) | Live call state, AI summary, scheduled-callback info. |
| `POST /chatwoot-webhook` | Chatwoot automation | Adding the label `call-me` to a conversation starts a call to the contact. |
| `POST /sarvam-webhook` | Sarvam | Receives the result of an outbound call (status, transcript, variables). |
| `POST /inbound-webhook` | Sarvam | Receives the result of an inbound call; turns it into a conversation. |
| `GET /health` | Us / monitoring | Returns `{"ok": true}`. |

---

## 3. Features — what is built and working

Everything in this section is **implemented and tested live**, not planned.

### 3.1 One helpdesk, three channels

- **Telegram inbox** — customers message the business's Telegram bot; agents
  reply from VaaniDesk; messages flow both ways in real time (webhook via
  ngrok).
- **Email inbox** — `hello@rajgajjar.in` (Hostinger) connected over
  IMAP/SMTP. Incoming mail becomes conversations; agents reply from the same
  screen and replies go out as real email. Chatwoot polls the mailbox every
  minute.
- **Phone Calls inbox** — a dedicated API-channel inbox that holds every
  phone conversation (inbound calls and callbacks), separate from chat.

### 3.2 Outbound AI voice calls from the chat

- Every conversation has an **"AI Voice Call" panel** (a Chatwoot Dashboard
  App served by the bridge, styled to match VaaniDesk's dark UI).
- The agent types or confirms the customer's phone number and presses call.
  The bridge asks Sarvam Samvaad to dial from **+91 79658 61269**.
- **The agent is context-aware:** before dialling, the bridge fetches the
  last 10 public messages of the conversation and passes them to Vaani as
  context, so she opens the call already knowing the customer's issue and
  refers to it ("I am calling about your delivery question…") instead of
  starting blank.
- A call can also be triggered by adding the **`call-me` label** to a
  conversation (Chatwoot automation → bridge webhook).

### 3.3 Live call status in the panel

- While a call runs, the panel shows **"Call in progress"** with a live
  elapsed-time clock, polling the bridge every 2 seconds.
- When Sarvam's end-of-call webhook arrives, the panel flips to the real
  outcome: **Connected · call ended**, **Busy**, **Not reachable**, or
  **Failed** (with the failure reason when Sarvam provides one).
- Honest limitation, by design: Sarvam sends one webhook at the end of a
  call attempt, not a mid-call stream, so "live" status means
  started/in-progress until the final outcome lands.

### 3.4 Everything about a call is written back into the conversation

When a call ends, the bridge posts into the same conversation:

- A **formatted result note**: outcome, duration, the variables the agent
  collected on the call (name, issue, order details — whatever the agent is
  configured to gather).
- The **full transcript** of the conversation with Vaani.
- **Labels** for filtering: `call-started`, `call-connected`,
  `call-busy`, `call-no_answer`, `call-failed`, `call-inbound`,
  `call-callback`.
- The **call recording (WAV)**, downloaded from Sarvam's Analytics API and
  attached to the conversation. Sarvam stitches the recording after hangup
  and returns 404 until it exists, so the bridge retries patiently for
  several minutes. The recording **plays and downloads inside VaaniDesk**
  (verified).

### 3.5 Inbound calls become conversations

- A customer who calls +91 79658 61269 talks to Vaani (the inbound agent app
  in Samvaad). When the call ends, Sarvam posts the result to the bridge's
  `/inbound-webhook`.
- The bridge **finds or creates the contact** by phone number, opens a
  conversation in the Phone Calls inbox, and posts the summary message,
  transcript note, labels (`call-inbound`, `call-<status>`) and the
  recording — same treatment as outbound.
- **Missed calls are not lost:** even a call nobody "handled" (busy,
  no-answer) still creates a conversation, so the business can follow up.
- **Duplicate protection:** each Sarvam interaction is processed once
  (deduplicated on `interaction_id`), so webhook retries never create
  duplicate conversations.
- The handler is defensive: it never returns an error to Sarvam, so a
  Chatwoot-side problem cannot break the telephony webhook chain (this was
  v13's headline fix — see version history).

### 3.6 AI post-call summaries (Mercury 3)

- After every call, the transcript is sent to **Mercury 3**, which returns a
  structured result: a short summary, the important details, and whether the
  customer asked for a callback and when.
- The summary is posted as an **"AI summary" private note** and shown as a
  card in the panel, so an agent understands a 3-minute call in seconds.
- The AI layer is **provider-swappable**: it is configured only through
  `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` environment variables and speaks
  the standard OpenAI chat-completions format. (Mercury's API is being wound
  down by its provider on 28 December 2026; because of the env-var design,
  moving to another model is a configuration change, not a code change.)
- If no AI key is configured, summaries silently switch off and everything
  else keeps working.

### 3.7 Automatic callback scheduling

- If a customer says on a call *"call me tomorrow at 10 AM"* or *"in two
  hours"*, the AI summary step extracts that request and resolves it to an
  exact time (it is given the current IST date/time, so relative phrases
  work).
- The bridge posts a **"Callback scheduled"** note, shows the scheduled time
  in the panel, and **persists the callback to `callbacks.json`** so it
  survives bridge restarts.
- A background thread checks every 20 seconds; when a callback is due, the
  bridge **places the outbound call automatically**, into the same
  conversation, labelled `call-callback`, with the earlier chat context
  attached.
- Safety guards: the resolved time must be between ~1 minute and 7 days in
  the future, and duplicate callbacks for the same conversation within
  10 minutes are ignored.
- **v13.1 reliability fix:** if placing a callback hits a transient network
  error (DNS failure, connection drop, timeout — e.g. the laptop's Wi-Fi
  blinking at that moment), the bridge retries up to 5 times, 45 seconds
  apart, before giving up and posting a "please call the customer manually"
  note. Genuine rejections from Sarvam (e.g. an invalid number) are not
  retried.

### 3.8 Customer confirmation messages

- After a **connected** outbound call, the customer receives a public
  message in their chat: a thank-you, what was noted from the call (the AI
  summary when available, otherwise the collected details), "We're
  processing your request and will update you right here", and the callback
  time if one was scheduled.
- It is deliberately **not** sent for busy/no-answer calls, and not for
  inbound calls (the Phone Calls inbox has no channel back to a phone
  caller).

### 3.9 Branding

- The helpdesk is fully rebranded **VaaniDesk**: product name and custom
  logos (light/dark, thumbnail, favicon) replace Chatwoot's throughout the
  UI. Branding is stored in Chatwoot's installation configuration, applied
  through its own settings mechanism.

---

## 4. End-to-end flows (for the report)

### Outbound call

1. Customer chats on Telegram → conversation in VaaniDesk.
2. Agent opens the AI Voice Call panel, confirms the number, presses call.
3. Bridge fetches the last 10 public messages as context →
   `POST` to Sarvam's Instant Outbound API with the agent app, the managed
   number, the context variables, and a webhook URL carrying the
   conversation id.
4. Panel shows "Call in progress" with a running clock.
5. Vaani calls the customer and discusses their actual issue.
6. Sarvam posts the result (status, duration, transcript, variables) to
   `/sarvam-webhook`.
7. Bridge posts the result note + transcript + labels, starts the recording
   download-and-attach in the background, and starts the AI summary in the
   background.
8. AI summary note + panel card appear; if a callback was requested, it is
   scheduled; the customer receives the confirmation message in the chat.

### Inbound call

1. Customer dials +91 79658 61269 → Vaani (inbound app) answers.
2. Sarvam posts the result to `/inbound-webhook`.
3. Bridge finds or creates the contact, creates the Phone Calls
   conversation, posts summary + transcript + labels, attaches the
   recording, runs the AI summary. A scheduled callback, if requested,
   fires later as an outbound call into the same conversation.

### Email

1. Customer emails hello@rajgajjar.in.
2. Chatwoot's every-minute IMAP fetch imports it as a conversation
   (deduplicated by message id).
3. Agent replies in VaaniDesk → reply leaves via Hostinger SMTP as a normal
   email.

---

## 5. Configuration reference (names only — values live in `.env` files, never in git)

Bridge (`~/bridge/.env` on the laptop):

| Variable | Meaning |
|---|---|
| `CHATWOOT_URL`, `CHATWOOT_API_TOKEN`, `CHATWOOT_ACCOUNT_ID` | How the bridge reaches Chatwoot (account id 2). |
| `SARVAM_WORKSPACE_KEY` | Samvaad workspace API key (`X-API-Key` header). Not the Sarvam STT/TTS subscription key — those are different products. |
| `SARVAM_ORG_ID`, `SARVAM_WORKSPACE_ID` | Samvaad organisation and workspace ids. |
| `SARVAM_APP_ID`, `SARVAM_APP_VERSION` | The outbound voice-agent app the bridge dials with. (Inbound needs no entry here: Samvaad routes inbound calls to the app attached to the number, and its webhook tells the bridge which app ran.) |
| `SARVAM_CONNECTION_ID`, `SARVAM_AGENT_NUMBER` | The telephony connection and the managed number (+91 79658 61269). |
| `PUBLIC_BRIDGE_URL` | The current Cloudflare Tunnel URL of the bridge; used in webhook URLs given to Sarvam. Changes whenever the tunnel restarts. |
| `INBOUND_INBOX_ID` | The Phone Calls API inbox id (5). Inbound processing is off while unset. |
| `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | The AI summary provider (Mercury 3 by default). |

Chatwoot (`~/chatwoot/.env`): `FRONTEND_URL` must equal the current ngrok
URL, or Telegram webhooks and links break.

---

## 6. How to run it (daily startup)

The whole system runs on Raj's laptop (Windows 11 + WSL2 Ubuntu + Docker
Desktop). Startup is a manual four-terminal routine:

0. (When the bridge changed) `cd ~/vaanidesk && git pull`, then
   `cp bridge/main.py ~/bridge/main.py`. Open Docker Desktop.
1. Terminal 1: `ngrok http 3000` — note the https URL.
2. Terminal 2: set `FRONTEND_URL` in `~/chatwoot/.env` to the ngrok URL,
   then `cd ~/chatwoot && docker compose up -d`.
3. Terminal 3: `cloudflared tunnel --url http://localhost:8000` — note the
   trycloudflare URL.
4. Terminal 4: set `PUBLIC_BRIDGE_URL` in `~/bridge/.env` to the
   cloudflared URL, then `cd ~/bridge`, activate the venv, export `.env`,
   `uvicorn main:app --host 0.0.0.0 --port 8000`.
5. Verify: `curl http://localhost:8000/health` → `{"ok": true}`.
6. Because free tunnel URLs change every restart, also update: the
   Dashboard App URL in Chatwoot (= bridge URL + `/panel`), the inbound
   webhook URL in Samvaad (= bridge URL + `/inbound-webhook`), and delete +
   recreate the Telegram inbox so its webhook re-registers on the new ngrok
   URL.

Full install-from-scratch instructions: `SETUP_GUIDE.md` in this repo.

---

## 7. Version history

| Version | Date | What it added |
|---|---|---|
| Scaffold | 24 Nov 2026 | Bridge basics: panel, outbound calls via Sarvam, result note + transcript, recording attach. First live test call succeeded the same day. VaaniDesk branding and Telegram inbox live. |
| v12 | 25 Nov 2026 | Live call status in the panel (elapsed clock, outcome flip); inbound calls → conversations (Phone Calls inbox); Mercury 3 AI summaries; recording stitch-retry. |
| v13 | 25 Nov 2026 | Inbound crash fix (Chatwoot wraps a created contact as `{payload: {contact: …}}`; the handler now unwraps it and can never error back to Sarvam); **callback scheduler**; **customer confirmation messages**. Inbound confirmed working live. |
| v13.1 | 25 Nov 2026 | Callback firing retries transient network errors (5 attempts, 45 s apart) instead of giving up after one DNS hiccup. Email inbox (Hostinger) also fixed and verified the same day — see §8. |

---

## 8. Notable problems we solved (good report material)

- **Corrupted Docker image:** the `chatwoot/chatwoot:latest` image was
  corrupted during install; pinning the exact version **v4.19.1** fixed it.
  The Postgres password also has to be set in two places (`.env` and the
  compose file) or the database refuses to start.
- **Email "Authentication failed" mystery:** Hostinger rejected Chatwoot's
  logins every minute even though the password was provably correct (a
  manual IMAP login with the same password worked). Root cause, in two
  layers: the password stored in Chatwoot was wrong **and** the settings
  form silently failed to save corrections. Two durable lessons: Hostinger
  requires IMAP authentication type **`login`** (Chatwoot's SASL `plain`
  is rejected), and when a form will not save, verify and fix the stored
  values directly through the Rails console instead of trusting the page.
- **Inbound webhook crash (v12 → v13):** creating a contact through
  Chatwoot's API returns the contact wrapped in a `payload` object; v12
  read it flat and crashed with `KeyError: 'id'` on every first-time
  caller. v13 unwraps the response, falls back to re-searching the contact,
  and guarantees the webhook answers Sarvam with success even on internal
  errors.
- **Tunnel fragility:** free ngrok/Cloudflare URLs change on every restart,
  and four different settings (Chatwoot `FRONTEND_URL`, Telegram webhook,
  Dashboard App URL, Samvaad webhook URL) all embed those URLs. Documented
  as a checklist in §6.
- **Recording delay:** Sarvam produces the stitched recording minutes after
  hangup and answers 404 until then; the bridge retries instead of treating
  the first 404 as failure.

---

## 9. Known limitations (state these honestly in the viva)

- **Runs on a laptop.** If the laptop sleeps or the tunnels die, the public
  endpoints die with it. Production hosting is future work.
- **Free tunnel URLs change** on every restart and must be updated in four
  places (§6).
- **No mid-call live feed.** Sarvam reports a call once, at the end; the
  panel's "live" state is started/in-progress plus the final outcome.
- **AI provider deadline.** Mercury 3's API shuts down on 28 December 2026.
  The bridge is insulated (env-var configuration), but a replacement
  provider must be chosen before then.
- **One voice agent.** A single general agent ("Vaani") handles all calls;
  per-business or per-topic agents are future work.
- **Telegram contacts have no phone number**, so panel calls from a Telegram
  conversation need the number typed manually. (Chat contacts who shared
  their number, and all phone-inbox conversations, carry it automatically.)

## 10. Future scope

- AI **suggested replies** in the panel (draft a chat reply from the
  conversation + call history; specced in `MERCURY3_PLAN.md`).
- **Signed recording links** for customers (a Sarvam signed URL valid 3
  days, with the bridge downloading a permanent copy before expiry).
- Permanent hosting (a small VPS) to remove the laptop + tunnel fragility.
- Per-topic voice agents and a proper analytics view of call outcomes.

## 11. Repository map

| Path | What it is |
|---|---|
| `bridge/main.py` | The entire bridge (v13.1). The only file that needs copying to the laptop to update. |
| `bridge/.env.example` | Template listing every environment variable (no real values). |
| `UPDATE_V12.md`, `UPDATE_V13.md`, `UPDATE_V13_1.md` | Per-version changelogs with test notes. |
| `SETUP_GUIDE.md` | Install-from-scratch guide, including every gotcha in §8. |
| `INBOUND_CONVERSATION_FEATURE.md` | Design notes for the inbound-call feature. |
| `MERCURY3_PLAN.md` | The AI-layer plan, including the suggested-replies spec. |
| `PROJECT_CONTEXT.md` | This file. |
