"""Polymarket public Data API v2 access (v1 retired 2026-11-02).

v2 serves ``{"data": [...], "pagination": {"next_cursor": ...}}`` under
``/v2``, takes an opaque ``cursor`` instead of ``offset`` and answers in
snake_case. Callers were written against v1's bare camelCase lists, so every
row is returned with the v1 field names added next to the v2 ones; the
selection rules that consume them (redemption proof, fill enrichment) stay
byte-for-byte the same.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

DEFAULT_DATA_API_HOST = "https://data-api.polymarket.com"
MAX_PAGES = 500

# v2 snake_case -> v1 camelCase, for the fields existing callers read.
V1_ALIASES = {
    "proxy_wallet": "proxyWallet",
    "condition_id": "conditionId",
    "token_id": "asset",
    "transaction_hash": "transactionHash",
    "event_slug": "eventSlug",
    "usdc_size": "usdcSize",
    "outcome_index": "outcomeIndex",
    "avg_price": "avgPrice",
    "current_value": "currentValue",
    "current_price": "curPrice",
    "realized_pnl": "realizedPnl",
}


def v2_url(host: str, path: str) -> str:
    base = (host or DEFAULT_DATA_API_HOST).rstrip("/")
    if base.endswith("/v2"):
        base = base[:-3]
    return f"{base}/v2/{path.lstrip('/')}"


def with_v1_aliases(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for v2_name, v1_name in V1_ALIASES.items():
        if v2_name in row and v1_name not in out:
            out[v1_name] = row[v2_name]
    # v1 /positions called the current holding `size`; v2 says `current_size`.
    if "current_size" in row and "size" not in out:
        out["size"] = row["current_size"]
    return out


def _page(response: httpx.Response, parse_float: Callable[[str], Any] | None) -> tuple[list[dict[str, Any]], str | None]:
    response.raise_for_status()
    payload = json.loads(response.text, parse_float=parse_float) if parse_float else response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("data api v2 response was not an object")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("data api v2 data was not a list")
    cursor = (payload.get("pagination") or {}).get("next_cursor")
    return [with_v1_aliases(item) for item in data if isinstance(item, dict)], cursor or None


def _params(params: dict[str, Any], cursor: str | None) -> dict[str, Any]:
    # A cursor carries its own page size; every anchor/filter is re-sent.
    if cursor is None:
        return params
    return {**{k: v for k, v in params.items() if k != "limit"}, "cursor": cursor}


def fetch_all(
    client: httpx.Client,
    host: str,
    path: str,
    params: dict[str, Any],
    *,
    parse_float: Callable[[str], Any] | None = None,
    max_pages: int = MAX_PAGES,
    truncate: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page, cursor = _page(client.get(v2_url(host, path), params=_params(params, cursor)), parse_float)
        rows.extend(page)
        if cursor is None:
            return rows
    if truncate:
        return rows
    raise RuntimeError(f"data api v2 {path} exceeded {max_pages} pages")


async def fetch_all_async(
    client: httpx.AsyncClient,
    host: str,
    path: str,
    params: dict[str, Any],
    *,
    parse_float: Callable[[str], Any] | None = None,
    max_pages: int = MAX_PAGES,
    truncate: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        response = await client.get(v2_url(host, path), params=_params(params, cursor))
        page, cursor = _page(response, parse_float)
        rows.extend(page)
        if cursor is None:
            return rows
    if truncate:
        return rows
    raise RuntimeError(f"data api v2 {path} exceeded {max_pages} pages")
