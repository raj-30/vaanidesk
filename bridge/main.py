"""
Chatwoot <-> Sarvam Samvaad bridge. (v13)
Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Flow:
  Chatwoot automation/webhook --(label 'call-me' added)--> POST /chatwoot-webhook
  Bridge --> Sarvam Instant Outbound API --> phone call happens
  Sarvam --> POST /sarvam-webhook (status, transcript, variables)
  Bridge --> Chatwoot API: private note + label on the same conversation
v12 adds:
  - Live call state: triggering a call stores a 'started' state so the
    panel shows the call in progress with an elapsed clock, then flips
    to the real outcome (connected / busy / no_answer / failed) when
    Sarvam's webhook lands.
  - Inbound: POST /inbound-webhook turns a customer call-in into a new
    conversation (find-or-create contact, Phone Calls API inbox).
  - AI summaries: optional LLM layer (Mercury 3 by default, any
    OpenAI-compatible provider via AI_* env vars) that posts a
    structured post-call summary note and feeds the panel.
v13 adds:
  - Inbound fix: Chatwoot v4 wraps a created contact as
    {payload: {contact: {...}}} - the bridge now unwraps it
    (v12 crashed with KeyError: 'id'), and the inbound handler
    can no longer 500 back to Sarvam.
  - Callback scheduler: if the customer asks on a call for a
    callback at a specific time, the AI resolves the moment and
    the bridge calls back automatically (callbacks.json).
  - After an outbound call, the customer gets a confirmation
    message in the chat: what was noted + that their request is
    being processed.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

CALL_RESULTS = {}  # conversation_id -> last Sarvam webhook payload summary

app = FastAPI()

CHATWOOT_URL = os.environ["CHATWOOT_URL"]            # e.g. http://localhost:3000
CHATWOOT_TOKEN = os.environ["CHATWOOT_API_TOKEN"]    # Profile > Access Token in Chatwoot
SARVAM_KEY = os.environ["SARVAM_WORKSPACE_KEY"]      # Samvaad workspace key (X-API-Key), NOT the STT/TTS subscription key
SARVAM_ORG = os.environ["SARVAM_ORG_ID"]
SARVAM_WS = os.environ["SARVAM_WORKSPACE_ID"]
SARVAM_APP_ID = os.environ["SARVAM_APP_ID"]          # the voice agent you built in Samvaad
SARVAM_APP_VERSION = int(os.environ.get("SARVAM_APP_VERSION", "1"))
SARVAM_CONNECTION_ID = os.environ["SARVAM_CONNECTION_ID"]
SARVAM_AGENT_NUMBER = os.environ["SARVAM_AGENT_NUMBER"]  # E.164, e.g. +91XXXXXXXXXX
PUBLIC_BRIDGE_URL = os.environ["PUBLIC_BRIDGE_URL"]  # cloudflared/ngrok https URL, no trailing slash
CHATWOOT_ACCOUNT_ID = int(os.environ.get("CHATWOOT_ACCOUNT_ID", "1"))  # default account (inbound calls land here)
INBOUND_INBOX_ID = os.environ.get("INBOUND_INBOX_ID")  # API-channel inbox "Phone Calls"; inbound feature is off while unset
# AI summary layer. Default provider is Inception Mercury 3, but anything
# OpenAI-compatible works: point AI_BASE_URL / AI_MODEL elsewhere later.
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.inceptionlabs.ai/v1")
AI_API_KEY = os.environ.get("AI_API_KEY")  # no key -> AI summaries silently off
AI_MODEL = os.environ.get("AI_MODEL", "mercury-3")

CW_HEADERS = {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}


def cw_post_note(account_id: int, conversation_id: int, content: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    r = requests.post(url, headers=CW_HEADERS, json={"content": content, "private": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_recording_bytes(interaction_id: str, app_id: str = None,
                          attempts: int = 12, delay: int = 20):
    """Download the call recording (WAV) from Sarvam's Analytics API.
    The stitched recording is produced after hangup and Sarvam answers
    404 until it exists ('No recording was found for this interaction'),
    so retry patiently for several minutes. Uses the app_id Sarvam sent
    in the webhook when available - the recording is scoped to the
    agent that actually handled the call."""
    url = (f"https://apps.sarvam.ai/api/analytics/v1/{SARVAM_ORG}/{SARVAM_WS}/"
           f"{app_id or SARVAM_APP_ID}/recordings/{quote(str(interaction_id), safe='')}")
    for i in range(attempts):
        try:
            r = requests.get(url, headers={"X-API-Key": SARVAM_KEY}, timeout=60)
            if r.status_code == 200 and r.content:
                return r.content
            detail = ""
            try:
                detail = (r.text or "")[:200]
            except Exception:
                pass
            print(f"[recording] attempt {i + 1}: HTTP {r.status_code} {detail}, retrying")
        except requests.RequestException as e:
            print(f"[recording] attempt {i + 1} failed: {e}")
        if i < attempts - 1:
            time.sleep(delay)
    return None


def cw_post_note_with_audio(account_id: int, conversation_id: int, content: str,
                            wav_bytes: bytes, filename: str):
    """Post a private note with an audio file attached, so the recording
    lives inside the Chatwoot conversation itself."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    files = {"attachments[]": (filename, wav_bytes, "audio/wav")}
    form = {"content": content, "private": "true"}
    r = requests.post(url, headers={"api_access_token": CHATWOOT_TOKEN},
                      data=form, files=files, timeout=120)
    r.raise_for_status()
    return r.json()


def attach_recording_later(account_id: int, conversation_id: int,
                           interaction_id: str, app_id: str = None):
    """Background thread: wait for Sarvam to finish stitching the
    recording, download it, and attach it to the conversation."""
    time.sleep(20)
    wav = fetch_recording_bytes(interaction_id, app_id)
    if not wav:
        print(f"[recording] giving up for conversation {conversation_id}: "
              "recording never became available")
        return
    try:
        cw_post_note_with_audio(
            account_id, conversation_id,
            "Call recording attached (customer on the left channel, "
            "agent on the right channel).",
            wav, f"vaanidesk-call-{conversation_id}.wav")
        result = CALL_RESULTS.get(int(conversation_id))
        if result is not None:
            result["summary"] = (result.get("summary") or "") + \
                "\n\nRecording: attached in this conversation."
        print(f"[recording] attached to conversation {conversation_id}")
    except Exception as e:
        print(f"[recording] upload to Chatwoot failed: {e}")


def cw_add_labels(account_id: int, conversation_id: int, labels: list[str]):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
    r = requests.post(url, headers=CW_HEADERS, json={"labels": labels}, timeout=30)
    r.raise_for_status()
    return r.json()


def cw_post_message(account_id: int, conversation_id: int, content: str,
                    private: bool = False, message_type: str = None):
    """Post any message into a conversation (public or private note)."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    body = {"content": content, "private": private}
    if message_type:
        body["message_type"] = message_type
    r = requests.post(url, headers=CW_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _digits(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _same_phone(a: str, b: str) -> bool:
    da, db = _digits(a), _digits(b)
    if not da or not db:
        return False
    return da[-10:] == db[-10:]  # compare national number, ignore country code


def cw_find_contact_by_phone(account_id: int, phone: str):
    """Search Chatwoot contacts by phone; return the matching contact
    dict or None. Used by the inbound flow to reuse existing contacts."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/contacts/search"
    r = requests.get(url, headers=CW_HEADERS, params={"q": phone}, timeout=30)
    r.raise_for_status()
    payload = r.json().get("payload") or []
    for contact in payload:
        if _same_phone(contact.get("phone_number"), phone):
            return contact
    return None


def _unwrap(body, *keys):
    """Dig through Chatwoot's response wrappers and return the
    innermost dict holding any of the wanted keys. Chatwoot v4 wraps
    a created contact as {payload: {contact: {...}, contact_inbox:
    {...}}}, while other endpoints answer flat - both are handled."""
    cur = body if isinstance(body, dict) else {}
    for _ in range(3):
        if any(k in cur for k in keys):
            return cur
        nxt = None
        for wrapper in ("payload", "contact", "contact_inbox", "conversation"):
            if isinstance(cur.get(wrapper), dict):
                nxt = cur[wrapper]
                break
        if nxt is None:
            return cur
        cur = nxt
    return cur


def cw_create_contact(account_id: int, name: str, phone: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/contacts"
    r = requests.post(url, headers=CW_HEADERS,
                      json={"name": name, "phone_number": phone}, timeout=30)
    r.raise_for_status()
    return _unwrap(r.json(), "id", "phone_number")


def cw_create_contact_inbox(account_id: int, contact_id: int, inbox_id: int):
    """Attach a contact to an inbox; returns the record incl. source_id,
    which a conversation is then created against."""
    url = (f"{CHATWOOT_URL}/api/v1/accounts/{account_id}"
           f"/contacts/{contact_id}/contact_inboxes")
    r = requests.post(url, headers=CW_HEADERS, json={"inbox_id": inbox_id}, timeout=30)
    r.raise_for_status()
    return _unwrap(r.json(), "source_id")


def cw_create_conversation(account_id: int, source_id: str, inbox_id: int,
                           additional_attributes: dict = None):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations"
    body = {"source_id": source_id, "inbox_id": inbox_id}
    if additional_attributes:
        body["additional_attributes"] = additional_attributes
    r = requests.post(url, headers=CW_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    return _unwrap(r.json(), "id")


def cw_get_chat_context(account_id: int, conversation_id: int, max_messages: int = 10) -> str:
    """Fetch the recent public messages of a Chatwoot conversation and
    return them as plain text, oldest first, so the voice agent knows
    what the customer already said in chat. Private notes are excluded."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    try:
        r = requests.get(url, headers=CW_HEADERS, timeout=30)
        r.raise_for_status()
        payload = r.json().get("payload") or []
    except Exception:
        return ""
    lines = []
    for m in payload:
        if m.get("private"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        who = "Customer" if m.get("message_type") == 0 else "Support agent"
        lines.append(f"{who}: {content}")
    return "\n".join(lines[-max_messages:])


def start_sarvam_call(phone_e164: str, conversation_id: int, variables: dict, account_id: int = 1):
    url = f"https://apps.sarvam.ai/api/outbounds/v1/orgs/{SARVAM_ORG}/workspaces/{SARVAM_WS}/outbounds"
    payload = {
        "app_config": {
            "app_id": SARVAM_APP_ID,
            "app_version": SARVAM_APP_VERSION,
            "connection_config": {
                "connection_id": SARVAM_CONNECTION_ID,
                "agent_phone_number": SARVAM_AGENT_NUMBER,
            },
            "agent_variables": variables,
        },
        "user_config": {"user_phone_number": phone_e164},
        "webhook_config": {
            "url": f"{PUBLIC_BRIDGE_URL}/sarvam-webhook",
            "metadata": {"conversation_id": conversation_id, "account_id": account_id},
        },
    }
    r = requests.post(url, headers={"X-API-Key": SARVAM_KEY, "Content-Type": "application/json"},
                      json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["attempt_id"]


@app.post("/chatwoot-webhook")
async def chatwoot_webhook(req: Request):
    """Point a Chatwoot automation rule / webhook here.
    Trigger used in this scaffold: a label 'call-me' is added to a conversation."""
    data = await req.json()
    labels = (data.get("labels") or [])
    if "call-me" not in labels:
        return {"skipped": "label call-me not present"}
    conversation_id = data["id"]
    account_id = data["account_id"]
    sender = (data.get("meta") or {}).get("sender") or {}
    phone = sender.get("phone_number")
    if not phone:
        cw_post_note(account_id, conversation_id, "AI call NOT started: contact has no phone number.")
        return {"error": "no phone"}
    chat_context = cw_get_chat_context(account_id, conversation_id)
    variables = {"customer_name": sender.get("name") or "Customer",
                 "chat_context": chat_context or "No previous chat messages were available."}
    attempt_id = start_sarvam_call(phone, conversation_id, variables, account_id)
    cw_post_note(account_id, conversation_id,
                 f"AI call started to {phone}. Sarvam attempt_id: {attempt_id}")
    cw_add_labels(account_id, conversation_id, ["call-started"])
    CALL_RESULTS[int(conversation_id)] = {"status": "started", "live": True,
                                          "started_at": time.time(), "phone": phone,
                                          "name": sender.get("name")}
    return {"attempt_id": attempt_id}


INPUT_VARIABLES = {"customer_name", "chat_context"}
VARIABLE_LABELS = {
    "issue_category": "Issue category",
    "issue_resolved": "Issue resolved",
    "callback_requested": "Callback requested",
    "callback_time": "Callback time",
    "customer_summary": "Summary",
}


def fmt_duration(duration) -> str:
    if duration is None:
        return "-"
    try:
        total = int(round(float(duration)))
    except (TypeError, ValueError):
        return str(duration)
    minutes, seconds = divmod(total, 60)
    return f"{minutes} min {seconds} sec" if minutes else f"{seconds} sec"


def fmt_variables(variables) -> list[str]:
    """Collected/output variables only, one 'Label: value' line each.
    Input variables (customer_name, chat_context) are skipped: Sarvam
    echoes them back and the chat_context blob wrecks the formatting."""
    if not variables:
        return []
    lines = []
    for key, value in variables.items():
        if key in INPUT_VARIABLES or value in (None, ""):
            continue
        label = VARIABLE_LABELS.get(key, key.replace("_", " ").capitalize())
        lines.append(f"- {label}: {value}")
    return lines


def fmt_transcript(transcript) -> list[str]:
    """Sarvam transcript turns carry their text in 'en_text', not
    'content' - reading 'content' silently produced empty turns."""
    if not transcript:
        return []
    lines = []
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        text = turn.get("en_text") or turn.get("content") or turn.get("text") or ""
        if not text:
            continue
        speaker = "Agent" if turn.get("role") == "agent" else "Customer"
        lines.append(f"{speaker}: {text}")
    return lines


def build_call_summary(status, duration, variables, transcript, failure_reason=None) -> str:
    lines = ["AI call completed",
             f"Status: {status}",
             f"Duration: {fmt_duration(duration)}"]
    if failure_reason:
        lines.append(f"Failure reason: {failure_reason}")
    variable_lines = fmt_variables(variables)
    if variable_lines:
        lines.append("")
        lines.append("Collected on call:")
        lines.extend(variable_lines)
    transcript_lines = fmt_transcript(transcript)
    if transcript_lines:
        lines.append("")
        lines.append("Transcript:")
        lines.extend(transcript_lines)
    return "\n".join(lines)


IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_ist(when) -> str:
    """Human time for notes and messages, always IST."""
    if isinstance(when, (int, float)):
        dt = datetime.fromtimestamp(when, IST)
    else:
        dt = when.astimezone(IST)
    return dt.strftime("%d %b %Y, %I:%M %p IST")


def ai_summarize_call(status, duration, variables, transcript):
    """Ask the configured LLM for a structured post-call summary.
    Returns (formatted_text, raw_dict), or (None, None) when the AI
    layer is off (no AI_API_KEY) or anything goes wrong - the call
    result itself must never depend on the AI provider being up.
    v13: the model also resolves a requested callback moment
    ('tomorrow 10 AM', 'in 2 hours') into an ISO timestamp the
    scheduler below can act on."""
    if not AI_API_KEY:
        return None, None
    transcript_lines = fmt_transcript(transcript)
    if not transcript_lines:
        return None, None
    variable_lines = fmt_variables(variables)
    now_ist = datetime.now(IST).strftime("%A, %d %B %Y, %I:%M %p IST")
    user_prompt = (
        f"Current date and time in India: {now_ist}\n"
        f"Call status: {status}\n"
        f"Duration: {fmt_duration(duration)}\n"
        + ("Collected on call:\n" + "\n".join(variable_lines) + "\n" if variable_lines else "")
        + "Transcript:\n" + "\n".join(transcript_lines)
    )
    payload = {
        "model": AI_MODEL,
        "temperature": 0.3,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": (
                "You summarize customer support phone calls for a support team. "
                "Reply with ONLY a JSON object, no markdown, no extra text, with keys: "
                "headline (one line: what the call was and how it ended), "
                "summary (2-3 plain lines), intent (short), "
                "sentiment (positive, neutral or negative), "
                "resolved (true or false), next_action (short, or 'None'), "
                "followup_draft (a short polite follow-up message to the customer, or empty string), "
                "callback_requested (true only if the customer asked to be called back at a specific time), "
                "callback_at (that moment as ISO 8601 with the +05:30 offset, resolved against the "
                "current date and time given in the user message; null when no specific callback time was asked).")},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        r = requests.post(f"{AI_BASE_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {AI_API_KEY}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=30)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:]
            content = content.strip()
        raw = json.loads(content)
    except Exception as e:
        print(f"[ai] summary failed: {e}")
        return None, None
    lines = []
    if raw.get("headline"):
        lines.append(str(raw["headline"]))
    if raw.get("summary"):
        lines.append(str(raw["summary"]))
    meta = []
    if raw.get("intent"):
        meta.append(f"Intent: {raw['intent']}")
    if raw.get("sentiment"):
        meta.append(f"Sentiment: {raw['sentiment']}")
    if raw.get("resolved") is not None:
        meta.append("Resolved: " + ("Yes" if raw["resolved"] else "No"))
    if meta:
        lines.append(" · ".join(meta))
    if raw.get("next_action"):
        lines.append(f"Next action: {raw['next_action']}")
    if raw.get("followup_draft"):
        lines.append(f"Follow-up draft: {raw['followup_draft']}")
    if raw.get("callback_requested") and raw.get("callback_at"):
        try:
            due = datetime.fromisoformat(str(raw["callback_at"]).replace("Z", "+00:00"))
            lines.append(f"Callback requested: {_fmt_ist(due)}")
        except Exception:
            pass
    return ("\n".join(lines) if lines else None), raw


# ---------------------------------------------------------------------------
# Callback scheduler. When a customer asks on a call to be called back at
# a specific time, the AI layer resolves that moment and the bridge stores
# it in callbacks.json (survives restarts). A background thread checks
# every 20 seconds and places due callbacks as normal outbound calls into
# the SAME conversation, so the whole story stays in one thread.
# ---------------------------------------------------------------------------
CALLBACKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callbacks.json")
_PENDING_CALLBACKS = []
_CALLBACKS_LOCK = threading.Lock()


def _save_callbacks():
    try:
        with open(CALLBACKS_FILE, "w") as f:
            json.dump(_PENDING_CALLBACKS, f)
    except Exception as e:
        print(f"[callback] could not save callbacks.json: {e}")


def _load_callbacks():
    global _PENDING_CALLBACKS
    try:
        with open(CALLBACKS_FILE) as f:
            items = json.load(f)
        _PENDING_CALLBACKS = [c for c in items
                              if isinstance(c, dict) and c.get("due_at")]
    except FileNotFoundError:
        _PENDING_CALLBACKS = []
    except Exception as e:
        print(f"[callback] could not load callbacks.json: {e}")
        _PENDING_CALLBACKS = []


def schedule_callback(account_id, conversation_id, phone, name, due_epoch):
    entry = {"account_id": int(account_id), "conversation_id": int(conversation_id),
             "phone": phone, "name": name or "Customer", "due_at": float(due_epoch)}
    with _CALLBACKS_LOCK:
        _PENDING_CALLBACKS.append(entry)
        _save_callbacks()
    when = _fmt_ist(due_epoch)
    result = CALL_RESULTS.get(int(conversation_id))
    if result is not None:
        result["callback_at"] = datetime.fromtimestamp(due_epoch, IST).isoformat()
    try:
        cw_post_note(account_id, int(conversation_id),
                     f"Callback scheduled: the customer asked to be called back on {when}. "
                     "The bridge will place the call automatically.")
    except Exception as e:
        print(f"[callback] scheduling note failed: {e}")
    print(f"[callback] scheduled for conversation {conversation_id} at {when}")
    return when


def _maybe_schedule_callback(account_id, conversation_id, variables, raw):
    """Turn the AI's extracted callback moment into a scheduled call.
    Returns the human-readable moment, or None when there is nothing
    valid to schedule."""
    if not raw or not raw.get("callback_requested") or not raw.get("callback_at"):
        return None
    try:
        due = datetime.fromisoformat(str(raw["callback_at"]).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=IST)
        due_epoch = due.timestamp()
    except Exception as e:
        print(f"[callback] unparseable callback_at {raw.get('callback_at')!r}: {e}")
        return None
    now = time.time()
    if not (now + 30 <= due_epoch <= now + 7 * 24 * 3600):
        print(f"[callback] callback_at outside the allowed window: {raw.get('callback_at')}")
        return None
    with _CALLBACKS_LOCK:
        for existing in _PENDING_CALLBACKS:
            if (existing.get("conversation_id") == int(conversation_id)
                    and abs(existing.get("due_at", 0) - due_epoch) < 600):
                print(f"[callback] already scheduled for conversation {conversation_id}")
                return _fmt_ist(existing["due_at"])
    result = CALL_RESULTS.get(int(conversation_id)) or {}
    phone = result.get("phone")
    name = (variables or {}).get("customer_name") or result.get("name")
    if not phone:
        print(f"[callback] no phone known for conversation {conversation_id}; cannot schedule")
        return None
    return schedule_callback(account_id, conversation_id, phone, name, due_epoch)


def _fire_callback(entry):
    account_id = entry["account_id"]
    conversation_id = entry["conversation_id"]
    phone = entry["phone"]
    name = entry.get("name") or "Customer"
    chat_context = cw_get_chat_context(account_id, conversation_id)
    variables = {"customer_name": name,
                 "chat_context": chat_context or "This is the callback the customer requested earlier."}
    try:
        attempt_id = start_sarvam_call(phone, conversation_id, variables, account_id)
    except Exception as e:
        print(f"[callback] firing failed for conversation {conversation_id}: {e}")
        try:
            cw_post_note(account_id, conversation_id,
                         f"Scheduled callback could NOT be placed ({e}). "
                         "Please call the customer manually.")
        except Exception:
            pass
        return
    CALL_RESULTS[conversation_id] = {"status": "started", "live": True,
                                     "started_at": time.time(),
                                     "phone": phone, "name": name}
    try:
        cw_post_note(account_id, conversation_id,
                     f"Scheduled callback started to {phone}. Attempt: {attempt_id}")
        cw_add_labels(account_id, conversation_id, ["call-started", "call-callback"])
    except Exception as e:
        print(f"[callback] post-fire note failed: {e}")
    print(f"[callback] fired for conversation {conversation_id}, attempt {attempt_id}")


def _callback_scheduler_loop():
    while True:
        try:
            now = time.time()
            due = []
            with _CALLBACKS_LOCK:
                remaining = []
                for entry in _PENDING_CALLBACKS:
                    (due if entry.get("due_at", 0) <= now else remaining).append(entry)
                if due:
                    _PENDING_CALLBACKS[:] = remaining
                    _save_callbacks()
            for entry in due:
                _fire_callback(entry)
        except Exception as e:
            print(f"[callback] scheduler loop error: {e}")
        time.sleep(20)


_load_callbacks()
threading.Thread(target=_callback_scheduler_loop, daemon=True).start()


def build_customer_confirmation(variables, raw, callback_when=None):
    """The kind message the customer sees in the chat after an outbound
    call: what was noted, that their request is being processed, and
    the callback moment if one was scheduled."""
    name = (variables or {}).get("customer_name") or "there"
    lines = [f"Hi {name}, thank you for speaking with VaaniDesk just now.", ""]
    noted = []
    if raw and raw.get("summary"):
        noted.append(str(raw["summary"]))
    else:
        for line in fmt_variables(variables):
            noted.append(line[2:] if line.startswith("- ") else line)
    if noted:
        lines.append("Here's what we noted from your call:")
        lines.extend(noted)
        lines.append("")
    lines.append("We're processing your request and will update you right here. "
                 "If anything changes, just reply to this chat.")
    if callback_when:
        lines.append("")
        lines.append(f"As requested, we'll call you back on {callback_when}.")
    return "\n".join(lines)


def ai_summary_later(account_id: int, conversation_id: int, status,
                     duration, variables, transcript, confirm_customer=False):
    """Background thread: generate the AI summary, store it for the
    panel (call-status) and post it as its own private note; schedule
    a callback if the customer asked for one at a specific time; and
    for outbound calls, send the customer a confirmation message in
    the chat so they know they were heard."""
    text, raw = ai_summarize_call(status, duration, variables, transcript)
    if text:
        result = CALL_RESULTS.get(int(conversation_id))
        if result is not None:
            result["ai_summary"] = text
        try:
            cw_post_note(account_id, int(conversation_id), "AI summary\n" + text)
            print(f"[ai] summary posted for conversation {conversation_id}")
        except Exception as e:
            print(f"[ai] posting summary note failed: {e}")
    callback_when = _maybe_schedule_callback(account_id, conversation_id, variables, raw)
    if confirm_customer and status == "connected":
        try:
            cw_post_message(account_id, int(conversation_id),
                            build_customer_confirmation(variables, raw, callback_when),
                            private=False)
            print(f"[ai] customer confirmation sent for conversation {conversation_id}")
        except Exception as e:
            print(f"[ai] customer confirmation failed: {e}")


@app.post("/sarvam-webhook")
async def sarvam_webhook(req: Request):
    """Sarvam posts here after every call attempt, connected or not.
    Fields per Sarvam docs: status, duration, interaction_id,
    interaction_transcript (turns with role + en_text),
    final_agent_variables, webhook_config.metadata."""
    data = await req.json()
    meta = ((data.get("webhook_config") or {}).get("metadata") or {})
    conversation_id = meta.get("conversation_id")
    if not conversation_id:
        return {"ignored": "no conversation_id in metadata"}
    # account_id is not in Sarvam's payload; store it in metadata too if you run multi-account.
    account_id = int(meta.get("account_id", os.environ.get("CHATWOOT_ACCOUNT_ID", "1")))
    status = data.get("status")
    duration = data.get("duration")
    transcript = data.get("interaction_transcript")
    variables = data.get("final_agent_variables")
    summary = build_call_summary(status, duration, variables, transcript,
                                 data.get("failure_reason"))
    _prev = CALL_RESULTS.get(int(conversation_id)) or {}
    CALL_RESULTS[int(conversation_id)] = {
        "status": status, "duration": duration, "variables": variables,
        "summary": summary, "failure_reason": data.get("failure_reason"),
        "live": False, "ended_at": time.time(),
        "phone": _prev.get("phone"), "name": _prev.get("name"),
        "callback_at": _prev.get("callback_at"),
    }
    cw_post_note(account_id, int(conversation_id), summary)
    cw_add_labels(account_id, int(conversation_id), [f"call-{status}"])
    interaction_id = data.get("interaction_id")
    if interaction_id:
        threading.Thread(target=attach_recording_later,
                         args=(account_id, int(conversation_id), interaction_id,
                               data.get("app_id")),
                         daemon=True).start()
    threading.Thread(target=ai_summary_later,
                     args=(account_id, int(conversation_id), status, duration,
                           variables, transcript),
                     kwargs={"confirm_customer": True},
                     daemon=True).start()
    return {"ok": True}


PROCESSED_INTERACTIONS = set()  # interaction_ids already turned into conversations


def extract_caller_phone(data: dict):
    """Inbound webhook payloads name the caller number differently
    across Sarvam surfaces; accept every plausible field."""
    for key in ("user_phone_number", "caller_number", "caller_phone",
                "from_number", "from", "phone_number"):
        value = data.get(key)
        if value:
            return value
    channel_info = data.get("channel_info") or {}
    for key in ("user_phone_number", "caller_number", "phone_number", "from"):
        value = channel_info.get(key)
        if value:
            return value
    return None


@app.post("/inbound-webhook")
async def inbound_webhook(req: Request):
    """Wrapper around the real handler: an inbound processing error
    must never surface as a 500 to Sarvam (it would retry and
    duplicate work). Log it, unmark the interaction so a genuine
    retry can succeed, and still answer 200."""
    data = await req.json()
    try:
        return await _inbound_webhook_impl(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        interaction_id = data.get("interaction_id")
        if interaction_id:
            PROCESSED_INTERACTIONS.discard(interaction_id)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _inbound_webhook_impl(data: dict):
    """Sarvam's inbound deployment posts here when a customer calls
    the agent's number. An inbound call has no conversation waiting,
    so the bridge creates one: find-or-create the contact by caller
    number, open a conversation in the Phone Calls (API) inbox, post
    the summary + transcript, label it, attach the recording, and run
    the AI summary - the same treatment outbound results get.
    Point the Samvaad inbound Webhook URL at <bridge>/inbound-webhook."""
    interaction_id = data.get("interaction_id")
    if interaction_id and interaction_id in PROCESSED_INTERACTIONS:
        return {"ok": True, "duplicate": True}
    if not INBOUND_INBOX_ID:
        return {"ignored": "INBOUND_INBOX_ID not set in bridge .env"}
    account_id = CHATWOOT_ACCOUNT_ID
    phone = extract_caller_phone(data)
    if not phone:
        return {"ignored": "no caller phone number in payload"}
    if interaction_id:
        PROCESSED_INTERACTIONS.add(interaction_id)
    status = data.get("status") or "connected"
    duration = data.get("duration")
    transcript = data.get("interaction_transcript")
    variables = data.get("final_agent_variables") or {}
    name = (variables.get("caller_name") or variables.get("customer_name")
            or variables.get("name") or f"Caller {phone}")
    contact = cw_find_contact_by_phone(account_id, phone)
    if not contact:
        contact = cw_create_contact(account_id, name, phone)
    contact_id = contact.get("id") if isinstance(contact, dict) else None
    if not contact_id:
        # The create/search shapes surprised us once already (v12
        # KeyError: 'id'); search once more before giving up.
        contact = cw_find_contact_by_phone(account_id, phone)
        contact_id = contact.get("id") if isinstance(contact, dict) else None
    if not contact_id:
        print(f"[inbound] could not resolve a contact id for {phone}: {contact!r}")
        return {"ok": False, "error": "could not resolve contact id"}
    contact_inbox = cw_create_contact_inbox(account_id, int(contact_id),
                                            int(INBOUND_INBOX_ID))
    conversation = cw_create_conversation(account_id, contact_inbox["source_id"],
                                          int(INBOUND_INBOX_ID),
                                          {"inbound_call": True})
    conversation_id = int(conversation["id"])
    missed = status in ("no_answer", "missed", "failed") or (not transcript and not duration)
    if missed:
        first_message = (f"Missed inbound call from {phone} ({name}). "
                         "The AI agent could not speak with the caller. "
                         "Consider calling back.")
    else:
        first_message = (f"Inbound call from {name} ({phone}). "
                         f"Status: {status}. Duration: {fmt_duration(duration)}.")
        variable_lines = fmt_variables(variables)
        if variable_lines:
            first_message += "\n\nCollected on call:\n" + "\n".join(variable_lines)
    cw_post_message(account_id, conversation_id, first_message,
                    private=False, message_type="incoming")
    summary = build_call_summary(status, duration, variables, transcript,
                                 data.get("failure_reason"))
    CALL_RESULTS[conversation_id] = {
        "status": status, "duration": duration, "variables": variables,
        "summary": summary, "failure_reason": data.get("failure_reason"),
        "live": False, "ended_at": time.time(), "inbound": True,
        "phone": phone, "name": name,
    }
    cw_post_note(account_id, conversation_id, summary)
    cw_add_labels(account_id, conversation_id, ["call-inbound", f"call-{status}"])
    if interaction_id and not missed:
        threading.Thread(target=attach_recording_later,
                         args=(account_id, conversation_id, interaction_id,
                               data.get("app_id")),
                         daemon=True).start()
    threading.Thread(target=ai_summary_later,
                     args=(account_id, conversation_id, status, duration,
                           variables, transcript),
                     daemon=True).start()
    return {"ok": True, "conversation_id": conversation_id}


@app.get("/panel", response_class=HTMLResponse)
def panel():
    """Chatwoot Dashboard App (panel v13). Register this URL in
    Chatwoot: Settings > Integrations > Dashboard Apps > Add.
    It appears as a tab inside every conversation.
    Chatwoot sends conversation+contact context via postMessage.
    While a call runs it shows the live in-progress state with an
    elapsed clock; when Sarvam's result lands it flips to the final
    outcome (connected / busy / not reachable / failed) with the
    collected details, the result summary, the AI summary and any
    scheduled callback."""
    return r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Voice Call — VaaniDesk</title>
<style>
  :root{
    --bg:#141517; --card:#1e1f23; --card2:#232429; --line:#2e2f36;
    --text:#ececf1; --muted:#9a9ba3; --faint:#6d6e76;
    --accent:#2563eb; --accent-hover:#1d4ed8;
    --green:#22c55e; --amber:#f59e0b; --orange:#fb923c; --red:#ef4444; --blue:#38bdf8;
  }
  *{box-sizing:border-box}
  body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg); color:var(--text); margin:0; padding:14px; font-size:14px;}
  .head{display:flex; align-items:center; gap:9px; margin-bottom:12px;}
  .mark{width:26px; height:26px; border-radius:8px; background:var(--accent);
        display:flex; align-items:center; justify-content:center; flex:0 0 auto;}
  .mark span{display:block; width:3px; border-radius:2px; background:#fff; margin:0 1px;}
  .head h1{font-size:15px; font-weight:600; margin:0; letter-spacing:.1px;}
  .head .sub{font-size:11px; color:var(--faint); margin-top:1px;}
  .dot{width:8px; height:8px; border-radius:50%; background:var(--faint); margin-left:auto; flex:0 0 auto;}
  .dot.live{background:var(--green); box-shadow:0 0 0 3px rgba(34,197,94,.15);}
  .card{background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; margin-bottom:10px;}
  .cust{display:flex; align-items:center; gap:11px;}
  .avatar{width:40px; height:40px; border-radius:50%; background:var(--card2);
          border:1px solid var(--line); display:flex; align-items:center; justify-content:center;
          font-weight:600; font-size:15px; color:var(--text); flex:0 0 auto;}
  .cust .name{font-size:15px; font-weight:600; line-height:1.25; word-break:break-word;}
  .cust .meta{font-size:12px; color:var(--muted); margin-top:2px;}
  label.flabel{display:block; font-size:11px; font-weight:600; letter-spacing:.4px;
               text-transform:uppercase; color:var(--faint); margin:12px 0 6px;}
  input[type=tel]{width:100%; padding:10px 11px; font-size:15px; background:var(--card2);
    color:var(--text); border:1px solid var(--line); border-radius:9px; outline:none;}
  input[type=tel]:focus{border-color:var(--accent);}
  .hint{font-size:11.5px; color:var(--faint); margin-top:6px; line-height:1.45;}
  button.call{width:100%; margin-top:12px; padding:12px 14px; font-size:15px; font-weight:600;
    background:var(--accent); color:#fff; border:0; border-radius:10px; cursor:pointer;
    display:flex; align-items:center; justify-content:center; gap:9px; transition:background .15s;}
  button.call:hover:not(:disabled){background:var(--accent-hover);}
  button.call:disabled{background:#2c2d33; color:#7c7d85; cursor:not-allowed;}
  .spin{width:15px; height:15px; border-radius:50%; border:2px solid rgba(255,255,255,.35);
        border-top-color:#fff; animation:sp 0.8s linear infinite; display:none;}
  @keyframes sp{to{transform:rotate(360deg)}}
  .pill{display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:600;
        padding:3px 10px; border-radius:999px; border:1px solid var(--line); background:var(--card2);}
  .pill i{width:7px; height:7px; border-radius:50%; background:var(--faint); display:block;}
  .pill.connected{color:var(--green)} .pill.connected i{background:var(--green)}
  .pill.no_answer{color:var(--amber)} .pill.no_answer i{background:var(--amber)}
  .pill.busy{color:var(--orange)} .pill.busy i{background:var(--orange)}
  .pill.failed{color:var(--red)} .pill.failed i{background:var(--red)}
  .pill.started{color:var(--blue)} .pill.started i{background:var(--blue); animation:pl 1.1s ease-in-out infinite}
  @keyframes pl{50%{opacity:.3}}
  .rrow{display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px;}
  .dur{font-size:12.5px; color:var(--muted); font-variant-numeric:tabular-nums;}
  .kv{display:flex; justify-content:space-between; gap:12px; padding:7px 0; border-top:1px solid var(--line);}
  .kv .k{color:var(--muted); font-size:12.5px; flex:0 0 auto;}
  .kv .v{font-size:12.5px; text-align:right; word-break:break-word;}
  .secttl{font-size:11px; font-weight:600; letter-spacing:.4px; text-transform:uppercase; color:var(--faint); margin-bottom:8px;}
  .summary{white-space:pre-wrap; font-size:13px; line-height:1.55; color:var(--text);}
  .ai{border-color:rgba(37,99,235,.45); background:linear-gradient(180deg, rgba(37,99,235,.08), rgba(37,99,235,0) 55%), var(--card);}
  .ai .secttl{color:var(--blue);}
  .empty{color:var(--faint); font-size:13px; text-align:center; padding:16px 8px; line-height:1.5;}
  .err{color:#fca5a5; font-size:12.5px; line-height:1.5; margin-top:10px; white-space:pre-wrap;}
  .failreason{font-size:12px; color:#fca5a5; margin-top:8px;}
  .foot{margin-top:4px; font-size:11px; color:var(--faint); text-align:center;}
  .hidden{display:none}
</style>
</head>
<body>

<div class="head">
  <div class="mark" aria-hidden="true">
    <span style="height:9px"></span><span style="height:15px"></span><span style="height:11px"></span><span style="height:6px"></span>
  </div>
  <div>
    <h1>AI Voice Call</h1>
    <div class="sub">VaaniDesk voice agent</div>
  </div>
  <div class="dot" id="liveDot" title="Bridge status"></div>
</div>

<div class="card">
  <div class="cust">
    <div class="avatar" id="avatar">?</div>
    <div style="min-width:0">
      <div class="name" id="custName">Waiting for conversation…</div>
      <div class="meta" id="custMeta">Open a conversation to load the customer.</div>
    </div>
  </div>
  <label class="flabel" for="phoneIn">Customer phone</label>
  <input id="phoneIn" type="tel" placeholder="+91XXXXXXXXXX" autocomplete="off">
  <div class="hint">Pre-filled from the contact. For chat channels with no number (for example Telegram), type the number in here.</div>
  <button class="call" id="callBtn" disabled>
    <span class="spin" id="spin"></span>
    <span id="callBtnText">Call customer with AI agent</span>
  </button>
  <div class="err hidden" id="callErr"></div>
</div>

<div class="card hidden" id="resultCard">
  <div class="rrow">
    <span class="pill" id="statusPill"><i></i><span id="statusText">—</span></span>
    <span class="dur" id="durText"></span>
  </div>
  <div id="kvBox"></div>
  <div class="failreason hidden" id="failReason"></div>
</div>

<div class="card ai hidden" id="aiCard">
  <div class="secttl">AI summary</div>
  <div class="summary" id="aiSummary"></div>
</div>

<div class="card hidden" id="summaryCard">
  <div class="secttl">Call result</div>
  <div class="summary" id="summaryText"></div>
</div>

<div class="card" id="emptyCard">
  <div class="empty">No calls yet in this conversation.<br>When a call finishes, its outcome, the details the agent collected, and the summary will appear here — the same result is also saved as a private note in the chat.</div>
</div>

<div class="foot">VaaniDesk bridge · panel v13</div>

<script>
let ctx = null;
let pollTimer = null;
let calling = false;
let liveStartedAt = null;   // epoch seconds when the live call started
let tickTimer = null;

const VAR_LABELS = {
  issue_category: "Issue category",
  issue_resolved: "Issue resolved",
  callback_requested: "Callback requested",
  callback_time: "Callback time",
  customer_summary: "Summary",
  customer_name: null, chat_context: null
};
/* Sarvam's final statuses, in the words a support agent actually uses.
   "connected" means the call happened and then ended (hangup). */
const STATUS_TEXT = {
  started: "Call in progress",
  connected: "Connected · call ended",
  no_answer: "Not reachable",
  busy: "Busy",
  failed: "Failed"
};

function $(id){ return document.getElementById(id); }

function initials(name){
  if(!name) return "?";
  const parts = name.trim().split(/\s+/).slice(0,2);
  return parts.map(p => p[0] ? p[0].toUpperCase() : "").join("") || "?";
}
function fmtClock(totalSec){
  const t = Math.max(0, Math.floor(totalSec));
  const m = Math.floor(t/60), s = t%60;
  return m + ":" + String(s).padStart(2,"0");
}
function fmtDur(d){
  if(d === null || d === undefined || d === "") return "";
  const t = Math.round(parseFloat(d));
  if(isNaN(t)) return String(d);
  return "Duration " + fmtClock(t);
}
function prettyVal(v){
  if(v === true) return "Yes";
  if(v === false) return "No";
  if(v === null || v === undefined || v === "") return "—";
  return String(v);
}

window.addEventListener("message", function(e){
  try{
    const d = (typeof e.data === "string") ? JSON.parse(e.data) : e.data;
    if(d && d.event === "appContext"){
      ctx = d.data || {};
      const c = ctx.conversation || {};
      const contact = ctx.contact || (c.meta && c.meta.sender) || {};
      ctx._phone = contact.phone_number || "";
      ctx._name = contact.name || "";
      ctx._accountId = c.account_id || 1;
      $("avatar").textContent = initials(ctx._name);
      $("custName").textContent = ctx._name || "Unknown customer";
      $("custMeta").textContent = "Conversation #" + (c.id || "—") +
        (ctx._phone ? "" : " · no phone on this contact");
      const pin = $("phoneIn");
      if(ctx._phone && !pin.value) pin.value = ctx._phone;
      $("liveDot").classList.add("live");
      updateBtn();
      if(window._fetchTimer){ clearInterval(window._fetchTimer); window._fetchTimer = null; }
      poll();
    }
  }catch(_){}
});

function askContext(){ window.parent.postMessage("chatwoot-dashboard-app:fetch-info", "*"); }
askContext();
window._fetchTimer = setInterval(function(){ if(!ctx) askContext(); }, 2000);

function updateBtn(){
  const v = $("phoneIn").value.trim();
  $("callBtn").disabled = !(ctx && !calling && v.length >= 10);
}
$("phoneIn").addEventListener("input", updateBtn);

function startTicker(){
  stopTicker();
  tickTimer = setInterval(function(){
    if(liveStartedAt){
      $("durText").textContent = "Elapsed " + fmtClock(Date.now()/1000 - liveStartedAt);
    }
  }, 1000);
}
function stopTicker(){ if(tickTimer){ clearInterval(tickTimer); tickTimer = null; } }

function renderResult(j){
  if(!j || !j.found){
    if(!calling){
      $("resultCard").classList.add("hidden");
      $("summaryCard").classList.add("hidden");
      $("aiCard").classList.add("hidden");
      $("emptyCard").classList.remove("hidden");
    }
    return;
  }
  const isLive = !!j.live && j.status === "started";
  $("emptyCard").classList.add("hidden");
  $("resultCard").classList.remove("hidden");
  const st = (j.status || "").toString();
  const pill = $("statusPill");
  pill.className = "pill " + st;
  $("statusText").textContent = STATUS_TEXT[st] || (st ? st : "Done");

  if(isLive){
    /* Real-time state: Sarvam reports the final outcome only when the
       attempt ends, so while the call is running the panel shows the
       in-progress state with a live elapsed clock, then flips to the
       real outcome (connected / busy / not reachable / failed) the
       moment the result lands. */
    liveStartedAt = j.started_at || (Date.now()/1000);
    $("durText").textContent = "Elapsed " + fmtClock(Date.now()/1000 - liveStartedAt);
    startTicker();
    $("kvBox").innerHTML = "";
    $("failReason").classList.add("hidden");
    $("summaryCard").classList.add("hidden");
    $("aiCard").classList.add("hidden");
    return;
  }

  /* Final state. */
  liveStartedAt = null;
  stopTicker();
  if(calling) setCalling(false);
  $("durText").textContent = fmtDur(j.duration);

  const kv = $("kvBox");
  kv.innerHTML = "";
  const vars = j.variables || {};
  Object.keys(vars).forEach(function(k){
    if(!(k in VAR_LABELS) || VAR_LABELS[k] === null) return;
    const row = document.createElement("div"); row.className = "kv";
    const kk = document.createElement("div"); kk.className = "k"; kk.textContent = VAR_LABELS[k];
    const vv = document.createElement("div"); vv.className = "v"; vv.textContent = prettyVal(vars[k]);
    row.appendChild(kk); row.appendChild(vv); kv.appendChild(row);
  });

  if(j.callback_at){
    const row = document.createElement("div"); row.className = "kv";
    const kk = document.createElement("div"); kk.className = "k"; kk.textContent = "Callback scheduled";
    const vv = document.createElement("div"); vv.className = "v";
    const d = new Date(j.callback_at);
    vv.textContent = isNaN(d) ? String(j.callback_at)
      : d.toLocaleString([], {day: "numeric", month: "short", hour: "numeric", minute: "2-digit"});
    row.appendChild(kk); row.appendChild(vv); kv.appendChild(row);
  }

  if(j.failure_reason){
    const fr = $("failReason");
    fr.textContent = "Reason: " + j.failure_reason;
    fr.classList.remove("hidden");
  } else {
    $("failReason").classList.add("hidden");
  }

  if(j.ai_summary){
    $("aiCard").classList.remove("hidden");
    $("aiSummary").textContent = typeof j.ai_summary === "string"
      ? j.ai_summary : JSON.stringify(j.ai_summary, null, 2);
  } else {
    $("aiCard").classList.add("hidden");
  }

  if(j.summary){
    $("summaryCard").classList.remove("hidden");
    $("summaryText").textContent = j.summary;
  } else {
    $("summaryCard").classList.add("hidden");
  }
}

async function poll(){
  if(!ctx || !ctx.conversation) return;
  try{
    const r = await fetch("/call-status?conversation_id=" + ctx.conversation.id);
    const j = await r.json();
    renderResult(j);
    clearTimeout(pollTimer);
    /* Fast polling while a call is live so the outcome flips in
       near-real-time; relaxed polling when idle. */
    pollTimer = setTimeout(poll, (calling || liveStartedAt) ? 2000 : 12000);
  }catch(_){
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, 5000);
  }
}

function setCalling(on){
  calling = on;
  $("spin").style.display = on ? "block" : "none";
  $("callBtnText").textContent = on ? "Calling…" : "Call customer with AI agent";
  updateBtn();
}

$("callBtn").addEventListener("click", async function(){
  if(!ctx || calling) return;
  $("callErr").classList.add("hidden");
  setCalling(true);
  try{
    const r = await fetch("/trigger-call", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        conversation_id: ctx.conversation.id,
        account_id: ctx._accountId || 1,
        phone: $("phoneIn").value.trim(),
        name: ctx._name || "Customer"
      })
    });
    const t = await r.text();
    let j = {};
    try{ j = JSON.parse(t); }
    catch(_){ j = {error: "Bridge returned an unreadable response (HTTP " + r.status + ")."}; }
    if(!j.attempt_id){
      setCalling(false);
      const err = $("callErr");
      err.textContent = "Call not started. " + (j.error || "Unknown error.") +
        (j.sarvam_detail ? "\nSarvam said: " + j.sarvam_detail : "");
      err.classList.remove("hidden");
      return;
    }
    /* Flip to the live view instantly; the bridge has also stored the
       live state, so polling takes over from here. */
    renderResult({found: true, live: true, status: "started", started_at: Date.now()/1000});
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, 1500);
  }catch(e){
    setCalling(false);
    const err = $("callErr");
    err.textContent = "Could not reach the bridge. Is it running?";
    err.classList.remove("hidden");
  }
});
</script>
</body>
</html>
"""


@app.post("/trigger-call")
async def trigger_call(req: Request):
    """Called by the panel button. Starts the Sarvam call directly,
    no label needed."""
    data = await req.json()
    conversation_id = int(data["conversation_id"])
    account_id = int(data.get("account_id", 1))
    phone = data["phone"]
    chat_context = cw_get_chat_context(account_id, conversation_id)
    variables = {"customer_name": data.get("name") or "Customer",
                 "chat_context": chat_context or "No previous chat messages were available."}
    try:
        attempt_id = start_sarvam_call(phone, conversation_id, variables, account_id)
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = (e.response.text or "")[:400]
        except Exception:
            pass
        return {"error": f"Sarvam API rejected the call request (HTTP {e.response.status_code}).",
                "sarvam_detail": detail}
    except requests.RequestException as e:
        return {"error": f"Could not reach the Sarvam API: {e}"}
    cw_post_note(account_id, conversation_id,
                 f"AI call started to {phone} from dashboard button. Attempt: {attempt_id}")
    cw_add_labels(account_id, conversation_id, ["call-started"])
    CALL_RESULTS[conversation_id] = {"status": "started", "live": True,
                                     "started_at": time.time(), "phone": phone,
                                     "name": data.get("name")}
    return {"attempt_id": attempt_id}


@app.get("/call-status")
def call_status(conversation_id: int):
    r = CALL_RESULTS.get(conversation_id)
    return {"found": bool(r), **(r or {})}


@app.get("/health")
def health():
    return {"ok": True}
