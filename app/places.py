from .backend import get_token
import httpx, os

async def autocomplete(query: str) -> list[dict]:
    query = query.strip()[:100]  # cap length to prevent abuse
    if not query:
        return []
    token = await get_token()
    base = os.getenv("BACKEND_URL", "")
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    f"{base}/api/locations/autocomplete/",
                    params={"q": query},
                    headers={"Authorization": f"Bearer {token}"},
                )
            suggestions = r.json().get("suggestions", [])[:4]
            return [
                {
                    "id": f"{s['lat']},{s['lng']}",
                    "title": s["description"][:24],
                    "description": s["description"][:72],
                }
                for s in suggestions
            ]
        except Exception as e:
            if attempt == 1:
                raise
            await __import__("asyncio").sleep(1)
    return []

async def get_coords(place_id: str) -> tuple[float, float]:
    """place_id is 'lat,lng' — parse and validate."""
    try:
        parts = place_id.split(",")
        lat, lng = float(parts[0]), float(parts[1])
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise ValueError("Out of range")
        return lat, lng
    except Exception:
        raise ValueError(f"Invalid place_id: {place_id!r}")
