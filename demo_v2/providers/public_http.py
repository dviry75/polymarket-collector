"""Unauthenticated public GET transport, with no trading/write API surface."""

from decimal import Decimal
from urllib.parse import urlsplit, unquote

import httpx


PUBLIC_ORIGINS = {
    "https://gamma-api.polymarket.com", "https://clob.polymarket.com",
    "https://api.binance.com", "https://data-api.binance.vision",
}


class PublicHTTP:
    def __init__(self, origin: str, *, client: httpx.AsyncClient | None = None):
        if origin not in PUBLIC_ORIGINS:
            raise ValueError("unsupported public provider origin")
        self.origin = origin
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self._owned = client is None

    async def get(self, path: str, *, params=None):
        parsed=urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.path!=path
                or any(part in (".","..") for part in unquote(path).split("/")) or "\\" in unquote(path)):
            raise ValueError("public path must be relative to the approved origin")
        allowed = {
            "https://gamma-api.polymarket.com": ("/series", "/events", "/markets"),
            "https://clob.polymarket.com": ("/book", "/markets/", "/fee-rate", "/tick-size"),
            "https://api.binance.com": ("/api/v3/klines", "/api/v3/ticker/price", "/api/v3/trades", "/api/v3/exchangeInfo"),
            "https://data-api.binance.vision": ("/api/v3/klines", "/api/v3/ticker/price", "/api/v3/trades", "/api/v3/exchangeInfo"),
        }[self.origin]
        if not any(path == prefix or path.startswith(prefix.rstrip("/")+"/") for prefix in allowed):
            raise ValueError("endpoint is outside the public data allowlist")
        response = await self.client.get(self.origin+path, params=params)
        response.raise_for_status()
        return response.json(parse_float=Decimal)

    async def close(self):
        if self._owned:
            await self.client.aclose()
