import hashlib
import hmac
import os
import traceback

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

load_dotenv()

from app.router import route, r as redis
from app.whatsapp import send_message, mark_read

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")  # Meta app secret for signature verification


def _verify_signature(body: bytes, signature_header: str) -> bool:
    """Reject requests not signed by Meta."""
    if not APP_SECRET:
        return True  # skip if not configured (dev mode)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    received = signature_header[7:]
    # constant-time compare to prevent timing attacks
    return hmac.compare_digest(expected, received)


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=params.get("hub.challenge", ""), status_code=200)
    return PlainTextResponse(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw_body = await request.body()

    if not _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        return PlainTextResponse(content="Forbidden", status_code=403)

    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse(content="Bad Request", status_code=400)

    if "entry" not in body or not body["entry"]:
        return PlainTextResponse(content="OK", status_code=200)

    value = body["entry"][0].get("changes", [{}])[0].get("value", {})

    if "statuses" in value or "messages" not in value or not value["messages"]:
        return PlainTextResponse(content="OK", status_code=200)

    message = value["messages"][0]
    msg_id = message.get("id", "")
    phone = message.get("from", "")
    msg_type = message.get("type")

    # Reject obviously invalid phone numbers
    if not phone or not phone.isdigit() or len(phone) < 7:
        return PlainTextResponse(content="OK", status_code=200)

    # Deduplicate — ignore if we already processed this message ID
    if msg_id and not redis.set(f"msgid:{msg_id}", "1", nx=True, ex=120):
        return PlainTextResponse(content="OK", status_code=200)

    # Mark as read immediately — shows blue ticks before the bot replies
    if msg_id:
        await mark_read(msg_id)

    contact = value.get("contacts", [{}])[0]
    client_name = contact.get("profile", {}).get("name", phone)

    try:
        await route(phone, msg_type, message, client_name)
    except Exception as e:
        print(f"[ERROR] route() failed: {e}\n{traceback.format_exc()}")

    return PlainTextResponse(content="OK", status_code=200)
