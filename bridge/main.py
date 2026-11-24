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


@app.post("/sarvam-webhook")
async def sarvam_webhook(req: Request):
    """Sarvam posts here after every call attempt, connected or not.
    Fields per Sarvam docs: status, duration, interaction_id,
    interaction_transcript, final_agent_variables, webhook_config.metadata."""
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
    lines = [f"AI call finished. Status: {status}. Duration: {duration}s."]
    if variables:
        lines.append(f"Collected on call: {variables}")
    if transcript:
        turns = "\n".join(f"- {t.get('role','')}: {t.get('content','')}" for t in transcript)
        lines.append(f"Transcript:\n{turns}")
    CALL_RESULTS[int(conversation_id)] = {
        "status": status, "duration": duration, "variables": variables,
    }
    cw_post_note(account_id, int(conversation_id), "\n".join(lines))
    cw_add_labels(account_id, int(conversation_id), [f"call-{status}"])
    return {"ok": True}


@app.get("/panel", response_class=HTMLResponse)
def panel():
    """Chatwoot Dashboard App. Register this URL in
    Chatwoot: Settings > Integrations > Dashboard Apps > Add.
    It appears as a tab inside every conversation.
    Chatwoot sends conversation+contact context via postMessage."""
    return r"""
<!doctype html><html><body style="font-family:sans-serif;padding:12px">
<h3 style="margin:0 0 8px">AI Voice Call</h3>
<div id="who">Waiting for conversation context...</div>
<label style="display:block;margin-top:10px;font-size:13px">Customer phone (type it in if the chat channel has no number, e.g. Telegram):
<input id="phoneIn" type="tel" placeholder="+91XXXXXXXXXX" style="display:block;margin-top:4px;padding:8px;width:90%;font-size:15px"></label>
<button id="callBtn" disabled style="margin-top:10px;padding:10px 14px;font-size:15px">
  Call customer with AI agent</button>
<div id="result" style="margin-top:10px;white-space:pre-wrap"></div>
<div id="debug" style="margin-top:12px;font-size:11px;color:#888;white-space:pre-wrap">debug: panel v6 loaded, no messages yet</div>
<script>
let ctx = null;
let msgCount = 0, fetchCount = 0;
function dbg(extra) {
  document.getElementById("debug").textContent =
    "debug: panel v6, messages heard: " + msgCount + ", context requests sent: " + fetchCount + (extra ? "\n" + extra : "");
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
    "Last call: " + j.status + ", " + j.duration + "s\nCollected: " + JSON.stringify(j.variables);
  else setTimeout(poll, 4000);
}
document.getElementById("callBtn").onclick = async () => {
  const c = ctx.conversation || {};
  document.getElementById("result").textContent = "Starting call...";
  const r = await fetch("/trigger-call", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({conversation_id: c.id, account_id: ctx._accountId || 1,
      phone: document.getElementById("phoneIn").value.trim(), name: ctx._name || "Customer"})});
  const j = await r.json();
  document.getElementById("result").textContent =
    j.attempt_id ? ("Call started. Attempt: " + j.attempt_id + "\nResult will appear here and as a private note in the chat.") : JSON.stringify(j);
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
    attempt_id = start_sarvam_call(phone, conversation_id, variables, account_id)
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
