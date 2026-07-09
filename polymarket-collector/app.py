import asyncio
import html
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pandas as pd
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "poly_data.sqlite3"

GAMMA_URL = "https://gamma-api.polymarket.com/events/slug/{slug}"
CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
TRADES_URL = "https://data-api.polymarket.com/trades?market={condition_id}&limit=100"
try:
    LOCAL_TIMEZONE = ZoneInfo("Asia/Jerusalem")
except ZoneInfoNotFoundError:
    LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="Asia/Jerusalem")

EVENT_CHECK_INTERVAL_SECONDS = 5
BOOK_CHECK_INTERVAL_SECONDS = 10

active_market: Optional[dict[str, Any]] = None
active_market_lock = asyncio.Lock()
last_trade_sample_at_by_condition_id: dict[str, datetime] = {}

app = FastAPI(title="Polymarket BTC Collector")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def format_local_datetime(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_iso_datetime(str(value))

    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(LOCAL_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")


def now_local_display() -> str:
    return format_local_datetime(now_utc()) or ""


def log_error(scope: str, error: Exception) -> None:
    print(f"[{scope}] error: {type(error).__name__}: {error}", flush=True)


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_trade_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return parse_trade_datetime(int(text))

    return parse_iso_datetime(text)


def market_has_ended(market: Optional[dict[str, Any]]) -> bool:
    if not market:
        return True

    end_dt = parse_iso_datetime(market.get("end_time"))
    return bool(end_dt and end_dt <= now_utc())


async def sleep_until_next_tick(interval_seconds: int, started_at: float) -> None:
    elapsed = asyncio.get_running_loop().time() - started_at
    delay = max(0.0, interval_seconds - elapsed)
    await asyncio.sleep(delay)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            local_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            polymarket_event_id TEXT,
            polymarket_market_id TEXT,
            condition_id TEXT,
            event_slug TEXT UNIQUE,
            market_slug TEXT,
            title TEXT,
            question TEXT,
            event_url TEXT,
            start_time TEXT,
            start_time_local TEXT,
            end_time TEXT,
            end_time_local TEXT,
            yes_token_id TEXT,
            no_token_id TEXT,
            outcomes TEXT,
            outcome_prices TEXT,
            active INTEGER,
            closed INTEGER,
            enable_order_book INTEGER,
            accepting_orders INTEGER,
            created_at_poly TEXT,
            created_at_poly_local TEXT,
            discovered_at TEXT,
            discovered_at_local TEXT,
            last_seen_at TEXT,
            last_seen_at_local TEXT,
            status TEXT,
            notes TEXT,
            raw_json TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at TEXT,
            sampled_at_local TEXT,
            event_slug TEXT,
            condition_id TEXT,
            up_token_id TEXT,
            down_token_id TEXT,
            up_best_ask REAL,
            up_best_bid REAL,
            down_best_ask REAL,
            down_best_bid REAL,
            up_last_trade_price REAL,
            down_last_trade_price REAL,
            up_spread REAL,
            down_spread REAL,
            up_midpoint REAL,
            down_midpoint REAL,
            raw_up_timestamp TEXT,
            raw_down_timestamp TEXT,
            up_volume_shares_10s REAL,
            down_volume_shares_10s REAL,
            up_volume_usdc_10s REAL,
            down_volume_usdc_10s REAL,
            trades_count_10s INTEGER,
            trades_window_start TEXT,
            trades_window_start_local TEXT,
            trades_window_end TEXT,
            trades_window_end_local TEXT,
            trades_error TEXT,
            status TEXT,
            error TEXT
        )
        """)

        ensure_column(conn, "events", "start_time_local", "TEXT")
        ensure_column(conn, "events", "end_time_local", "TEXT")
        ensure_column(conn, "events", "created_at_poly_local", "TEXT")
        ensure_column(conn, "events", "discovered_at_local", "TEXT")
        ensure_column(conn, "events", "last_seen_at_local", "TEXT")
        ensure_column(conn, "orderbook_log", "sampled_at_local", "TEXT")
        ensure_column(conn, "orderbook_log", "up_volume_shares_10s", "REAL")
        ensure_column(conn, "orderbook_log", "down_volume_shares_10s", "REAL")
        ensure_column(conn, "orderbook_log", "up_volume_usdc_10s", "REAL")
        ensure_column(conn, "orderbook_log", "down_volume_usdc_10s", "REAL")
        ensure_column(conn, "orderbook_log", "trades_count_10s", "INTEGER")
        ensure_column(conn, "orderbook_log", "trades_window_start", "TEXT")
        ensure_column(conn, "orderbook_log", "trades_window_start_local", "TEXT")
        ensure_column(conn, "orderbook_log", "trades_window_end", "TEXT")
        ensure_column(conn, "orderbook_log", "trades_window_end_local", "TEXT")
        ensure_column(conn, "orderbook_log", "trades_error", "TEXT")

        conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def floor_to_5m_epoch(dt: datetime) -> int:
    ts = int(dt.timestamp())
    return (ts // 300) * 300


def candidate_slugs() -> list[str]:
    """
    בודק כמה חלונות סביב הזמן הנוכחי.
    זה חשוב כי לפעמים market עתידי כבר פתוח להזמנות כמה דקות לפני תחילת החלון.
    """
    base = floor_to_5m_epoch(now_utc())
    candidates = [
        base - 300,
        base,
        base + 300,
        base + 600,
    ]
    return [f"btc-updown-5m-{ts}" for ts in candidates]


def safe_json_loads(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, list) else []
        except Exception:
            return []
    return []


def is_market_open(event_data: dict[str, Any], market: dict[str, Any]) -> bool:
    end_dt = parse_iso_datetime(market.get("endDate") or event_data.get("endDate"))
    if end_dt and end_dt <= now_utc():
        return False

    return (
        event_data.get("active") is True
        and event_data.get("closed") is False
        and market.get("active") is True
        and market.get("closed") is False
        and market.get("acceptingOrders") is True
        and market.get("enableOrderBook") is True
    )


def extract_market(event_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    markets = event_data.get("markets") or []
    if not markets:
        return None

    market = markets[0]

    outcomes = safe_json_loads(market.get("outcomes"))
    token_ids = safe_json_loads(market.get("clobTokenIds"))

    if len(outcomes) < 2 or len(token_ids) < 2:
        return None

    outcome_to_token = {
        str(outcome).strip().lower(): str(token_id)
        for outcome, token_id in zip(outcomes, token_ids)
    }

    up_token_id = outcome_to_token.get("up") or outcome_to_token.get("yes") or str(token_ids[0])
    down_token_id = outcome_to_token.get("down") or outcome_to_token.get("no") or str(token_ids[1])

    event_slug = event_data.get("slug")
    market_slug = market.get("slug")
    start_time = event_data.get("startTime") or market.get("eventStartTime") or market.get("startDate")
    end_time = event_data.get("endDate") or market.get("endDate")
    created_at_poly = market.get("createdAt") or event_data.get("createdAt")

    return {
        "polymarket_event_id": str(event_data.get("id", "")),
        "polymarket_market_id": str(market.get("id", "")),
        "condition_id": market.get("conditionId"),
        "event_slug": event_slug,
        "market_slug": market_slug,
        "title": event_data.get("title"),
        "question": market.get("question"),
        "event_url": f"https://polymarket.com/event/{event_slug}",
        "start_time": start_time,
        "start_time_local": format_local_datetime(start_time),
        "end_time": end_time,
        "end_time_local": format_local_datetime(end_time),
        "yes_token_id": up_token_id,
        "no_token_id": down_token_id,
        "outcomes": json.dumps(outcomes, ensure_ascii=False),
        "outcome_prices": market.get("outcomePrices"),
        "active": 1 if market.get("active") is True else 0,
        "closed": 1 if market.get("closed") is True else 0,
        "enable_order_book": 1 if market.get("enableOrderBook") is True else 0,
        "accepting_orders": 1 if market.get("acceptingOrders") is True else 0,
        "created_at_poly": created_at_poly,
        "created_at_poly_local": format_local_datetime(created_at_poly),
        "status": "open" if is_market_open(event_data, market) else "closed",
        "notes": "",
        "raw_json": json.dumps(event_data, ensure_ascii=False),
        "_is_open": is_market_open(event_data, market),
    }


def upsert_event(market_row: dict[str, Any]) -> None:
    discovered_at = now_iso()
    discovered_at_local = now_local_display()
    last_seen_at = now_iso()
    last_seen_at_local = now_local_display()

    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT local_event_id, discovered_at FROM events WHERE event_slug = ?",
            (market_row["event_slug"],),
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE events SET
                    polymarket_event_id = ?,
                    polymarket_market_id = ?,
                    condition_id = ?,
                    market_slug = ?,
                    title = ?,
                    question = ?,
                    event_url = ?,
                    start_time = ?,
                    start_time_local = ?,
                    end_time = ?,
                    end_time_local = ?,
                    yes_token_id = ?,
                    no_token_id = ?,
                    outcomes = ?,
                    outcome_prices = ?,
                    active = ?,
                    closed = ?,
                    enable_order_book = ?,
                    accepting_orders = ?,
                    created_at_poly = ?,
                    created_at_poly_local = ?,
                    last_seen_at = ?,
                    last_seen_at_local = ?,
                    status = ?,
                    notes = ?,
                    raw_json = ?
                WHERE event_slug = ?
            """, (
                market_row["polymarket_event_id"],
                market_row["polymarket_market_id"],
                market_row["condition_id"],
                market_row["market_slug"],
                market_row["title"],
                market_row["question"],
                market_row["event_url"],
                market_row["start_time"],
                market_row["start_time_local"],
                market_row["end_time"],
                market_row["end_time_local"],
                market_row["yes_token_id"],
                market_row["no_token_id"],
                market_row["outcomes"],
                market_row["outcome_prices"],
                market_row["active"],
                market_row["closed"],
                market_row["enable_order_book"],
                market_row["accepting_orders"],
                market_row["created_at_poly"],
                market_row["created_at_poly_local"],
                last_seen_at,
                last_seen_at_local,
                market_row["status"],
                market_row["notes"],
                market_row["raw_json"],
                market_row["event_slug"],
            ))
        else:
            conn.execute("""
                INSERT INTO events (
                    polymarket_event_id,
                    polymarket_market_id,
                    condition_id,
                    event_slug,
                    market_slug,
                    title,
                    question,
                    event_url,
                    start_time,
                    start_time_local,
                    end_time,
                    end_time_local,
                    yes_token_id,
                    no_token_id,
                    outcomes,
                    outcome_prices,
                    active,
                    closed,
                    enable_order_book,
                    accepting_orders,
                    created_at_poly,
                    created_at_poly_local,
                    discovered_at,
                    discovered_at_local,
                    last_seen_at,
                    last_seen_at_local,
                    status,
                    notes,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_row["polymarket_event_id"],
                market_row["polymarket_market_id"],
                market_row["condition_id"],
                market_row["event_slug"],
                market_row["market_slug"],
                market_row["title"],
                market_row["question"],
                market_row["event_url"],
                market_row["start_time"],
                market_row["start_time_local"],
                market_row["end_time"],
                market_row["end_time_local"],
                market_row["yes_token_id"],
                market_row["no_token_id"],
                market_row["outcomes"],
                market_row["outcome_prices"],
                market_row["active"],
                market_row["closed"],
                market_row["enable_order_book"],
                market_row["accepting_orders"],
                market_row["created_at_poly"],
                market_row["created_at_poly_local"],
                discovered_at,
                discovered_at_local,
                last_seen_at,
                last_seen_at_local,
                market_row["status"],
                market_row["notes"],
                market_row["raw_json"],
            ))

        conn.commit()


async def discover_open_market() -> Optional[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        found_open: list[dict[str, Any]] = []

        for slug in candidate_slugs():
            try:
                response = await client.get(GAMMA_URL.format(slug=slug))
                if response.status_code != 200:
                    continue

                event_data = response.json()
                market_row = extract_market(event_data)

                if not market_row:
                    continue

                upsert_event(market_row)

                if market_row["_is_open"]:
                    found_open.append(market_row)

            except Exception as e:
                log_error(f"discover slug={slug}", e)
                continue

        if not found_open:
            return None

        def sort_key(row: dict[str, Any]) -> str:
            return row.get("start_time") or row.get("end_time") or ""

        found_open.sort(key=sort_key)
        return found_open[0]


def best_bid(book: dict[str, Any]) -> Optional[float]:
    bids = book.get("bids") or []
    prices = []
    for item in bids:
        try:
            prices.append(float(item["price"]))
        except Exception:
            pass
    return max(prices) if prices else None


def best_ask(book: dict[str, Any]) -> Optional[float]:
    asks = book.get("asks") or []
    prices = []
    for item in asks:
        try:
            prices.append(float(item["price"]))
        except Exception:
            pass
    return min(prices) if prices else None


def to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def calc_spread(best_ask_value: Optional[float], best_bid_value: Optional[float]) -> Optional[float]:
    if best_ask_value is None or best_bid_value is None:
        return None
    return round(best_ask_value - best_bid_value, 6)


def calc_midpoint(best_ask_value: Optional[float], best_bid_value: Optional[float]) -> Optional[float]:
    if best_ask_value is None or best_bid_value is None:
        return None
    return round((best_ask_value + best_bid_value) / 2, 6)


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        response = await client.get(CLOB_BOOK_URL.format(token_id=token_id))
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}: {response.text[:300]}"
        return response.json(), None
    except Exception as e:
        return None, str(e)


async def fetch_trades(client: httpx.AsyncClient, condition_id: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        response = await client.get(TRADES_URL.format(condition_id=condition_id))
        if response.status_code != 200:
            return [], f"HTTP {response.status_code}: {response.text[:300]}"

        payload = response.json()
        if isinstance(payload, list):
            return payload, None
        if isinstance(payload, dict):
            trades = payload.get("trades") or payload.get("data") or payload.get("results")
            if isinstance(trades, list):
                return trades, None

        return [], "Unexpected trades response format"
    except Exception as e:
        return [], str(e)


def trade_value(trade: dict[str, Any], field_names: list[str]) -> Any:
    for field_name in field_names:
        if field_name in trade:
            return trade.get(field_name)
    return None


def trade_outcome(trade: dict[str, Any]) -> str:
    value = trade_value(trade, ["outcome", "asset", "outcomeName", "tokenOutcome", "side"])
    return str(value or "").strip().lower()


def calculate_trade_volume(
    trades: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    result = {
        "up_volume_shares_10s": 0.0,
        "down_volume_shares_10s": 0.0,
        "up_volume_usdc_10s": 0.0,
        "down_volume_usdc_10s": 0.0,
        "trades_count_10s": 0,
    }

    for trade in trades:
        traded_at = parse_trade_datetime(
            trade_value(trade, ["timestamp", "createdAt", "created_at", "time", "date"])
        )
        if not traded_at or not (window_start < traded_at <= window_end):
            continue

        outcome = trade_outcome(trade)
        size = to_float(trade_value(trade, ["size", "amount", "shares", "quantity"]))
        price = to_float(trade_value(trade, ["price", "avgPrice"]))
        if size is None or price is None:
            continue

        if outcome == "up":
            result["up_volume_shares_10s"] += size
            result["up_volume_usdc_10s"] += size * price
        elif outcome == "down":
            result["down_volume_shares_10s"] += size
            result["down_volume_usdc_10s"] += size * price
        else:
            continue

        result["trades_count_10s"] += 1

    for key in [
        "up_volume_shares_10s",
        "down_volume_shares_10s",
        "up_volume_usdc_10s",
        "down_volume_usdc_10s",
    ]:
        result[key] = round(result[key], 6)

    return result


def trade_sample_window(condition_id: str, window_end: datetime) -> tuple[datetime, datetime]:
    window_start = last_trade_sample_at_by_condition_id.get(condition_id)
    if not window_start:
        window_start = window_end - timedelta(seconds=BOOK_CHECK_INTERVAL_SECONDS)
    return window_start, window_end


def insert_orderbook_log(row: dict[str, Any]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO orderbook_log (
                sampled_at,
                sampled_at_local,
                event_slug,
                condition_id,
                up_token_id,
                down_token_id,
                up_best_ask,
                up_best_bid,
                down_best_ask,
                down_best_bid,
                up_last_trade_price,
                down_last_trade_price,
                up_spread,
                down_spread,
                up_midpoint,
                down_midpoint,
                raw_up_timestamp,
                raw_down_timestamp,
                up_volume_shares_10s,
                down_volume_shares_10s,
                up_volume_usdc_10s,
                down_volume_usdc_10s,
                trades_count_10s,
                trades_window_start,
                trades_window_start_local,
                trades_window_end,
                trades_window_end_local,
                trades_error,
                status,
                error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.get("sampled_at"),
            row.get("sampled_at_local"),
            row.get("event_slug"),
            row.get("condition_id"),
            row.get("up_token_id"),
            row.get("down_token_id"),
            row.get("up_best_ask"),
            row.get("up_best_bid"),
            row.get("down_best_ask"),
            row.get("down_best_bid"),
            row.get("up_last_trade_price"),
            row.get("down_last_trade_price"),
            row.get("up_spread"),
            row.get("down_spread"),
            row.get("up_midpoint"),
            row.get("down_midpoint"),
            row.get("raw_up_timestamp"),
            row.get("raw_down_timestamp"),
            row.get("up_volume_shares_10s"),
            row.get("down_volume_shares_10s"),
            row.get("up_volume_usdc_10s"),
            row.get("down_volume_usdc_10s"),
            row.get("trades_count_10s"),
            row.get("trades_window_start"),
            row.get("trades_window_start_local"),
            row.get("trades_window_end"),
            row.get("trades_window_end_local"),
            row.get("trades_error"),
            row.get("status"),
            row.get("error"),
        ))
        conn.commit()


async def event_collector_loop() -> None:
    global active_market

    while True:
        tick_started_at = asyncio.get_running_loop().time()

        try:
            market = await discover_open_market()

            async with active_market_lock:
                if market:
                    active_market = market
                    print(f"[event] active market: {market['event_slug']}", flush=True)
                else:
                    if market_has_ended(active_market):
                        active_market = None
                    print("[event] no open market found", flush=True)
        except Exception as e:
            log_error("event loop", e)

        finally:
            await sleep_until_next_tick(EVENT_CHECK_INTERVAL_SECONDS, tick_started_at)


async def orderbook_collector_loop() -> None:
    global active_market

    while True:
        tick_started_at = asyncio.get_running_loop().time()

        try:
            async with active_market_lock:
                market = dict(active_market) if active_market else None

            if not market:
                continue

            if market_has_ended(market):
                async with active_market_lock:
                    active_market = None
                print(f"[book] market ended: {market.get('event_slug')}", flush=True)
                continue

            up_token_id = market["yes_token_id"]
            down_token_id = market["no_token_id"]
            condition_id = market.get("condition_id")
            trades_window_end_dt = now_utc()
            trades_window_start_dt, trades_window_end_dt = trade_sample_window(condition_id, trades_window_end_dt)

            async with httpx.AsyncClient(timeout=10) as client:
                up_book, up_error = await fetch_book(client, up_token_id)
                down_book, down_error = await fetch_book(client, down_token_id)
                trades, trades_error = await fetch_trades(client, condition_id)

            errors = []
            if up_error:
                errors.append(f"up_error={up_error}")
            if down_error:
                errors.append(f"down_error={down_error}")
            if trades_error:
                errors.append(f"trades_error={trades_error}")

            up_best_bid = best_bid(up_book) if up_book else None
            up_best_ask = best_ask(up_book) if up_book else None
            down_best_bid = best_bid(down_book) if down_book else None
            down_best_ask = best_ask(down_book) if down_book else None
            trade_volume = calculate_trade_volume(trades, trades_window_start_dt, trades_window_end_dt)

            if not errors:
                status = "success"
            elif up_book or down_book:
                status = "partial_error"
            else:
                status = "error"

            row = {
                "sampled_at": now_iso(),
                "sampled_at_local": now_local_display(),
                "event_slug": market.get("event_slug"),
                "condition_id": condition_id,
                "up_token_id": up_token_id,
                "down_token_id": down_token_id,
                "up_best_ask": up_best_ask,
                "up_best_bid": up_best_bid,
                "down_best_ask": down_best_ask,
                "down_best_bid": down_best_bid,
                "up_last_trade_price": to_float(up_book.get("last_trade_price")) if up_book else None,
                "down_last_trade_price": to_float(down_book.get("last_trade_price")) if down_book else None,
                "up_spread": calc_spread(up_best_ask, up_best_bid),
                "down_spread": calc_spread(down_best_ask, down_best_bid),
                "up_midpoint": calc_midpoint(up_best_ask, up_best_bid),
                "down_midpoint": calc_midpoint(down_best_ask, down_best_bid),
                "raw_up_timestamp": str(up_book.get("timestamp")) if up_book else None,
                "raw_down_timestamp": str(down_book.get("timestamp")) if down_book else None,
                "up_volume_shares_10s": trade_volume["up_volume_shares_10s"],
                "down_volume_shares_10s": trade_volume["down_volume_shares_10s"],
                "up_volume_usdc_10s": trade_volume["up_volume_usdc_10s"],
                "down_volume_usdc_10s": trade_volume["down_volume_usdc_10s"],
                "trades_count_10s": trade_volume["trades_count_10s"],
                "trades_window_start": trades_window_start_dt.isoformat(),
                "trades_window_start_local": format_local_datetime(trades_window_start_dt),
                "trades_window_end": trades_window_end_dt.isoformat(),
                "trades_window_end_local": format_local_datetime(trades_window_end_dt),
                "trades_error": trades_error,
                "status": status,
                "error": " | ".join(errors) if errors else None,
            }

            insert_orderbook_log(row)
            if not trades_error:
                last_trade_sample_at_by_condition_id[condition_id] = trades_window_end_dt

            print(
                f"[book] {row['event_slug']} status={status} "
                f"up={up_best_bid}/{up_best_ask} down={down_best_bid}/{down_best_ask} "
                f"vol_up={row['up_volume_shares_10s']} vol_down={row['down_volume_shares_10s']}",
                flush=True,
            )
        except Exception as e:
            log_error("book loop", e)

        finally:
            await sleep_until_next_tick(BOOK_CHECK_INTERVAL_SECONDS, tick_started_at)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(event_collector_loop())
    asyncio.create_task(orderbook_collector_loop())


def render_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "<p>No data yet.</p>"

    headers = rows[0].keys()

    thead = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)

    body = ""
    for row in rows:
        cells = ""
        for h in headers:
            value = row[h]
            text = "" if value is None else str(value)
            cells += f"<td>{html.escape(text)}</td>"
        body += f"<tr>{cells}</tr>"

    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr>{thead}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    with get_conn() as conn:
        events = conn.execute("""
            SELECT
                local_event_id,
                polymarket_event_id,
                polymarket_market_id,
                condition_id,
                event_slug,
                market_slug,
                title,
                question,
                event_url,
                start_time,
                start_time_local,
                end_time,
                end_time_local,
                yes_token_id,
                no_token_id,
                outcomes,
                outcome_prices,
                active,
                closed,
                enable_order_book,
                accepting_orders,
                created_at_poly,
                created_at_poly_local,
                discovered_at,
                discovered_at_local,
                last_seen_at,
                last_seen_at_local,
                status,
                notes
            FROM events
            ORDER BY local_event_id DESC
            LIMIT 50
        """).fetchall()

        logs = conn.execute("""
            SELECT
                sampled_at,
                sampled_at_local,
                event_slug,
                condition_id,
                up_token_id,
                down_token_id,
                up_best_ask,
                up_best_bid,
                down_best_ask,
                down_best_bid,
                up_last_trade_price,
                down_last_trade_price,
                up_spread,
                down_spread,
                up_midpoint,
                down_midpoint,
                raw_up_timestamp,
                raw_down_timestamp,
                up_volume_shares_10s,
                down_volume_shares_10s,
                up_volume_usdc_10s,
                down_volume_usdc_10s,
                trades_count_10s,
                trades_window_start,
                trades_window_start_local,
                trades_window_end,
                trades_window_end_local,
                trades_error,
                status,
                error
            FROM orderbook_log
            ORDER BY id DESC
            LIMIT 300
        """).fetchall()

    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="10">
        <title>Polymarket BTC Collector</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 24px;
                background: #f7f7f7;
                color: #111;
            }}
            h1, h2 {{
                margin-bottom: 8px;
            }}
            .card {{
                background: white;
                border: 1px solid #ddd;
                border-radius: 10px;
                padding: 16px;
                margin-bottom: 24px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            }}
            .actions {{
                margin: 12px 0 20px 0;
            }}
            a.button {{
                display: inline-block;
                padding: 10px 14px;
                background: #111;
                color: white;
                text-decoration: none;
                border-radius: 6px;
            }}
            .table-wrap {{
                overflow-x: auto;
                max-height: 520px;
                border: 1px solid #ddd;
            }}
            table {{
                border-collapse: collapse;
                width: 100%;
                min-width: 1600px;
                font-size: 13px;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 7px;
                white-space: nowrap;
                vertical-align: top;
            }}
            th {{
                background: #f0f0f0;
                position: sticky;
                top: 0;
                z-index: 1;
            }}
            .muted {{
                color: #666;
                font-size: 13px;
            }}
        </style>
    </head>
    <body>
        <h1>Polymarket BTC Collector</h1>
        <div class="muted">Auto refresh every 10 seconds. Server time: {html.escape(now_iso())}</div>

        <div class="actions">
            <a class="button" href="/download.xlsx">Download Excel</a>
        </div>

        <div class="card">
            <h2>Events / Markets</h2>
            {render_table(events)}
        </div>

        <div class="card">
            <h2>Orderbook Log</h2>
            {render_table(logs)}
        </div>
    </body>
    </html>
    """


@app.get("/download.xlsx")
def download_xlsx() -> StreamingResponse:
    with sqlite3.connect(DB_PATH) as conn:
        events_df = pd.read_sql_query("""
            SELECT
                local_event_id,
                polymarket_event_id,
                polymarket_market_id,
                condition_id,
                event_slug,
                market_slug,
                title,
                question,
                event_url,
                start_time,
                start_time_local,
                end_time,
                end_time_local,
                yes_token_id,
                no_token_id,
                outcomes,
                outcome_prices,
                active,
                closed,
                enable_order_book,
                accepting_orders,
                created_at_poly,
                created_at_poly_local,
                discovered_at,
                discovered_at_local,
                last_seen_at,
                last_seen_at_local,
                status,
                notes
            FROM events
            ORDER BY local_event_id ASC
        """, conn)

        logs_df = pd.read_sql_query("""
            SELECT
                sampled_at,
                sampled_at_local,
                event_slug,
                condition_id,
                up_token_id,
                down_token_id,
                up_best_ask,
                up_best_bid,
                down_best_ask,
                down_best_bid,
                up_last_trade_price,
                down_last_trade_price,
                up_spread,
                down_spread,
                up_midpoint,
                down_midpoint,
                raw_up_timestamp,
                raw_down_timestamp,
                up_volume_shares_10s,
                down_volume_shares_10s,
                up_volume_usdc_10s,
                down_volume_usdc_10s,
                trades_count_10s,
                trades_window_start,
                trades_window_start_local,
                trades_window_end,
                trades_window_end_local,
                trades_error,
                status,
                error
            FROM orderbook_log
            ORDER BY id ASC
        """, conn)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        events_df.to_excel(writer, sheet_name="events", index=False)
        logs_df.to_excel(writer, sheet_name="orderbook_log", index=False)

    output.seek(0)

    filename = f"polymarket_data_{now_utc().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": now_iso(),
        "db": str(DB_PATH),
        "active_market": active_market.get("event_slug") if active_market else None,
    }
