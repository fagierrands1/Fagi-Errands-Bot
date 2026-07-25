from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse
import os
from dotenv import load_dotenv
from router import route
from whatsapp import send_message

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    return PlainTextResponse(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()

    if "entry" not in body or not body["entry"]:
        return PlainTextResponse(content="OK", status_code=200)

    value = body["entry"][0].get("changes", [{}])[0].get("value", {})

    if "statuses" in value or "messages" not in value or not value["messages"]:
        return PlainTextResponse(content="OK", status_code=200)

    message = value["messages"][0]
    msg_id = message.get("id", "")
    phone = message.get("from")
    msg_type = message.get("type")

    # Deduplicate — ignore if we already processed this message ID
    from router import r as redis
    if msg_id and not redis.set(f"msgid:{msg_id}", "1", nx=True, ex=120):
        return PlainTextResponse(content="OK", status_code=200)

    contact = value.get("contacts", [{}])[0]
    client_name = contact.get("profile", {}).get("name", phone)

    try:
        await route(phone, msg_type, message, client_name)
    except Exception as e:
        import traceback
        print(f"[ERROR] route() failed: {e}\n{traceback.format_exc()}")

    return PlainTextResponse(content="OK", status_code=200)


# @app.post("/status-update")
# async def status_update(request: Request):
#     """
#     Called by the backend when an order status changes.
#     Expected payload: {"order_number": "ORD-123", "status": "Assigned"}
#     """
#     body = await request.json()
#     order_number = body.get("order_number", "")
#     new_status = body.get("status", "")
#
#     msg = STATUS_CLIENT_MSG.get(new_status)
#     if not msg:
#         return PlainTextResponse(content="OK", status_code=200)
#
#     client_phone = _get_order_phone(order_number)
#     if not client_phone:
#         print(f"[STATUS-UPDATE] No phone found for order {order_number}")
#         return PlainTextResponse(content="OK", status_code=200)
#
#     await send_message(client_phone, msg)
#     print(f"[STATUS-UPDATE] {order_number} → {new_status} → {client_phone}")
#     return PlainTextResponse(content="OK", status_code=200)
