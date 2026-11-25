# VaaniDesk bridge v12 — what is new and how to switch to it

## What v12 adds
1. **Live call status in the panel.** The moment a call is triggered, the bridge
   stores a live "started" state. The panel shows **Call in progress** with a
   live elapsed clock and polls every 2 seconds. When Sarvam's result lands,
   it flips to the real outcome: **Connected · call ended**, **Busy**,
   **Not reachable**, or **Failed** (with Sarvam's failure reason shown).
   Honest limit: Sarvam sends one webhook when the attempt ends, not a live
   stream, so mid-call the panel cannot tell ringing from talking — it shows
   "in progress + elapsed", and the true outcome appears within ~2 seconds
   of the call ending.
2. **Inbound conversations.** New endpoint `POST /inbound-webhook`. When a
   customer calls the Sarvam number, the bridge creates a Chatwoot
   conversation for it: find-or-create the contact by caller number, open a
   conversation in the **Phone Calls** inbox, post the call summary as the
   first message, the full transcript as a private note, labels
   `call-inbound` + outcome, attach the recording, and run the AI summary.
   A missed call still creates a conversation ("Missed inbound call…") —
   a missed call is a lead. Webhook retries are deduplicated.
3. **Mercury 3 AI summaries.** After every call (outbound and inbound), the
   bridge sends the transcript to the AI provider and posts an **AI summary**
   private note: headline, 2–3 line summary, intent, sentiment, resolved or
   not, next action, and a follow-up message draft. The panel shows it in its
   own AI summary card. The provider is config, not code: `AI_BASE_URL`,
   `AI_API_KEY`, `AI_MODEL` in .env. Mercury's API shuts down **28 Dec 2026**;
   after that, point those three lines at any OpenAI-compatible provider.
   No key in .env = feature silently off, everything else works as before.
4. **Panel v12 UI** is baked into the bridge (customer card, status pill,
   collected-data rows, calling state on the button, no debug line).

Outbound behaviour is unchanged: same endpoints, same notes, same recording
flow. Your current .env keeps working — v12 only ADDS optional lines.

## Switch to v12 (on your laptop, WSL Ubuntu terminal)
1. Back up the old bridge file:
   `cd ~/bridge && cp main.py main.py.v11.bak`
2. Copy the new `main.py` from this pack over `~/bridge/main.py`
   (Explorer trick: open `\\wsl.localhost\Ubuntu\home\raj_gajjar\bridge`
   and drag it in).
3. Add the new lines to `~/bridge/.env` (see .env.example in this pack):
   `INBOUND_INBOX_ID=` and the three `AI_*` lines. Leave values empty for now.
4. Start everything with your normal manual routine and open the panel —
   you should see the new design and "panel v12" in the footer.

## Turn ON inbound (one-time, ~5 minutes)
1. In VaaniDesk: **Settings → Inboxes → Add Inbox → API** channel.
   Name it **Phone Calls**, create it.
2. Open that inbox's settings page and look at the browser address bar —
   the number at the end is the inbox ID.
3. In `~/bridge/.env` set `INBOUND_INBOX_ID=<that number>`. Restart the bridge.
4. In Samvaad, open your inbound deployment and set its **Webhook URL** to
   `<your cloudflared bridge URL>/inbound-webhook`. Deploy.
   (Use the cloudflared URL for port 8000, not the ngrok one. It changes when
   the tunnel restarts — update it then, same as the Dashboard App URL.)
5. Test: call **+917965861269** from your personal phone, talk, hang up.
   Within a minute a new conversation appears in the Phone Calls inbox with
   the summary, transcript note, labels and recording.

## Turn ON Mercury summaries (one-time, ~2 minutes)
1. Go to platform.inceptionlabs.ai, create an API key.
2. Paste it into `~/bridge/.env` as `AI_API_KEY=<key>` — in the file only,
   never in chat, screenshots or the repo.
3. Restart the bridge. Make any test call. A few seconds after the result
   note, an **AI summary** note appears, and the panel shows the AI card.

## How this was verified before it reached you
Built and tested on this side with Chatwoot and Sarvam mocked: all routes
load, trigger stores the live state and the webhook flips it to the final
outcome, inbound creates a conversation for a connected call, ignores a
duplicate webhook, and files a missed call, phone numbers match with or
without the +91, and the AI layer stays off (without breaking anything)
when no key is set. It has NOT yet run against your live Chatwoot + Sarvam —
your first test call after switching is the real end-to-end check. If
anything misbehaves, `main.py.v11.bak` is the one-command rollback.
