import httpx, os

_TOKEN = None
_PHONE_ID = None
_WA_URL = None


def _wa_headers() -> tuple[str, dict]:
    global _TOKEN, _PHONE_ID, _WA_URL
    if _TOKEN is None:
        _TOKEN = os.getenv("WHATSAPP_TOKEN", "")
        _PHONE_ID = os.getenv("PHONE_NUMBER_ID", "")
        _WA_URL = f"https://graph.facebook.com/v19.0/{_PHONE_ID}/messages"
    return _WA_URL, {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}


async def send_image(to: str, media_id: str):
    url, headers = _wa_headers()
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": media_id}}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"[SEND image] to={to} status={r.status_code}")


async def send_message(to: str, text: str):
    url, headers = _wa_headers()
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"[SEND] to={to} status={r.status_code}")


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]):
    """buttons: list of (id, title) tuples, max 3"""
    url, headers = _wa_headers()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons[:3]]
            }
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"[SEND buttons] to={to} status={r.status_code} {r.text}")


async def send_list(to: str, body: str, button_label: str, sections: list[dict]):
    """sections: [{"title": str, "rows": [{"id": str, "title": str, "description": str}]}]"""
    url, headers = _wa_headers()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {
                "button": button_label[:20],
                "sections": sections
            }
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"[SEND list] to={to} status={r.status_code} {r.text}")
