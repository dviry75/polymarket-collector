import json
from datetime import datetime, timedelta, timezone
from typing import Any

import truststore

truststore.inject_into_ssl()

import requests


GAMMA_EVENT_BY_SLUG_URL = "https://gamma-api.polymarket.com/events/slug/{slug}"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
TRADES_URL = "https://data-api.polymarket.com/trades"


BTC_5M_TERMS = [
    "bitcoin",
    "btc",
    "updown",
    "up or down",
    "5m",
    "5 min",
    "5-minute",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def floor_to_5m_epoch(dt: datetime) -> int:
    timestamp = int(dt.timestamp())
    return (timestamp // 300) * 300


def btc_5m_candidate_slugs(now: datetime | None = None) -> list[str]:
    base = floor_to_5m_epoch(now or utc_now())
    return [f"btc-updown-5m-{epoch}" for epoch in [base - 300, base, base + 300, base + 600]]


def fetch_active_events(events_url: str, limit: int) -> list[dict[str, Any]]:
    response = requests.get(
        events_url,
        params={"active": "true", "closed": "false", "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("events") or payload.get("results")
        if isinstance(data, list):
            return data

    raise ValueError("Unexpected Polymarket Gamma API response format.")


def fetch_event_by_slug(slug: str) -> dict[str, Any] | None:
    response = requests.get(GAMMA_EVENT_BY_SLUG_URL.format(slug=slug), timeout=10)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def find_btc_up_down_5m_markets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    discovered_at = utc_now_iso()
    matches: list[dict[str, Any]] = []

    for event in events:
        for market in event.get("markets") or []:
            if is_btc_up_down_5m_match(event, market):
                matches.append(build_market_row(event, market, discovered_at))

    return matches


def discover_current_btc_5m_market() -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for slug in btc_5m_candidate_slugs():
        event = fetch_event_by_slug(slug)
        if not event:
            continue
        for market in event.get("markets") or []:
            row = build_market_row(event, market, utc_now_iso())
            if row.get("status") == "discovered":
                matches.append(row)

    now = utc_now()
    open_matches = [
        match
        for match in matches
        if not (parse_datetime(match.get("end_time")) and parse_datetime(match.get("end_time")) <= now)
    ]
    if not open_matches:
        return None

    open_matches.sort(key=lambda row: row.get("start_time") or row.get("end_time") or "")
    return open_matches[0]


def is_btc_up_down_5m_match(event: dict[str, Any], market: dict[str, Any]) -> bool:
    searchable_text = " ".join(
        str(value or "")
        for value in [
            event.get("title"),
            event.get("slug"),
            market.get("question"),
            market.get("slug"),
        ]
    ).lower()

    has_asset = "bitcoin" in searchable_text or "btc" in searchable_text
    has_direction = "updown" in searchable_text or "up or down" in searchable_text
    has_window = any(term in searchable_text for term in ["5m", "5 min", "5-minute"])
    return has_asset and has_direction and has_window


def build_market_row(event: dict[str, Any], market: dict[str, Any], discovered_at: str) -> dict[str, Any]:
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    clob_token_ids = parse_jsonish_array(market.get("clobTokenIds") or market.get("clob_token_ids"))
    outcomes = parse_jsonish_array(market.get("outcomes"))
    outcome_prices = parse_jsonish_array(market.get("outcomePrices") or market.get("outcome_prices"))
    enable_order_book = bool(market.get("enableOrderBook") or market.get("enable_order_book"))

    status = "discovered"
    notes = ""
    yes_token_id = ""
    no_token_id = ""

    if len(clob_token_ids) == 2:
        yes_token_id = str(clob_token_ids[0])
        no_token_id = str(clob_token_ids[1])
    else:
        status = "error"
        notes = f"Expected exactly two clobTokenIds, got {len(clob_token_ids)}."

    if not enable_order_book:
        status = "error"
        notes = "enableOrderBook is false." if not notes else f"{notes} enableOrderBook is false."

    event_slug = str(event.get("slug") or "").strip()

    return {
        "local_event_id": make_local_event_id(condition_id),
        "polymarket_event_id": event.get("id", ""),
        "polymarket_market_id": market.get("id", ""),
        "condition_id": condition_id,
        "event_slug": event_slug,
        "market_slug": market.get("slug", ""),
        "title": event.get("title", ""),
        "question": market.get("question", ""),
        "event_url": f"https://polymarket.com/event/{event_slug}" if event_slug else "",
        "start_time": market.get("startDate") or market.get("startTime") or event.get("startDate") or "",
        "end_time": market.get("endDate") or market.get("endTime") or event.get("endDate") or "",
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "outcomes": json.dumps(outcomes, ensure_ascii=False),
        "outcome_prices": json.dumps(outcome_prices, ensure_ascii=False),
        "active": market.get("active", event.get("active", "")),
        "closed": market.get("closed", event.get("closed", "")),
        "enable_order_book": enable_order_book,
        "created_at_polymarket": market.get("createdAt") or event.get("createdAt") or "",
        "discovered_at": discovered_at,
        "last_seen_at": discovered_at,
        "status": status,
        "notes": notes,
    }


def fetch_orderbook(token_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = requests.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=8)
        if response.status_code != 200:
            return None, f"orderbook HTTP {response.status_code}: {response.text[:200]}"
        payload = response.json()
        return payload if isinstance(payload, dict) else None, None
    except requests.RequestException as exc:
        return None, str(exc)


def fetch_trades(condition_id: str, limit: int = 100) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = requests.get(TRADES_URL, params={"market": condition_id, "limit": limit}, timeout=8)
        if response.status_code != 200:
            return [], f"trades HTTP {response.status_code}: {response.text[:200]}"
        payload = response.json()
        if isinstance(payload, list):
            return payload, None
        if isinstance(payload, dict):
            trades = payload.get("trades") or payload.get("data") or payload.get("results")
            if isinstance(trades, list):
                return trades, None
        return [], "unexpected trades response format"
    except requests.RequestException as exc:
        return [], str(exc)


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def best_bid(book: dict[str, Any] | None) -> float | None:
    prices = []
    for bid in (book or {}).get("bids") or []:
        price = to_float(bid.get("price"))
        if price is not None:
            prices.append(price)
    return max(prices) if prices else None


def best_ask(book: dict[str, Any] | None) -> float | None:
    prices = []
    for ask in (book or {}).get("asks") or []:
        price = to_float(ask.get("price"))
        if price is not None:
            prices.append(price)
    return min(prices) if prices else None


def spread(best_ask_value: float | None, best_bid_value: float | None) -> float | None:
    if best_ask_value is None or best_bid_value is None:
        return None
    return round(best_ask_value - best_bid_value, 6)


def midpoint(best_ask_value: float | None, best_bid_value: float | None) -> float | None:
    if best_ask_value is None or best_bid_value is None:
        return None
    return round((best_ask_value + best_bid_value) / 2, 6)


def orderbook_metrics(book: dict[str, Any] | None) -> dict[str, Any]:
    ask = best_ask(book)
    bid = best_bid(book)
    return {
        "best_ask": ask,
        "best_bid": bid,
        "midpoint": midpoint(ask, bid),
        "spread": spread(ask, bid),
        "last_trade_price": to_float((book or {}).get("last_trade_price")),
        "timestamp": (book or {}).get("timestamp"),
    }


def trade_value(trade: dict[str, Any], field_names: list[str]) -> Any:
    for field_name in field_names:
        if field_name in trade:
            return trade.get(field_name)
    return None


def trade_dedupe_key(trade: dict[str, Any]) -> str:
    values = [
        trade.get("transactionHash"),
        trade.get("asset"),
        trade.get("outcome"),
        trade.get("price"),
        trade.get("size"),
        trade.get("timestamp"),
    ]
    return "|".join(str(value or "") for value in values)


def calculate_trade_window(
    trades: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    seen_trade_keys: set[str],
) -> dict[str, Any]:
    result = {
        "up_trades_count_window": 0,
        "down_trades_count_window": 0,
        "up_volume_shares_window": 0.0,
        "down_volume_shares_window": 0.0,
        "up_volume_usdc_window": 0.0,
        "down_volume_usdc_window": 0.0,
    }

    for trade in trades:
        traded_at = parse_datetime(trade_value(trade, ["timestamp", "createdAt", "created_at", "time", "date"]))
        if not traded_at or not (window_start < traded_at <= window_end):
            continue

        dedupe_key = trade_dedupe_key(trade)
        if dedupe_key in seen_trade_keys:
            continue

        outcome = str(trade_value(trade, ["outcome", "asset", "outcomeName", "tokenOutcome"]) or "").strip().lower()
        size = to_float(trade_value(trade, ["size", "amount", "shares", "quantity"]))
        price = to_float(trade_value(trade, ["price", "avgPrice"]))
        if size is None or price is None:
            seen_trade_keys.add(dedupe_key)
            continue

        if outcome == "up":
            result["up_trades_count_window"] += 1
            result["up_volume_shares_window"] += size
            result["up_volume_usdc_window"] += size * price
        elif outcome == "down":
            result["down_trades_count_window"] += 1
            result["down_volume_shares_window"] += size
            result["down_volume_usdc_window"] += size * price
        else:
            seen_trade_keys.add(dedupe_key)
            continue

        seen_trade_keys.add(dedupe_key)

    result["up_volume_shares_window"] = round(result["up_volume_shares_window"], 6)
    result["down_volume_shares_window"] = round(result["down_volume_shares_window"], 6)
    result["up_volume_usdc_window"] = round(result["up_volume_usdc_window"], 6)
    result["down_volume_usdc_window"] = round(result["down_volume_usdc_window"], 6)
    result["total_trades_count_window"] = (
        result["up_trades_count_window"] + result["down_trades_count_window"]
    )
    result["total_volume_usdc_window"] = round(
        result["up_volume_usdc_window"] + result["down_volume_usdc_window"],
        6,
    )
    return result


def parse_jsonish_array(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def make_local_event_id(condition_id: str) -> str:
    suffix = condition_id[:12] if condition_id else "missing_id"
    return f"BTC_5M_{suffix}"
