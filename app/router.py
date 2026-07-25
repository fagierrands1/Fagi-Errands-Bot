import redis as redis_lib
import os
from .states import *
from .messages import *
from .whatsapp import send_message, send_list
from .places import autocomplete, get_coords
from .backend import create_order, get_order_status, get_client_orders, calculate_pricing

r = redis_lib.Redis(host="localhost", port=6379, decode_responses=True)

def get_state(phone: str) -> str:
    return r.get(f"state:{phone}") or WELCOME_MENU

def set_state(phone: str, state: str):
    r.set(f"state:{phone}", state, ex=1800)

def clear_session(phone: str):
    preserve_prefixes = ("order_", "client_name", "state")
    for key in r.scan_iter(f"*:{phone}"):
        field = key.split(f":{phone}")[0]
        if not any(field.startswith(p) for p in preserve_prefixes):
            r.delete(key)

def cache_set(phone: str, field: str, value: str):
    r.set(f"{field}:{phone}", value, ex=1800)

def cache_get(phone: str, field: str) -> str:
    return r.get(f"{field}:{phone}") or ""

def _register_order(phone: str, order_number: str, order_id: int):
    """Store minimal mapping so agent buttons can resolve order_number → phone + backend_id."""
    r.set(f"ordermap:{order_number}", phone, ex=86400)
    r.set(f"backend_id:{order_number}", str(order_id), ex=86400)

def _get_order_phone(order_number: str) -> str:
    return r.get(f"ordermap:{order_number}") or ""

def _get_backend_id(order_number: str) -> str:
    return r.get(f"backend_id:{order_number}") or ""

def _format_order(o: dict) -> dict:
    """Normalize a backend order dict to a flat shape used in the bot."""
    rider = o.get("assistant") or {}
    return {
        "id": str(o.get("id", "")),
        "order_number": o.get("order_number", ""),
        "pickup": o.get("pickup_address", ""),
        "delivery": o.get("delivery_address", ""),
        "price": o.get("total_price", ""),
        "status": o.get("status", "Pending"),
        "client_status": o.get("client_status", ""),
        "client_name": (o.get("user") or {}).get("first_name", ""),
        "client_phone": (o.get("user") or {}).get("phone_number", ""),
        "rider_name": f"{rider.get('first_name','')} {rider.get('last_name','')}".strip(),
        "rider_phone": rider.get("phone_number", ""),
        "picked_at": o.get("picked_at", ""),
        "delivered_at": o.get("delivered_at", ""),
        "distance_km": o.get("distance_km", ""),
        "created_at": o.get("created_at", ""),
    }



async def route(phone: str, msg_type: str, message: dict, client_name: str = ""):
    import time
    state = get_state(phone)
    if client_name:
        cache_set(phone, "client_name", client_name)

    agent_phone = (os.getenv("AGENT_PHONE") or "").lstrip("+")

    # Agent interactions
    if phone.lstrip("+") == agent_phone:
        await _agent_dispatch(phone, msg_type, message)
        return

    # Inactivity check — if >60s since last message and mid-flow, reset to menu
    last_active = r.get(f"last_active:{phone}")
    now = int(time.time())
    if last_active and state not in (WELCOME_MENU, HAND_OFF_TO_HUMAN):
        if now - int(last_active) > 600:
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_message(phone, "⏱ Your session timed out. Here's the main menu:")
            await send_welcome(phone)
            r.set(f"last_active:{phone}", now, ex=1800)
            return
    r.set(f"last_active:{phone}", now, ex=1800)
    r.sadd("all_clients", phone)  # permanent registry for broadcasts
    if state == HAND_OFF_TO_HUMAN:
        body = message.get("text", {}).get("body", "").strip() if msg_type == "text" else ""
        if body.lower() == "cancel":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_welcome(phone)
        elif body:
            from .llm import triage
            reply = await triage(body)
            if reply.startswith("ACTION:"):
                clear_session(phone)
                set_state(phone, WELCOME_MENU)
                await _dispatch(phone, msg_type, message, WELCOME_MENU)
            else:
                await send_message(phone, reply)
        return

    if state == RESUME_DRAFT_PROMPT:
        clear_session(phone)
        set_state(phone, WELCOME_MENU)
        await send_welcome(phone)
        return

    await _dispatch(phone, msg_type, message, state)


STATUS_LABELS = {
    "Pending": "⏳ Pending",
    "Assigned": "🛵 Rider Assigned",
    "InTransit": "🚀 In Transit",
    "Completed": "✅ Completed",
    "Cancelled": "❌ Cancelled",
}

STATUS_CLIENT_MSG = {
    "Assigned": "🛵 *Rider Assigned!*\nA rider has been assigned to your errand and is on the way to pick up your package.",
    "InTransit": "🚀 *Your Errand is In Transit!*\nYour package has been picked up and is on the way to the destination.",
    "Completed": "✅ *Errand Completed!*\nYour package has been delivered successfully. Thank you for using Fagi Errands! 🎉",
    "Cancelled": "❌ *Your Errand has been Cancelled.*\nWe're sorry for the inconvenience. We hope to serve you again next time you need reliable delivery services! 🙏",
}

async def _agent_dispatch(agent: str, msg_type: str, message: dict):
    body = ""
    order_id = None

    if msg_type == "text":
        body = message.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "list_reply":
            body = interactive["list_reply"]["id"]
        elif interactive.get("type") == "button_reply":
            body = interactive["button_reply"]["id"]

    # Broadcast to all active clients today
    if body.lower().startswith("broadcast:"):
        text = body[len("broadcast:"):].strip()
        if not text:
            await send_message(agent, "⚠️ Broadcast message is empty.")
            return
        phones = set(r.smembers("all_clients"))
        for key in r.scan_iter("client_name:*"):
            phones.add(key.replace("client_name:", ""))
        agent_bare = agent.lstrip("+")
        phones = [p for p in phones if p != agent_bare]
        count = 0
        for phone in phones:
            await send_message(phone, f"📣 *Fagi Errands Update*\n\n{text}")
            count += 1
        await send_message(agent, f"✅ Broadcast sent to {count} client(s).")
        return

    # Agent selects an order to manage
    if body.lower() == "orders":
        # Not used in current flow — agent gets notified per order
        await send_message(agent, "📭 Use the order buttons sent with each new errand.")
        return

    # Agent taps order from notification → straight to status picker
    elif body.startswith("agentorder_"):
        order_number = body.replace("agentorder_", "")
        client_phone = _get_order_phone(order_number)
        client_name = r.get(f"client_name:{client_phone}") or client_phone
        await send_list(agent,
            f"📦 *{order_number}* | {client_name}\nSelect new status:",
            "Set Status",
            [{"title": "Order Status", "rows": [
                {"id": f"agentsetstatus_{order_number}_{s}", "title": label}
                for s, label in STATUS_LABELS.items()
            ]}]
        )

    # Agent wants to update status — show status list
    elif body.startswith("agentstatus_"):
        order_number = body.replace("agentstatus_", "")
        client_phone = _get_order_phone(order_number)
        client_name = r.get(f"client_name:{client_phone}") or client_phone
        await send_list(agent,
            f"📦 *{order_number}* | {client_name}\nSelect new status:",
            "Set Status",
            [{"title": "Order Status", "rows": [
                {"id": f"agentsetstatus_{order_number}_{s}", "title": label}
                for s, label in STATUS_LABELS.items()
            ]}]
        )

    # Agent picks a specific status
    elif body.startswith("agentsetstatus_"):
        parts = body.replace("agentsetstatus_", "").rsplit("_", 1)
        order_number, new_status = parts[0], parts[1]
        client_phone = _get_order_phone(order_number)
        if not client_phone:
            await send_message(agent, "⚠️ Order not found.")
            return
        await send_message(agent, f"✅ Order *{order_number}* status noted as *{new_status}*.")
        if new_status in STATUS_CLIENT_MSG:
            await send_message(client_phone, STATUS_CLIENT_MSG[new_status])

    # Agent cancels an order
    elif body.startswith("agentcancel_") or body.lower().startswith("cancel "):
        order_number = body.replace("agentcancel_", "") if body.startswith("agentcancel_") else body.split(" ", 1)[1].strip().upper()
        client_phone = _get_order_phone(order_number)
        if not client_phone:
            await send_message(agent, "⚠️ Order not found.")
            return
        await send_message(agent, f"✅ Order *{order_number}* cancelled.")
        await send_message(client_phone, STATUS_CLIENT_MSG["Cancelled"])

    # Agent requests client contact
    elif body.startswith("agentcall_") or body.lower().startswith("call "):
        order_number = body.replace("agentcall_", "") if body.startswith("agentcall_") else body.split(" ", 1)[1].strip().upper()
        client_phone = _get_order_phone(order_number)
        if not client_phone:
            await send_message(agent, "⚠️ Order not found.")
            return
        client_name = r.get(f"client_name:{client_phone}") or client_phone
        await send_message(agent,
            f"📞 *Client Contact*\n"
            f"👤 {client_name}\n"
            f"📱 +{client_phone}"
        )


async def _handle_price_enquiry(phone: str, body: str):
    """Extract two locations from free text, geocode them, calculate price, reply."""
    from .places import autocomplete, get_coords
    from .backend import calculate_pricing

    from groq import Groq
    import json as _json
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    pickup_q, delivery_q = "", ""
    for model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
        try:
            extract = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Extract pickup and delivery locations from the message. Reply with ONLY valid JSON: {\"pickup\": \"...\", \"delivery\": \"...\"}. If you cannot find two locations, reply {\"pickup\": \"\", \"delivery\": \"\"}"},
                    {"role": "user", "content": body},
                ],
                max_tokens=60,
                temperature=0,
            )
            locs = _json.loads(extract.choices[0].message.content.strip())
            pickup_q = locs.get("pickup", "")
            delivery_q = locs.get("delivery", "")
            break
        except Exception:
            pass

    if not pickup_q or not delivery_q:
        set_state(phone, AWAITING_PRICE_LOCATIONS)
        await send_message(phone, "Sure! Tell me the pickup and delivery locations and I'll get you a price instantly 🚀")
        return

    try:
        pickup_preds = await autocomplete(pickup_q)
        delivery_preds = await autocomplete(delivery_q)
    except Exception as e:
        print(f"[autocomplete ERROR] {e}")
        await send_message(phone, "⚠️ Location service is unavailable right now. Please try again in a moment.")
        return

    if not pickup_preds and not delivery_preds:
        set_state(phone, AWAITING_PRICE_LOCATIONS)
        await send_message(phone, f"❓ Couldn't find *{pickup_q}* or *{delivery_q}*. Try using area names (e.g. Westlands, Karen).")
        return

    if not pickup_preds:
        # Save delivery, ask to clarify pickup
        for i, p in enumerate(delivery_preds):
            cache_set(phone, f"price_delivery_{i}", p["id"])
            cache_set(phone, f"price_delivery_name_{i}", p["description"])
        cache_set(phone, "price_delivery_q", delivery_q)
        set_state(phone, AWAITING_PRICE_PICKUP_CLARIFY)
        await send_message(phone, f"❓ Couldn't find *{pickup_q}*. What's the pickup area? (e.g. type the estate or landmark name)")
        return

    if not delivery_preds:
        # Save pickup, ask to clarify delivery
        for i, p in enumerate(pickup_preds):
            cache_set(phone, f"price_pickup_{i}", p["id"])
            cache_set(phone, f"price_pickup_name_{i}", p["description"])
        cache_set(phone, "price_pickup_q", pickup_q)
        set_state(phone, AWAITING_PRICE_DELIVERY_CLARIFY)
        await _send_clarify_list(phone, delivery_q, pickup_preds, "pickup", "price")
        return

    await _finish_price(phone, pickup_preds[0], delivery_preds[0])


async def _send_clarify_list(phone: str, not_found: str, found_preds: list, found_side: str, flow: str):
    """Show suggestions for the found side and ask user to clarify the not-found side."""
    from .whatsapp import send_list
    # Cache found preds
    for i, p in enumerate(found_preds):
        cache_set(phone, f"{flow}_{found_side}_{i}", p["id"])
        cache_set(phone, f"{flow}_{found_side}_name_{i}", p["description"])
    await send_message(phone,
        f"❓ Couldn't find *{not_found}*. Could you be more specific?\n\n"
        f"Type the area name, estate or a nearby landmark."
    )


async def _finish_price(phone: str, pickup_pred: dict, delivery_pred: dict):
    from .places import get_coords
    from .backend import calculate_pricing
    try:
        pickup_lat, pickup_lng = await get_coords(pickup_pred["id"])
        delivery_lat, delivery_lng = await get_coords(delivery_pred["id"])
        pricing = await calculate_pricing(pickup_lat, pickup_lng, delivery_lat, delivery_lng)
    except Exception as e:
        print(f"[_finish_price ERROR] {e}")
        await send_message(phone, "⚠️ Couldn't calculate the price right now. Please try again.")
        return
    price = pricing.get("total_price")
    distance = pricing.get("distance_km")
    # Cache locations so booking can reuse them without asking again
    cache_set(phone, "quoted_pickup_id", pickup_pred["id"])
    cache_set(phone, "quoted_pickup_name", pickup_pred["description"])
    cache_set(phone, "quoted_delivery_id", delivery_pred["id"])
    cache_set(phone, "quoted_delivery_name", delivery_pred["description"])
    dist_text = f" ({distance} km)" if distance else ""
    await send_message(phone,
        f"📦 Price estimate{dist_text}:\n"
        f"*KES {price}*\n\n"
        f"📍 From: {pickup_pred['description']}\n"
        f"🏁 To: {delivery_pred['description']}\n\n"
        f"💡 Want me to book this now? Just say *book* or reply *1* 🚀"
    )
    set_state(phone, AWAITING_BOOK_FROM_QUOTE)


async def _finish_fast_book(phone: str, pickup_pred: dict, delivery_pred: dict):
    from .places import get_coords
    pickup_lat, pickup_lng = await get_coords(pickup_pred["id"])
    delivery_lat, delivery_lng = await get_coords(delivery_pred["id"])
    cache_set(phone, "pickup_lat", str(pickup_lat))
    cache_set(phone, "pickup_lng", str(pickup_lng))
    cache_set(phone, "pickup_name", pickup_pred["description"])
    cache_set(phone, "delivery_lat", str(delivery_lat))
    cache_set(phone, "delivery_lng", str(delivery_lng))
    cache_set(phone, "delivery_name", delivery_pred["description"])
    cache_set(phone, "load_type", "Standard Parcel")
    cache_set(phone, "receiver_name", cache_get(phone, "client_name") or phone)
    cache_set(phone, "receiver_phone", "0" + phone[-9:])
    cache_set(phone, "item_description", "Parcel")
    cache_set(phone, "package_price", "0")
    await _build_invoice(phone)


async def _dispatch(phone: str, msg_type: str, message: dict, state: str):
    # Media / unsupported type rejection
    if msg_type in ["image", "video", "audio", "document", "sticker", "reaction", "contacts"]:
        await send_message(phone, "❓ Please reply using the menu options only — images, stickers and files are not supported.")
        return

    # Extract input
    body = ""
    if msg_type == "text":
        body = message.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "list_reply":
            body = interactive["list_reply"]["id"]
        elif interactive.get("type") == "button_reply":
            body = interactive["button_reply"]["id"]
    location = message.get("location") if msg_type == "location" else None

    # Global cancel — works from any state
    if body.lower() == "cancel":
        clear_session(phone)
        set_state(phone, WELCOME_MENU)
        await send_session_ended(phone)
        await send_welcome(phone)
        return

    if state == WELCOME_MENU:
        if body == "1":
            set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
            await send_message(phone,
                "🛵 *Quick Book — Step 1 of 3*\n\n"
                "Where are we picking up and delivering?\n\n"
                "Reply in this format:\n"
                "*Pickup location, Delivery location*\n\n"
                "Example: _Westlands, Karen_\n\n"
                "Type *cancel* to go back."
            )
        elif body == "2":
            from datetime import date
            set_state(phone, CHECKING_STATUS)
            local_phone = "0" + phone[-9:]
            raw_orders = await get_client_orders(local_phone)
            today = date.today().isoformat()
            orders = [
                _format_order(o) for o in raw_orders
                if o.get("status") not in ("Cancelled", "Completed", "Draft")
                and (o.get("created_at", "") or "")[:10] == today
            ]
            if orders:
                await send_message(phone, _format_orders_text(orders))
                from .whatsapp import send_buttons
                await send_buttons(phone,
                    "What would you like to do?",
                    [("status_book", "🛵 Book Another"), ("3", "🤝 Talk to Agent")]
                )
            else:
                await send_no_status(phone)
        elif body == "3":
            await _handoff(phone, "Direct Main Menu Agent Request (Option 3)")
        elif body == "4":
            await send_message(phone,
                "📱 *Download Fagi Errands App*\n\n"
                "https://play.google.com/store/apps/details?id=com.fagierrands.fagierrandsservices"
            )
        elif body == "5":
            await send_message(phone,
                "🌐 *Visit Our Website*\n\n"
                "https://fagierrands.com"
            )
        elif body == "6":
            await send_rates(phone)
        elif msg_type == "text" and body:
            from .llm import triage
            intent = await triage(body)
            print(f"[LLM] body={repr(body)} intent={repr(intent)}")
            # Normalize — extract ACTION even if LLM added extra text
            action = None
            for token in intent.split():
                if token.startswith("ACTION:"):
                    action = token
                    break
            if action == "ACTION:price":
                await _handle_price_enquiry(phone, body)
            elif action == "ACTION:book":
                set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
                await send_message(phone,
                    "🛵 *Quick Book — Step 1 of 3*\n\n"
                    "Where are we picking up and delivering?\n\n"
                    "Reply in this format:\n"
                    "*Pickup location, Delivery location*\n\n"
                    "Example: _Westlands, Karen_\n\n"
                    "Type *cancel* to go back."
                )
            elif action == "ACTION:status":
                set_state(phone, CHECKING_STATUS)
                local_phone = "0" + phone[-9:]
                from datetime import date
                raw_orders = await get_client_orders(local_phone)
                today = date.today().isoformat()
                orders = [
                    _format_order(o) for o in raw_orders
                    if o.get("status") not in ("Cancelled", "Completed", "Draft")
                    and (o.get("created_at", "") or "")[:10] == today
                ]
                if orders:
                    await send_message(phone, _format_orders_text(orders))
                    from .whatsapp import send_buttons
                    await send_buttons(phone, "What would you like to do?",
                        [("status_book", "🛵 Book Another"), ("3", "🤝 Talk to Agent")])
                else:
                    await send_no_status(phone)
            elif action == "ACTION:agent":
                await _handoff(phone, f"LLM routed: {body[:80]}")
            elif action == "ACTION:menu":
                await send_welcome(phone)
            else:
                # Plain chat reply from LLM
                await send_message(phone, intent)
        else:
            await send_welcome(phone)

    elif state == AWAITING_BOOK_FROM_QUOTE:
        # Client got a price quote — locations already cached, go straight to invoice
        quoted_pickup = {"id": cache_get(phone, "quoted_pickup_id"), "description": cache_get(phone, "quoted_pickup_name")}
        quoted_delivery = {"id": cache_get(phone, "quoted_delivery_id"), "description": cache_get(phone, "quoted_delivery_name")}
        if body.lower() in ("1", "book", "yes", "ok", "okay", "confirm", "ndio", "sawa"):
            await _finish_fast_book(phone, quoted_pickup, quoted_delivery)
        elif body.lower() == "cancel":
            clear_session(phone); set_state(phone, WELCOME_MENU)
            await send_session_ended(phone); await send_welcome(phone)
        else:
            # They said something else — let LLM handle but keep quote context
            from .llm import triage
            intent = await triage(body)
            action = next((t for t in intent.split() if t.startswith("ACTION:")), None)
            if action == "ACTION:book":
                await _finish_fast_book(phone, quoted_pickup, quoted_delivery)
            else:
                await send_message(phone, intent)

    elif state == AWAITING_PRICE_LOCATIONS:
        if body.lower() == "cancel":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
            await send_welcome(phone)
        else:
            set_state(phone, WELCOME_MENU)
            await _handle_price_enquiry(phone, body)

    elif state == AWAITING_FAST_BOOK_LOCATIONS:
        if body.lower() == "cancel":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
            await send_welcome(phone)
            return
        parts = [p.strip() for p in body.split(",", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            # Client gave only one location — treat it as pickup, ask for delivery
            pickup_q = parts[0]
            try:
                pickup_preds = await autocomplete(pickup_q)
            except Exception:
                await send_message(phone, "⚠️ Location service unavailable. Please try again.")
                return
            if pickup_preds:
                for i, p in enumerate(pickup_preds):
                    cache_set(phone, f"book_pickup_{i}", p["id"])
                    cache_set(phone, f"book_pickup_name_{i}", p["description"])
                set_state(phone, AWAITING_BOOK_DELIVERY_CLARIFY)
                await send_message(phone, f"📍 Got it — picking up from *{pickup_preds[0]['description']}*\n\nWhere should we deliver to?")
            else:
                await send_message(phone, f"❓ Couldn't find *{pickup_q}*. Try: *Pickup location, Delivery location*\n\nExample: _Westlands, Karen_")
            return
        pickup_q, delivery_q = parts[0], parts[1]
        try:
            pickup_preds = await autocomplete(pickup_q)
            delivery_preds = await autocomplete(delivery_q)
        except Exception as e:
            print(f"[fast_book autocomplete ERROR] {e}")
            await send_message(phone, "⚠️ Location service is unavailable right now. Please try again.")
            return

        if not pickup_preds and not delivery_preds:
            await send_message(phone, f"❓ Couldn't find *{pickup_q}* or *{delivery_q}*. Try using area names (e.g. Westlands, Karen).")
            return

        if not pickup_preds:
            for i, p in enumerate(delivery_preds):
                cache_set(phone, f"book_delivery_{i}", p["id"])
                cache_set(phone, f"book_delivery_name_{i}", p["description"])
            set_state(phone, AWAITING_BOOK_PICKUP_CLARIFY)
            await send_message(phone, f"❓ Couldn't find *{pickup_q}*. What's the pickup area? Type the estate or a nearby landmark.")
            return

        if not delivery_preds:
            for i, p in enumerate(pickup_preds):
                cache_set(phone, f"book_pickup_{i}", p["id"])
                cache_set(phone, f"book_pickup_name_{i}", p["description"])
            set_state(phone, AWAITING_BOOK_DELIVERY_CLARIFY)
            await send_message(phone, f"❓ Couldn't find *{delivery_q}*. What's the delivery area? Type the estate or a nearby landmark.")
            return

        await _finish_fast_book(phone, pickup_preds[0], delivery_preds[0])

    elif state == AWAITING_PRICE_PICKUP_CLARIFY:
        if body.lower() == "cancel":
            clear_session(phone); set_state(phone, WELCOME_MENU)
            await send_session_ended(phone); await send_welcome(phone); return
        try:
            pickup_preds = await autocomplete(body)
        except Exception:
            await send_message(phone, "⚠️ Location service unavailable. Please try again."); return
        if not pickup_preds:
            await send_message(phone, f"❓ Still couldn't find *{body}*. Try a nearby landmark or estate name."); return
        delivery_pred = {"id": cache_get(phone, "price_delivery_0"), "description": cache_get(phone, "price_delivery_name_0")}
        await _finish_price(phone, pickup_preds[0], delivery_pred)

    elif state == AWAITING_PRICE_DELIVERY_CLARIFY:
        if body.lower() == "cancel":
            clear_session(phone); set_state(phone, WELCOME_MENU)
            await send_session_ended(phone); await send_welcome(phone); return
        try:
            delivery_preds = await autocomplete(body)
        except Exception:
            await send_message(phone, "⚠️ Location service unavailable. Please try again."); return
        if not delivery_preds:
            await send_message(phone, f"❓ Still couldn't find *{body}*. Try a nearby landmark or estate name."); return
        pickup_pred = {"id": cache_get(phone, "price_pickup_0"), "description": cache_get(phone, "price_pickup_name_0")}
        await _finish_price(phone, pickup_pred, delivery_preds[0])

    elif state == AWAITING_BOOK_PICKUP_CLARIFY:
        if body.lower() == "cancel":
            clear_session(phone); set_state(phone, WELCOME_MENU)
            await send_session_ended(phone); await send_welcome(phone); return
        try:
            pickup_preds = await autocomplete(body)
        except Exception:
            await send_message(phone, "⚠️ Location service unavailable. Please try again."); return
        if not pickup_preds:
            await send_message(phone, f"❓ Still couldn't find *{body}*. Try a nearby landmark or estate name."); return
        delivery_pred = {"id": cache_get(phone, "book_delivery_0"), "description": cache_get(phone, "book_delivery_name_0")}
        await _finish_fast_book(phone, pickup_preds[0], delivery_pred)

    elif state == AWAITING_BOOK_DELIVERY_CLARIFY:
        if body.lower() == "cancel":
            clear_session(phone); set_state(phone, WELCOME_MENU)
            await send_session_ended(phone); await send_welcome(phone); return
        try:
            delivery_preds = await autocomplete(body)
        except Exception:
            await send_message(phone, "⚠️ Location service unavailable. Please try again."); return
        if not delivery_preds:
            await send_message(phone, f"❓ Still couldn't find *{body}*. Try a nearby landmark or estate name."); return
        pickup_pred = {"id": cache_get(phone, "book_pickup_0"), "description": cache_get(phone, "book_pickup_name_0")}
        await _finish_fast_book(phone, pickup_pred, delivery_preds[0])

    elif state == AWAITING_LOAD_TYPE:
        if body == "1":
            cache_set(phone, "load_type", "Standard Parcel")
            set_state(phone, AWAITING_PICKUP_LOCATION)
            await send_pickup_prompt(phone)
        elif body == "2":
            cache_set(phone, "load_type", "Cargo / Large Items")
            set_state(phone, AWAITING_PICKUP_LOCATION)
            await send_pickup_prompt(phone)
        elif body == "4":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        else:
            await send_invalid(phone)

    elif state == AWAITING_PICKUP_LOCATION:
        if body == "4":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        elif location:
            cache_set(phone, "pickup_lat", str(location["latitude"]))
            cache_set(phone, "pickup_lng", str(location["longitude"]))
            set_state(phone, AWAITING_DELIVERY_LOCATION)
            await send_delivery_prompt(phone)
        else:
            predictions = await autocomplete(body)
            if not predictions:
                await send_message(phone, "❓ No locations found. Try a different search term.")
            else:
                for i, p in enumerate(predictions):
                    cache_set(phone, f"place_pickup_{i}", p["id"])
                    cache_set(phone, f"place_pickup_name_{i}", p["description"])
                set_state(phone, AWAITING_PICKUP_CONFIRM)
                await send_list(phone,
                    f"📍 Results for *{body}*\nSelect your pickup location:",
                    "Choose Location",
                    [{"title": "Pickup Locations", "rows": [
                        {"id": f"pickup_{i}", "title": p["title"], "description": p["description"]}
                        for i, p in enumerate(predictions)
                    ]}]
                )

    elif state == AWAITING_PICKUP_CONFIRM:
        if body == "4":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        elif body.startswith("pickup_"):
            idx = int(body.split("_")[1])
            place_id = cache_get(phone, f"place_pickup_{idx}")
            place_name = cache_get(phone, f"place_pickup_name_{idx}")
            lat, lng = await get_coords(place_id)
            cache_set(phone, "pickup_lat", str(lat))
            cache_set(phone, "pickup_lng", str(lng))
            cache_set(phone, "pickup_name", place_name)
            set_state(phone, AWAITING_DELIVERY_LOCATION)
            await send_delivery_prompt(phone)
        else:
            await send_invalid(phone)

    elif state == AWAITING_DELIVERY_LOCATION:
        if body == "4":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        elif location:
            cache_set(phone, "delivery_lat", str(location["latitude"]))
            cache_set(phone, "delivery_lng", str(location["longitude"]))
            set_state(phone, AWAITING_RECEIVER_DETAILS)
            await send_receiver_prompt(phone)
        else:
            predictions = await autocomplete(body)
            if not predictions:
                await send_message(phone, "❓ No locations found. Try a different search term.")
            else:
                for i, p in enumerate(predictions):
                    cache_set(phone, f"place_delivery_{i}", p["id"])
                    cache_set(phone, f"place_delivery_name_{i}", p["description"])
                set_state(phone, AWAITING_DELIVERY_CONFIRM)
                await send_list(phone,
                    f"🗺 Results for *{body}*\nSelect your delivery location:",
                    "Choose Location",
                    [{"title": "Delivery Locations", "rows": [
                        {"id": f"delivery_{i}", "title": p["title"], "description": p["description"]}
                        for i, p in enumerate(predictions)
                    ]}]
                )

    elif state == AWAITING_DELIVERY_CONFIRM:
        if body == "4":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        elif body.startswith("delivery_"):
            idx = int(body.split("_")[1])
            place_id = cache_get(phone, f"place_delivery_{idx}")
            place_name = cache_get(phone, f"place_delivery_name_{idx}")
            lat, lng = await get_coords(place_id)
            cache_set(phone, "delivery_lat", str(lat))
            cache_set(phone, "delivery_lng", str(lng))
            cache_set(phone, "delivery_name", place_name)
            set_state(phone, AWAITING_RECEIVER_DETAILS)
            await send_receiver_prompt(phone)
        else:
            await send_invalid(phone)

    elif state == AWAITING_RECEIVER_DETAILS:
        if body == "4" or body.lower() == "cancel":
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
            return
        client_name = cache_get(phone, "client_name") or phone
        # Format phone to local (0XXXXXXXXX)
        local_phone = phone[-9:].zfill(10) if len(phone) > 9 else phone
        local_phone = "0" + local_phone[-9:]
        if body.lower() == "skip":
            cache_set(phone, "receiver_name", client_name)
            cache_set(phone, "receiver_phone", local_phone)
            cache_set(phone, "item_description", "Parcel")
            cache_set(phone, "package_price", "0")
        else:
            parts = [p.strip() for p in body.split(",")]
            if len(parts) < 2:
                await send_message(phone,
                    "❓ Please use the format:\n*Item Value (KES), Receiver Phone*\n\nExample: _5000, 0712345678_\n\nOr type *skip* to use your own details.")
                return
            cache_set(phone, "item_description", "Parcel")
            cache_set(phone, "package_price", parts[0] if parts[0].isdigit() else "0")
            cache_set(phone, "receiver_name", client_name)
            cache_set(phone, "receiver_phone", parts[1])
        await _build_invoice(phone)

    elif state == AWAITING_INVOICE_CONFIRMATION:
        pickup_lat = cache_get(phone, "pickup_lat")
        pickup_lng = cache_get(phone, "pickup_lng")
        delivery_lat = cache_get(phone, "delivery_lat")
        delivery_lng = cache_get(phone, "delivery_lng")
        load_type = cache_get(phone, "load_type")
        price = cache_get(phone, "price")
        distance = cache_get(phone, "distance")
        agent_phone = os.getenv("AGENT_PHONE")

        if body == "1":
            pickup_name = cache_get(phone, "pickup_name") or f"https://maps.google.com/?q={pickup_lat},{pickup_lng}"
            delivery_name = cache_get(phone, "delivery_name") or f"https://maps.google.com/?q={delivery_lat},{delivery_lng}"
            client_name = cache_get(phone, "client_name") or phone
            item_description = cache_get(phone, "item_description") or "Parcel"
            estimated_value = int(cache_get(phone, "package_price") or 0)
            receiver_name = cache_get(phone, "receiver_name") or client_name
            receiver_phone = cache_get(phone, "receiver_phone") or phone

            # Format client phone to local 0XXXXXXXXX
            local_client_phone = "0" + phone[-9:]

            payload = {
                "client_phone": local_client_phone,
                "title": "WhatsApp Errand",
                "item_description": item_description,
                "pickup_address": pickup_name,
                "pickup_lat": float(pickup_lat),
                "pickup_lng": float(pickup_lng),
                "delivery_address": delivery_name,
                "delivery_lat": float(delivery_lat),
                "delivery_lng": float(delivery_lng),
                "receiver_name": receiver_name,
                "receiver_phone": receiver_phone,
                "distance_km": float(distance),
                "base_price": int(price),
                "total_price": int(price),
                "estimated_value": estimated_value,
                "payment_method": "cash",
                "order_type": 1,
            }

            result = await create_order(payload)

            if "error" in result:
                error_msg = result["error"]
                clear_session(phone)
                set_state(phone, WELCOME_MENU)
                if "register" in error_msg.lower():
                    await send_message(phone,
                        "⚠️ *Account Not Found*\n\n"
                        "You need to register first to place an errand.\n\n"
                        "📱 Download the app:\nhttps://play.google.com/store/search?q=fagi+errands\n\n"
                        "🌐 Or register on our website:\nhttps://fagierrands.com"
                    )
                else:
                    await send_message(phone, f"⚠️ Could not place your order: {error_msg}")
                return

            order_number = result.get("order_number", "N/A")
            order_id = result.get("order_id")
            total_price = result.get("total_price", price)

            clear_session(phone)
            set_state(phone, ORDER_ACTIVE)
            _register_order(phone, order_number, order_id)

            await _send_order_menu(phone, 1)
            await send_message(agent_phone,
                f"🚴 NEW ERRAND — ACTION REQUIRED\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔖 Order: {order_number}\n"
                f"👤 Client: {client_name}\n"
                f"📞 WhatsApp: +{phone}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Load Type: {load_type}\n"
                f"📏 Distance: {distance} km\n"
                f"💵 Fare: KES {total_price}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Pickup: {pickup_name}\n"
                f"🏁 Delivery: {delivery_name}\n"
                f"📦 Item: {item_description}\n"
                f"👤 Receiver: {receiver_name} ({receiver_phone})"
            )
            from .whatsapp import send_buttons
            await send_buttons(agent_phone,
                "Manage this order:",
                [
                    (f"agentstatus_{order_number}", "📋 Update Status"),
                    (f"agentcancel_{order_number}", "❌ Cancel Order"),
                    (f"agentcall_{order_number}", "📞 Call Client"),
                ]
            )
        elif body in ["2", "4"]:
            clear_session(phone)
            set_state(phone, WELCOME_MENU)
            await send_session_ended(phone)
        else:
            await send_invalid(phone)

    elif state == ORDER_ACTIVE:
        from datetime import date
        local_phone = "0" + phone[-9:]
        raw_orders = await get_client_orders(local_phone)
        today = date.today().isoformat()
        orders = [
            _format_order(o) for o in raw_orders
            if o.get("status") not in ("Cancelled", "Completed", "Draft")
            and (o.get("created_at", "") or "")[:10] == today
        ]
        if body == "track_order":
            if not orders:
                await send_message(phone, "📭 No active errands found for today.")
                await _send_order_menu(phone, 0)
            else:
                await send_message(phone, _format_orders_text(orders))
                await _send_order_menu(phone, len(orders))
        elif body.startswith("view_order_"):
            idx = int(body.split("_")[2])
            o = orders[idx] if idx < len(orders) else None
            if o:
                await send_message(phone,
                    f"📦 *Order #{o['id']}*\n"
                    f"📍 {o['pickup']}\n"
                    f"🏁 {o['delivery']}\n"
                    f"💵 KES {o['price']}\n"
                    f"Status: {STATUS_LABELS.get(o['status'], o['status'])}"
                )
            await _send_order_menu(phone, len(orders))
        elif body == "new_order":
            set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
            await send_message(phone,
                "🛵 *Quick Book — Step 1 of 3*\n\n"
                "Where are we picking up and delivering?\n\n"
                "Reply in this format:\n"
                "*Pickup location, Delivery location*\n\n"
                "Example: _Westlands, Karen_\n\n"
                "Type *cancel* to go back."
            )
        elif body == "3":
            await _handoff(phone, f"Agent request with {len(orders)} active order(s)")
        elif msg_type == "text" and body:
            from .llm import triage
            intent = await triage(body)
            action = next((t for t in intent.split() if t.startswith("ACTION:")), None)
            if action == "ACTION:book":
                set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
                await send_message(phone,
                    "🛵 *Quick Book — Step 1 of 3*\n\n"
                    "Where are we picking up and delivering?\n\n"
                    "Reply in this format:\n"
                    "*Pickup location, Delivery location*\n\n"
                    "Example: _Westlands, Karen_\n\n"
                    "Type *cancel* to go back."
                )
            elif action == "ACTION:status":
                if not orders:
                    await send_message(phone, "📭 No active errands found for today.")
                    await _send_order_menu(phone, 0)
                else:
                    await send_message(phone, _format_orders_text(orders))
                    await _send_order_menu(phone, len(orders))
            elif action == "ACTION:agent":
                await _handoff(phone, f"Agent request with {len(orders)} active order(s)")
            elif action == "ACTION:price":
                await _handle_price_enquiry(phone, body)
            else:
                await send_message(phone, intent)
        else:
            # non-text (button tap etc.) with no matching handler — show menu
            await _send_order_menu(phone, len(orders))

    elif state == CHECKING_STATUS:
        if body in ("1", "status_book"):
            set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
            await send_message(phone,
                "🛵 *Quick Book — Step 1 of 3*\n\n"
                "Where are we picking up and delivering?\n\n"
                "Reply in this format:\n"
                "*Pickup location, Delivery location*\n\n"
                "Example: _Westlands, Karen_\n\n"
                "Type *cancel* to go back."
            )
        elif body in ("0", "status_home"):
            set_state(phone, WELCOME_MENU)
            await send_welcome(phone)
        elif body == "3":
            from datetime import date
            local_phone = "0" + phone[-9:]
            raw_orders = await get_client_orders(local_phone)
            today = date.today().isoformat()
            orders = [
                _format_order(o) for o in raw_orders
                if o.get("status") not in ("Cancelled", "Completed", "Draft")
                and (o.get("created_at", "") or "")[:10] == today
            ]
            client_name = cache_get(phone, "client_name") or phone
            reason = f"Status check agent request\n👤 {client_name} | +{phone}"
            if orders:
                reason += "\n\n" + _format_orders_text(orders)
            await _handoff(phone, reason)
        elif body.startswith("statusview_"):
            order_id = body.replace("statusview_", "")
            try:
                tracking = await get_order_status(int(order_id))
                o = _format_order(tracking)
            except Exception:
                o = None
            if o:
                from .whatsapp import send_buttons
                await send_buttons(phone,
                    f"📦 *Order #{o['id']}*\n"
                    f"📍 {o['pickup'][:40]}\n"
                    f"🏁 {o['delivery'][:40]}\n"
                    f"💵 KES {o['price']}\n"
                    f"Status: {STATUS_LABELS.get(o['status'], o['status'])}",
                    [
                        ("status_book", "🛵 Book Another"),
                        ("3", "🤝 Talk to Agent"),
                    ]
                )
        else:
            from .llm import triage
            intent = await triage(body)
            action = next((t for t in intent.split() if t.startswith("ACTION:")), None)
            if action == "ACTION:status":
                local_phone = "0" + phone[-9:]
                from datetime import date
                raw_orders = await get_client_orders(local_phone)
                today = date.today().isoformat()
                orders = [
                    _format_order(o) for o in raw_orders
                    if o.get("status") not in ("Cancelled", "Completed", "Draft")
                    and (o.get("created_at", "") or "")[:10] == today
                ]
                if orders:
                    await send_message(phone, _format_orders_text(orders))
                    await _send_order_menu(phone, len(orders))
                else:
                    await send_no_status(phone)
            elif action == "ACTION:book":
                set_state(phone, AWAITING_FAST_BOOK_LOCATIONS)
                await send_message(phone,
                    "🛵 *Quick Book*\n\nReply with:\n*Pickup location, Delivery location*\n\nExample: _Westlands, Karen_"
                )
            elif action == "ACTION:agent":
                await _handoff(phone, f"Agent request from status screen")
            else:
                await send_message(phone, intent)


def _format_orders_text(orders: list) -> str:
    nums = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["📦 *Today's Errands*\n"]
    for i, o in enumerate(orders):
        n = nums[i] if i < len(nums) else f"{i+1}."
        status_label = STATUS_LABELS.get(o['status'], o['status'])
        rider_line = f"\n   🏍 Rider: {o['rider_name']} ({o['rider_phone']})" if o.get('rider_name') else ""
        picked_line = f"\n   ✅ Picked up" if o.get('picked_at') else ""
        lines.append(
            f"{n} *{o.get('order_number', 'Order #' + o['id'])}*\n"
            f"   📍 {o['pickup'][:40]}\n"
            f"   🏁 {o['delivery'][:40]}\n"
            f"   💵 KES {o['price']}"
            f"{rider_line}"
            f"{picked_line}\n"
            f"   🔄 Status: *{status_label}*"
        )
    return "\n\n".join(lines)


async def _send_order_menu(phone: str, order_count: int = 1):
    from .whatsapp import send_buttons
    buttons = [("track_order", "📍 Track My Order")]
    if order_count < 2:
        buttons.append(("new_order", "➕ Place Another"))
    buttons.append(("3", "🤝 Talk to Agent"))
    count_text = f"{order_count} active errand{'s' if order_count > 1 else ''}"
    await send_buttons(phone, f"📦 You have {count_text}.", buttons)


async def _build_invoice(phone: str):
    pickup_lat = cache_get(phone, "pickup_lat")
    pickup_lng = cache_get(phone, "pickup_lng")
    delivery_lat = cache_get(phone, "delivery_lat")
    delivery_lng = cache_get(phone, "delivery_lng")
    if not all([pickup_lat, pickup_lng, delivery_lat, delivery_lng]):
        await send_message(phone, "⚠️ Could not read your locations. Please start again.")
        clear_session(phone)
        set_state(phone, WELCOME_MENU)
        return
    if pickup_lat == delivery_lat and pickup_lng == delivery_lng:
        await send_message(phone, "⚠️ Pickup and delivery locations are the same. Please enter different locations.")
        set_state(phone, AWAITING_DELIVERY_LOCATION)
        await send_delivery_prompt(phone)
        return
    pricing = await calculate_pricing(float(pickup_lat), float(pickup_lng), float(delivery_lat), float(delivery_lng))
    if not pricing:
        await send_message(phone, "⚠️ Could not calculate pricing right now. Please try again.")
        clear_session(phone)
        set_state(phone, WELCOME_MENU)
        return
    distance = pricing.get("distance_km", 0)
    price = pricing.get("total_price", 0)
    cache_set(phone, "distance", str(distance))
    cache_set(phone, "price", str(price))
    set_state(phone, AWAITING_INVOICE_CONFIRMATION)
    load_type = cache_get(phone, "load_type")
    await send_message(phone,
        f"🧾 *Errand Invoice & Summary*\n\n"
        f"● 📦 Load Type: {load_type}\n"
        f"● 📏 Distance: {distance} km\n"
        f"● 💵 Delivery Fee: KES {price}"
    )
    from .whatsapp import send_buttons
    await send_buttons(phone,
        "Confirm your booking:",
        [
            ("1", "✅ Confirm & Dispatch"),
            ("2", "❌ Cancel Order"),
            ("4", "🚪 End Session"),
        ]
    )


async def _handoff(phone: str, reason: str):
    from datetime import datetime, timezone, timedelta
    nairobi = timezone(timedelta(hours=3))
    hour = datetime.now(nairobi).hour  # 0-23
    if not (7 <= hour < 20):
        await send_message(phone,
            "Our agents are available from 7am to 8pm. It's currently outside working hours.\n\n"
            "I'm still here to help — you can book an errand, check rates, or check your order status. "
            "If you need urgent help, reply *1* to book or describe your issue and I'll do my best."
        )
        return
    set_state(phone, HAND_OFF_TO_HUMAN)
    await send_agent_handoff(phone)
    agent_phone = os.getenv("AGENT_PHONE")
    if agent_phone:
        await send_message(agent_phone,
            f"🚨 PRIORITY CALL REQUEST\n"
            f"● Client WhatsApp: {phone}\n"
            f"● Request Type: {reason}\n"
            f"● Action: Call the client immediately to handle their request manually."
        )


def _haversine(lat1, lng1, lat2, lng2) -> float:
    from math import radians, sin, cos, sqrt, atan2
    R = 6371
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _calculate_price(distance_km: float) -> int:
    if distance_km <= 7.5:
        return 200
    return int(200 + (distance_km - 7.5) * 23)
