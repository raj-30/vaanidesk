# VaaniDesk bridge v13 — inbound fix, callback scheduler, customer confirmation

## The inbound crash, fixed
Your test call reached the bridge fine (your `INBOUND_INBOX_ID=5` is set
correctly — the Phone Calls inbox in your screenshot). It crashed at
`KeyError: 'id'` because of a Chatwoot v4 response shape: creating a contact
returns `{payload: {contact: {...}, contact_inbox: {...}}}`, and v12 used the
payload dict itself, which has no `id`. Verified against Chatwoot v4.19.1's
own source (create.json.jbuilder). v13 unwraps these responses properly,
re-searches once if an id still cannot be resolved, and the inbound handler
can no longer return a 500 to Sarvam — errors are logged, the interaction is
unmarked so a genuine retry can succeed, and the bridge answers 200.

## New: callback scheduler
If a customer on a call (inbound or outbound) asks to be called back at a
specific time, the AI summary step now also extracts that moment — it
understands things like "tomorrow 10 AM" or "in 2 hours" because it is given
the current IST time. The bridge then:
- posts a private note: "Callback scheduled: … <date, time IST>"
- shows "Callback scheduled" in the panel's result card
- stores it in `callbacks.json` next to main.py (survives bridge restarts)
- at the due time, places the call automatically into the SAME conversation,
  with the chat history as context, and labels it `call-callback`
Guards: only times between ~1 minute and 7 days ahead are accepted, and the
same callback is never scheduled twice. This feature needs the AI layer on
(AI_API_KEY set) — the time extraction is the AI's job.

## New: customer confirmation after an outbound call
When an outbound call connects, the customer now gets a message in the chat
itself (Telegram/WhatsApp/website — wherever the conversation lives):

> Hi <name>, thank you for speaking with VaaniDesk just now.
> Here's what we noted from your call: …
> We're processing your request and will update you right here. If anything
> changes, just reply to this chat.
> As requested, we'll call you back on <date, time IST>.   (only if scheduled)

With the AI on, the "what we noted" part is the AI summary; with it off, it
falls back to the details the agent collected. Busy / no-answer calls get no
message — only connected ones. Inbound conversations don't get it either
(the Phone Calls inbox has no channel back to the caller).

## Your update steps (no new env variables this time)
1. `cd ~/vaanidesk && git pull`
2. `cp bridge/main.py ~/bridge/main.py`
3. Restart the bridge from `~/bridge` with .env loaded, as usual.
4. Test inbound again: call +917965861269 as a NEW caller (a number with no
   existing contact is exactly the case that crashed). A conversation should
   appear in Phone Calls with summary, transcript and recording.
5. Test the scheduler: on a call, say "call me back tomorrow at <a time a
   few minutes ahead, adjusted>" — realistically, ask for a callback ~5
   minutes ahead on a test call and watch the note + the automatic call.
   Note: callback times come from the AI, so AI_API_KEY must be set.

Rollback: `cp ~/bridge/main.py.v12.bak ~/bridge/main.py` (if you kept the
backup) or `git log` / previous commit a0cd8b6 in the repo.
