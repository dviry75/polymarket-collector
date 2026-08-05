from __future__ import annotations

from typing import Any
import httpx


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


async def geographic_preflight() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(GEOBLOCK_URL)
            response.raise_for_status()
            payload = response.json()
        return {
            "status": "BLOCKED" if payload.get("blocked") is True else "ALLOWED",
            "blocked": payload.get("blocked") is True,
            "country": str(payload.get("country") or ""),
            "region": str(payload.get("region") or ""),
        }
    except Exception as exc:
        return {
            "status": "FAILED", "blocked": None, "country": "", "region": "",
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
