import os
import base64
import httpx
from datetime import datetime

DARAJA_BASE = "https://sandbox.safaricom.co.ke"  # change to https://api.safaricom.co.ke for production


async def _get_token() -> str:
    key = os.getenv("MPESA_CONSUMER_KEY")
    secret = os.getenv("MPESA_CONSUMER_SECRET")
    creds = base64.b64encode(f"{key}:{secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{DARAJA_BASE}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}"}
        )
        return r.json()["access_token"]


async def stk_push(phone: str, amount: int, order_id: str) -> dict:
    """Initiate STK push. phone must be 254XXXXXXXXX format."""
    shortcode = os.getenv("MPESA_SHORTCODE")
    passkey = os.getenv("MPESA_PASSKEY")
    callback_url = os.getenv("MPESA_CALLBACK_URL")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()

    token = await _get_token()
    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": order_id,
        "TransactionDesc": f"Payment for {order_id}",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{DARAJA_BASE}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"}
        )
        return r.json()
