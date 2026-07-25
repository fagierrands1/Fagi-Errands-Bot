import httpx
import os
import redis as redis_lib
from dotenv import load_dotenv

load_dotenv()

r = redis_lib.Redis(host="localhost", port=6379, decode_responses=True)

TOKEN_KEY = "handler_token"


def _base() -> str:
    return os.getenv("BACKEND_URL", "")


async def _login() -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_base()}/api/accounts/login/",
            json={
                "phone_number": os.getenv("HANDLER_PHONE"),
                "password": os.getenv("HANDLER_PASSWORD"),
            },
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        token = data.get("token", "")
        if token:
            r.set(TOKEN_KEY, token, ex=82800)  # cache for 23hrs
        return token


async def get_token() -> str:
    token = r.get(TOKEN_KEY)
    if not token:
        token = await _login()
    return token


async def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {await get_token()}",
        "Content-Type": "application/json",
    }


async def create_order(payload: dict) -> dict:
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/api/orders/create-for-client/",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            r.delete(TOKEN_KEY)
            headers = await _auth_headers()
            resp = await client.post(
                f"{_base()}/api/orders/create-for-client/",
                json=payload,
                headers=headers,
            )
        return resp.json()


async def calculate_pricing(pickup_lat: float, pickup_lng: float, delivery_lat: float, delivery_lng: float) -> dict:
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_base()}/api/orders/calculate-pricing/",
                json={
                    "pickup": f"{pickup_lat},{pickup_lng}",
                    "delivery": f"{delivery_lat},{delivery_lng}",
                    "type": "parcel",
                },
                headers=headers,
            )
            if resp.status_code == 401:
                r.delete(TOKEN_KEY)
                headers = await _auth_headers()
                resp = await client.post(
                    f"{_base()}/api/orders/calculate-pricing/",
                    json={
                        "pickup": f"{pickup_lat},{pickup_lng}",
                        "delivery": f"{delivery_lat},{delivery_lng}",
                        "type": "parcel",
                    },
                    headers=headers,
                )
            data = resp.json()
            # Backend uses straight-line (Haversine) distance.
            # Apply road correction factor of 1.3 to approximate actual road distance in Nairobi.
            raw_km = data.get("distance_km", 0)
            road_km = round(raw_km * 1.3, 2)
            BASE_KM = 7.5
            BASE_FEE = 200
            RATE_PER_KM = 23
            if road_km <= BASE_KM:
                total = BASE_FEE
            else:
                total = BASE_FEE + round((road_km - BASE_KM) * RATE_PER_KM)
            data["distance_km"] = road_km
            data["distance_text"] = f"{road_km} km"
            data["total_price"] = total
            return data
    except Exception as e:
        print(f"[calculate_pricing ERROR] {e}")
        return {}


async def get_client_orders(client_phone: str) -> list:
    # Normalize: strip non-digits, ensure it's a plausible phone number
    client_phone = "".join(c for c in client_phone if c.isdigit())
    if len(client_phone) < 7:
        return []
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/api/orders/",
            params={"client_phone": client_phone},
            headers=headers,
        )
        if resp.status_code == 401:
            r.delete(TOKEN_KEY)
            headers = await _auth_headers()
            resp = await client.get(
                f"{_base()}/api/orders/",
                params={"client_phone": client_phone},
                headers=headers,
            )
        data = resp.json()
        return data if isinstance(data, list) else []


async def get_order_status(order_id: int) -> dict:
    if not isinstance(order_id, int) or order_id <= 0:
        return {}
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{_base()}/api/orders/{order_id}/",
            headers=headers,
        )
        if resp.status_code == 401:
            r.delete(TOKEN_KEY)
            headers = await _auth_headers()
            resp = await client.get(
                f"{_base()}/api/orders/{order_id}/",
                headers=headers,
            )
        return resp.json()
