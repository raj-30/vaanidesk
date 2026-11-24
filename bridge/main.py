"""
Chatwoot <-> Sarvam Samvaad bridge.
Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Flow:
  Chatwoot automation/webhook --(label 'call-me' added)--> POST /chatwoot-webhook
  Bridge --> Sarvam Instant Outbound API --> phone call happens
  Sarvam --> POST /sarvam-webhook (status, transcript, variables)
  Bridge --> Chatwoot API: private note + label on the same conversation
"""
import os
import threading
import time
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

CW_HEADERS = {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}


def cw_post_note(account_id: int, conversation_id: int, content: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    r = requests.post(url, headers=CW_HEADERS, json={"content": content, "private": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_recording_bytes(interaction_id: str, attempts: int = 8, delay: int = 15):
    """Download the call recording (WAV) from Sarvam's Analytics API.
    The stitched recording is produced after hangup and may not exist
    the moment the webhook fires, so retry for a few minutes."""
    url = (f"https://apps.sarvam.ai/api/analytics/v1/{SARVAM_ORG}/{SARVAM_WS}/"
           f"{SARVAM_APP_ID}/recordings/{quote(str(interaction_id), safe='')}")
    for i in range(attempts):
        try:
            r = requests.get(url, headers={"X-API-Key": SARVAM_KEY}, timeout=60)
            if r.status_code == 200 and r.content:
                return r.content
            print(f"[recording] attempt {i + 1}: HTTP {r.status_code}, retrying")
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


def attach_recording_later(account_id: int, conversation_id: int, interaction_id: str):
    """Background thread: wait for Sarvam to finish stitching the
    recording, download it, and attach it to the conversation."""
    time.sleep(20)
    wav = fetch_recording_bytes(interaction_id)
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
    CALL_RESULTS[int(conversation_id)] = {
        "status": status, "duration": duration, "variables": variables,
        "summary": summary,
    }
    cw_post_note(account_id, int(conversation_id), summary)
    cw_add_labels(account_id, int(conversation_id), [f"call-{status}"])
    interaction_id = data.get("interaction_id")
    if interaction_id:
        threading.Thread(target=attach_recording_later,
                         args=(account_id, int(conversation_id), interaction_id),
                         daemon=True).start()
    return {"ok": True}


@app.get("/panel", response_class=HTMLResponse)
def panel():
    """Chatwoot Dashboard App. Register this URL in
    Chatwoot: Settings > Integrations > Dashboard Apps > Add.
    It appears as a tab inside every conversation.
    Chatwoot sends conversation+contact context via postMessage."""
    return r"""
<!doctype html><html><body style="font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:14px;background:#141517;color:#ececf1;margin:0">
<style>
button { background:#1f93ff; color:#fff; border:0; border-radius:6px; cursor:pointer; }
button:disabled { background:#33343a; color:#8e8f96; cursor:not-allowed; }
input { background:#1e1f23; color:#ececf1; border:1px solid #34363c; border-radius:6px; outline:none; }
input:focus { border-color:#1f93ff; }
</style>
<h3 style="margin:0 0 8px;font-size:16px">AI Voice Call</h3>
<div id="who">Waiting for conversation context...</div>
<label style="display:block;margin-top:10px;font-size:13px;color:#c9cacf">Customer phone (type it in if the chat channel has no number, e.g. Telegram):
<input id="phoneIn" type="tel" placeholder="+91XXXXXXXXXX" style="display:block;margin-top:4px;padding:8px;width:92%;font-size:15px;box-sizing:border-box"></label>
<button id="callBtn" disabled style="margin-top:10px;padding:10px 14px;font-size:15px">
  Call customer with AI agent</button>
<div id="result" style="margin-top:10px;white-space:pre-wrap"></div>
<div id="debug" style="margin-top:12px;font-size:11px;color:#7d7e85;white-space:pre-wrap">debug: panel v10 loaded, no messages yet</div>
<script>
let ctx = null;
let msgCount = 0, fetchCount = 0;
function dbg(extra) {
  document.getElementById("debug").textContent =
    "debug: panel v10, messages heard: " + msgCount + ", context requests sent: " + fetchCount + (extra ? "\n" + extra : "");
}
window.addEventListener("message", function (e) {
  msgCount++;
  try {
    const d = (typeof e.data === "string") ? JSON.parse(e.data) : e.data;
    dbg("last message: " + String(typeof e.data === "string" ? e.data : JSON.stringify(e.data)).slice(0, 160));
    if (d && d.event === "appContext") {
      ctx = d.data;
      const c = ctx.conversation || {};
      const contact = ctx.contact || (c.meta && c.meta.sender) || {};
      ctx._phone = contact.phone_number || "";
      ctx._name = contact.name || "";
      ctx._accountId = c.account_id || 1;
      document.getElementById("who").textContent =
        "Customer: " + (ctx._name || "-") + "  Phone: " + (ctx._phone || "-");
      const pin = document.getElementById("phoneIn");
      if (ctx._phone && !pin.value) pin.value = ctx._phone;
      updateBtn();
      if (window._fetchTimer) { clearInterval(window._fetchTimer); window._fetchTimer = null; }
      poll();
    }
  } catch (_) {}
});
function updateBtn() {
  const v = document.getElementById("phoneIn").value.trim();
  document.getElementById("callBtn").disabled = !(ctx && v.length >= 10);
}
document.getElementById("phoneIn").addEventListener("input", updateBtn);
function askContext() { fetchCount++; dbg(); window.parent.postMessage("chatwoot-dashboard-app:fetch-info", "*"); }
askContext();
window._fetchTimer = setInterval(function () { if (!ctx) askContext(); }, 2000);
async function poll() {
  if (!ctx) return;
  const r = await fetch("/call-status?conversation_id=" + ctx.conversation.id);
  const j = await r.json();
  if (j.found) document.getElementById("result").textContent =
    j.summary || ("Last call: " + j.status);
  else setTimeout(poll, 4000);
}
document.getElementById("callBtn").onclick = async () => {
  const c = ctx.conversation || {};
  document.getElementById("result").textContent = "Starting call...";
  const r = await fetch("/trigger-call", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({conversation_id: c.id, account_id: ctx._accountId || 1,
      phone: document.getElementById("phoneIn").value.trim(), name: ctx._name || "Customer"})});
  const t = await r.text();
  let j = {};
  try { j = JSON.parse(t); } catch (_) { j = {error: "Bridge returned an unreadable response (HTTP " + r.status + "): " + t.slice(0, 200)}; }
  document.getElementById("result").textContent =
    j.attempt_id ? ("Call started. Attempt: " + j.attempt_id + "\nResult will appear here and as a private note in the chat.")
                 : ("Call NOT started. " + (j.error || JSON.stringify(j)) + (j.sarvam_detail ? "\nSarvam said: " + j.sarvam_detail : ""));
  setTimeout(poll, 5000);
};
</script></body></html>
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
    return {"attempt_id": attempt_id}


@app.get("/call-status")
def call_status(conversation_id: int):
    r = CALL_RESULTS.get(conversation_id)
    return {"found": bool(r), **(r or {})}


@app.get("/health")
def health():
    return {"ok": True}
