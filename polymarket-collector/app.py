import asyncio
import html
import json
import os
import shutil
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from openpyxl import Workbook

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "poly_data.sqlite3"
EXPORT_DIR = APP_DIR / "output"
EXPORT_FILE_PREFIX = "polymarket_data_"
EXPORT_FILE_SUFFIX = ".xlsx"


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError:
        return default


GAMMA_URL = "https://gamma-api.polymarket.com/events/slug/{slug}"
CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
COINBASE_CANDLES_URL = os.getenv(
    "COINBASE_CANDLES_URL",
    "https://api.exchange.coinbase.com/products/{product_id}/candles",
).strip()
COINBASE_PRODUCT_ID = os.getenv("COINBASE_PRODUCT_ID", "BTC-USD").strip() or "BTC-USD"
COINBASE_CANDLE_GRANULARITY_SECONDS = env_int("COINBASE_CANDLE_GRANULARITY_SECONDS", 300)
COINBASE_VOLUME_POLL_INTERVAL_SECONDS = env_int("COINBASE_VOLUME_POLL_INTERVAL_SECONDS", 30)
COINBASE_REQUEST_TIMEOUT_SECONDS = env_int("COINBASE_REQUEST_TIMEOUT_SECONDS", 10)
COINBASE_MAX_DELTA_GAP_SECONDS = env_int("COINBASE_MAX_DELTA_GAP_SECONDS", 90)
COINBASE_MISSING_CANDLE_RETRY_COUNT = env_int("COINBASE_MISSING_CANDLE_RETRY_COUNT", 2)
COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS = env_int("COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS", 2)

try:
    LOCAL_TIMEZONE = ZoneInfo("Asia/Jerusalem")
except ZoneInfoNotFoundError:
    LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="Asia/Jerusalem")

EVENT_CHECK_INTERVAL_SECONDS = 5
BOOK_CHECK_INTERVAL_SECONDS = 2

active_market: Optional[dict[str, Any]] = None
active_market_lock = asyncio.Lock()
coinbase_volume_state: dict[str, Optional[str]] = {
    "last_sample_at": None,
    "last_success_at": None,
    "status": "starting",
    "last_error": None,
}
export_state_lock = threading.Lock()
export_state: dict[str, Optional[str]] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "filename": None,
    "path": None,
    "error": None,
    "row_counts": None,
}

EXPORT_SHEETS: list[tuple[str, list[str], str]] = [
    (
        "events",
        [
            "local_event_id",
            "polymarket_event_id",
            "polymarket_market_id",
            "condition_id",
            "event_slug",
            "market_slug",
            "title",
            "question",
            "event_url",
            "start_time",
            "start_time_local",
            "end_time",
            "end_time_local",
            "yes_token_id",
            "no_token_id",
            "outcomes",
            "outcome_prices",
            "active",
            "closed",
            "enable_order_book",
            "accepting_orders",
            "created_at_poly",
            "created_at_poly_local",
            "discovered_at",
            "discovered_at_local",
            "last_seen_at",
            "last_seen_at_local",
            "status",
            "notes",
        ],
        """
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
        """,
    ),
    (
        "orderbook_log",
        [
            "sampled_at",
            "sampled_at_local",
            "event_slug",
            "condition_id",
            "up_token_id",
            "down_token_id",
            "up_best_ask",
            "up_best_bid",
            "down_best_ask",
            "down_best_bid",
            "up_last_trade_price",
            "down_last_trade_price",
            "up_spread",
            "down_spread",
            "up_midpoint",
            "down_midpoint",
            "raw_up_timestamp",
            "raw_down_timestamp",
            "up_volume_shares_10s",
            "down_volume_shares_10s",
            "up_volume_usdc_10s",
            "down_volume_usdc_10s",
            "trades_count_10s",
            "trades_window_start",
            "trades_window_start_local",
            "trades_window_end",
            "trades_window_end_local",
            "trades_error",
            "status",
            "error",
        ],
        """
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
        """,
    ),
    (
        "btc_volume_log",
        [
            "id",
            "sampled_at",
            "sample_bucket_at",
            "candle_start_at",
            "product_id",
            "granularity_seconds",
            "volume_btc_cumulative",
            "volume_btc_delta",
            "seconds_since_previous_sample",
            "event_slug",
            "condition_id",
            "source",
            "status",
            "error",
        ],
        """
        SELECT
            id,
            sampled_at,
            sample_bucket_at,
            candle_start_at,
            product_id,
            granularity_seconds,
            volume_btc_cumulative,
            volume_btc_delta,
            seconds_since_previous_sample,
            event_slug,
            condition_id,
            source,
            status,
            error
        FROM btc_volume_log
        ORDER BY sampled_at ASC
        """,
    ),
    (
        "rules",
        [
            "id",
            "name",
            "created_at",
            "updated_at",
            "entry_price",
            "stop_loss_price",
            "take_profit_price",
            "max_yes_entries_per_event",
            "max_no_entries_per_event",
            "status",
            "eligible_after_event_id",
        ],
        """
        SELECT
            id,
            name,
            created_at,
            updated_at,
            entry_price,
            stop_loss_price,
            take_profit_price,
            max_yes_entries_per_event,
            max_no_entries_per_event,
            status,
            eligible_after_event_id
        FROM rules
        ORDER BY id ASC
        """,
    ),
    (
        "deals",
        [
            "id",
            "rule_id",
            "rule_name",
            "event_id",
            "side",
            "result",
            "entry_at",
            "entry_price",
            "entry_orderbook_log_id",
            "exit_at",
            "exit_price",
            "exit_orderbook_log_id",
            "exit_reason",
            "market_result",
            "price_change_points",
            "return_percent",
            "created_at",
            "updated_at",
        ],
        """
        SELECT
            id,
            rule_id,
            rule_name,
            event_id,
            side,
            result,
            entry_at,
            entry_price,
            entry_orderbook_log_id,
            exit_at,
            exit_price,
            exit_orderbook_log_id,
            exit_reason,
            market_result,
            price_change_points,
            return_percent,
            created_at,
            updated_at
        FROM deals
        ORDER BY id ASC
        """,
    ),
]

app = FastAPI(title="Polymarket BTC Collector")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def connect_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, factory=ClosingConnection)


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
    with connect_db() as conn:
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

        conn.execute("""
        CREATE TABLE IF NOT EXISTS btc_volume_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at TEXT NOT NULL,
            sample_bucket_at TEXT NOT NULL,
            candle_start_at TEXT NOT NULL,
            product_id TEXT NOT NULL,
            granularity_seconds INTEGER NOT NULL,
            volume_btc_cumulative REAL,
            volume_btc_delta REAL,
            seconds_since_previous_sample REAL,
            event_slug TEXT,
            condition_id TEXT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss_price REAL NOT NULL,
            take_profit_price REAL NOT NULL,
            max_yes_entries_per_event INTEGER NOT NULL,
            max_no_entries_per_event INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
            eligible_after_event_id TEXT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            rule_name TEXT,
            event_id TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('yes', 'no')),
            result TEXT NOT NULL CHECK (result IN ('open', 'win', 'loss')),
            entry_at TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_orderbook_log_id INTEGER NOT NULL,
            exit_at TEXT,
            exit_price REAL,
            exit_orderbook_log_id INTEGER,
            exit_reason TEXT CHECK (exit_reason IS NULL OR exit_reason IN ('take_profit', 'stop_loss', 'event_resolution')),
            market_result TEXT,
            price_change_points REAL,
            return_percent REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES rules(id),
            FOREIGN KEY (entry_orderbook_log_id) REFERENCES orderbook_log(id),
            FOREIGN KEY (exit_orderbook_log_id) REFERENCES orderbook_log(id)
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
        ensure_column(conn, "btc_volume_log", "sampled_at", "TEXT")
        ensure_column(conn, "btc_volume_log", "sample_bucket_at", "TEXT")
        ensure_column(conn, "btc_volume_log", "candle_start_at", "TEXT")
        ensure_column(conn, "btc_volume_log", "product_id", "TEXT")
        ensure_column(conn, "btc_volume_log", "granularity_seconds", "INTEGER")
        ensure_column(conn, "btc_volume_log", "volume_btc_cumulative", "REAL")
        ensure_column(conn, "btc_volume_log", "volume_btc_delta", "REAL")
        ensure_column(conn, "btc_volume_log", "seconds_since_previous_sample", "REAL")
        ensure_column(conn, "btc_volume_log", "event_slug", "TEXT")
        ensure_column(conn, "btc_volume_log", "condition_id", "TEXT")
        ensure_column(conn, "btc_volume_log", "source", "TEXT")
        ensure_column(conn, "btc_volume_log", "status", "TEXT")
        ensure_column(conn, "btc_volume_log", "error", "TEXT")
        ensure_column(conn, "rules", "eligible_after_event_id", "TEXT")
        ensure_column(conn, "deals", "rule_name", "TEXT")

        conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_btc_volume_log_unique_bucket
        ON btc_volume_log (product_id, candle_start_at, sample_bucket_at)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_btc_volume_log_sampled_at
        ON btc_volume_log (sampled_at)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_btc_volume_log_candle_start_at
        ON btc_volume_log (candle_start_at)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_btc_volume_log_event_slug
        ON btc_volume_log (event_slug)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rules_status
        ON rules (status)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deals_rule_id
        ON deals (rule_id)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deals_event_id
        ON deals (event_id)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deals_result
        ON deals (result)
        """)
        conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_one_open_per_rule
        ON deals (rule_id)
        WHERE result = 'open'
        """)
        conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_unique_entry_sample
        ON deals (rule_id, event_id, side, entry_orderbook_log_id)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_deals_rule_event_side
        ON deals (rule_id, event_id, side)
        """)

        conn.commit()
    conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def get_conn() -> sqlite3.Connection:
    conn = connect_db()
    conn.row_factory = sqlite3.Row
    return conn


def decimal_price(value: Any, field_name: str) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} must be a numeric price")

    if not price.is_finite():
        raise ValueError(f"{field_name} must be a finite numeric price")
    if price <= Decimal("0"):
        raise ValueError(f"{field_name} must be positive")
    if price >= Decimal("1"):
        raise ValueError(f"{field_name} must be below 1")
    return price.quantize(Decimal("0.000001"))


def decimal_from_db(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def prices_equal(left: Any, right: Any) -> bool:
    left_price = decimal_from_db(left)
    right_price = decimal_from_db(right)
    return left_price is not None and right_price is not None and left_price == right_price


def validate_rule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("name must not be empty")

    entry_price = decimal_price(payload.get("entry_price"), "entry_price")
    stop_loss_price = decimal_price(payload.get("stop_loss_price"), "stop_loss_price")
    take_profit_price = decimal_price(payload.get("take_profit_price"), "take_profit_price")

    if entry_price == Decimal("0.500000"):
        raise ValueError("entry_price must not be 0.5")
    if stop_loss_price >= entry_price:
        raise ValueError("stop_loss_price must be below entry_price")
    if take_profit_price <= entry_price:
        raise ValueError("take_profit_price must be above entry_price")

    try:
        max_yes = int(payload.get("max_yes_entries_per_event"))
        max_no = int(payload.get("max_no_entries_per_event"))
    except (TypeError, ValueError):
        raise ValueError("entry limits must be non-negative integers")

    if max_yes < 0:
        raise ValueError("max_yes_entries_per_event must be non-negative")
    if max_no < 0:
        raise ValueError("max_no_entries_per_event must be non-negative")

    status = str(payload.get("status", "active")).strip().lower()
    if status not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")

    return {
        "name": name,
        "entry_price": float(entry_price),
        "stop_loss_price": float(stop_loss_price),
        "take_profit_price": float(take_profit_price),
        "max_yes_entries_per_event": max_yes,
        "max_no_entries_per_event": max_no,
        "status": status,
    }


def active_event_id_for_rule_gate() -> Optional[str]:
    event_slug, _ = current_active_market_snapshot()
    return event_slug


def create_rule(payload: dict[str, Any]) -> sqlite3.Row:
    values = validate_rule_payload(payload)
    created_at = now_iso()
    eligible_after_event_id = active_event_id_for_rule_gate() if values["status"] == "active" else None

    try:
        with get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO rules (
                    name,
                    created_at,
                    updated_at,
                    entry_price,
                    stop_loss_price,
                    take_profit_price,
                    max_yes_entries_per_event,
                    max_no_entries_per_event,
                    status,
                    eligible_after_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                values["name"],
                created_at,
                created_at,
                values["entry_price"],
                values["stop_loss_price"],
                values["take_profit_price"],
                values["max_yes_entries_per_event"],
                values["max_no_entries_per_event"],
                values["status"],
                eligible_after_event_id,
            ))
            rule_id = cursor.lastrowid
            conn.commit()
            row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    except sqlite3.Error as exc:
        log_error("create rule db", exc)
        raise

    print(
        f"[rules] created id={rule_id} name={values['name']} status={values['status']} "
        f"eligible_after_event_id={eligible_after_event_id}",
        flush=True,
    )
    if eligible_after_event_id:
        print(f"[rules] rule id={rule_id} waits for next event after {eligible_after_event_id}", flush=True)
    return row


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def deactivate_rule(rule_id: int) -> tuple[sqlite3.Row, str]:
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
            if not row:
                raise KeyError(f"Rule {rule_id} does not exist")
            if row["status"] == "inactive":
                print(f"[rules] deactivate requested for already inactive id={rule_id}", flush=True)
                return row, "Rule is already inactive"

            updated_at = now_iso()
            conn.execute(
                "UPDATE rules SET status = 'inactive', updated_at = ? WHERE id = ?",
                (updated_at, rule_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    except sqlite3.Error as exc:
        log_error("deactivate rule db", exc)
        raise

    print(f"[rules] deactivated id={rule_id}", flush=True)
    return updated, "Rule deactivated"


def calculate_deal_metrics(entry_price: Any, exit_price: Any) -> tuple[float, float]:
    entry = Decimal(str(entry_price))
    exit_value = Decimal(str(exit_price))
    points = abs(exit_value - entry) * Decimal("100")
    return_percent = ((exit_value - entry) / entry) * Decimal("100")
    return float(points), float(return_percent)


def close_deal(
    conn: sqlite3.Connection,
    deal: sqlite3.Row,
    result: str,
    exit_reason: str,
    exit_price: Any,
    exit_at: str,
    exit_orderbook_log_id: Optional[int],
    market_result: Optional[str] = None,
) -> None:
    price_change_points, return_percent = calculate_deal_metrics(deal["entry_price"], exit_price)
    conn.execute("""
        UPDATE deals SET
            result = ?,
            exit_at = ?,
            exit_price = ?,
            exit_orderbook_log_id = ?,
            exit_reason = ?,
            market_result = ?,
            price_change_points = ?,
            return_percent = ?,
            updated_at = ?
        WHERE id = ? AND result = 'open'
    """, (
        result,
        exit_at,
        float(Decimal(str(exit_price))),
        exit_orderbook_log_id,
        exit_reason,
        market_result,
        price_change_points,
        return_percent,
        now_iso(),
        deal["id"],
    ))


def resolve_market_result(event_row: sqlite3.Row) -> Optional[str]:
    if int(event_row["closed"] or 0) != 1 and event_row["status"] != "closed":
        return None

    prices = safe_json_loads(event_row["outcome_prices"])
    if len(prices) < 2:
        return None

    parsed: list[Decimal] = []
    for value in prices[:2]:
        try:
            parsed.append(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return None

    if parsed[0] == Decimal("1") and parsed[1] == Decimal("0"):
        return "yes"
    if parsed[1] == Decimal("1") and parsed[0] == Decimal("0"):
        return "no"
    return None


def close_deals_for_event_resolution(conn: sqlite3.Connection, event_id: str) -> None:
    conn.row_factory = sqlite3.Row
    event_row = conn.execute("""
        SELECT *
        FROM events
        WHERE event_slug = ?
        LIMIT 1
    """, (event_id,)).fetchone()
    if not event_row:
        return

    market_result = resolve_market_result(event_row)
    if not market_result:
        return

    open_deals = conn.execute("""
        SELECT *
        FROM deals
        WHERE event_id = ? AND result = 'open'
    """, (event_id,)).fetchall()

    for deal in open_deals:
        result = "win" if deal["side"] == market_result else "loss"
        exit_price = 1 if result == "win" else 0
        close_deal(
            conn,
            deal,
            result,
            "event_resolution",
            exit_price,
            now_iso(),
            None,
            market_result,
        )
        print(
            f"[deals] closed id={deal['id']} rule_id={deal['rule_id']} "
            f"reason=event_resolution market_result={market_result} result={result}",
            flush=True,
        )


def floor_to_5m_epoch(dt: datetime) -> int:
    ts = int(dt.timestamp())
    return (ts // 300) * 300


def floor_to_epoch(dt: datetime, seconds: int) -> int:
    ts = int(dt.timestamp())
    return (ts // seconds) * seconds


def iso_from_epoch(epoch: int | float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def truncate_text(value: Optional[str], limit: int = 240) -> Optional[str]:
    if not value:
        return None
    return value if len(value) <= limit else f"{value[:limit - 3]}..."


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

    with connect_db() as conn:
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

        close_deals_for_event_resolution(conn, market_row["event_slug"])
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


def coinbase_candles_url() -> str:
    if "{product_id}" in COINBASE_CANDLES_URL:
        return COINBASE_CANDLES_URL.format(product_id=COINBASE_PRODUCT_ID)
    return COINBASE_CANDLES_URL


def coinbase_candle_params(now_dt: datetime) -> dict[str, Any]:
    bucket_epoch = floor_to_epoch(now_dt, COINBASE_CANDLE_GRANULARITY_SECONDS)
    bucket_start = datetime.fromtimestamp(bucket_epoch, timezone.utc)
    return {
        "granularity": COINBASE_CANDLE_GRANULARITY_SECONDS,
        "start": iso_z(bucket_start),
        "end": iso_z(now_dt),
    }


async def fetch_coinbase_candles(
    client: httpx.AsyncClient,
    params: Optional[dict[str, Any]] = None,
) -> tuple[Optional[list[Any]], Optional[str]]:
    retry_statuses = {429, 500, 502, 503, 504}
    last_error = None
    request_params = params or {"granularity": COINBASE_CANDLE_GRANULARITY_SECONDS}

    for attempt in range(3):
        try:
            response = await client.get(
                coinbase_candles_url(),
                params=request_params,
                headers={"User-Agent": "polymarket-btc-collector/1.0"},
            )
            if response.status_code == 200:
                payload = response.json()
                if not isinstance(payload, list):
                    return None, "Coinbase response is not a list"
                return payload, None

            last_error = f"Coinbase HTTP {response.status_code}: {response.text[:160]}"
            if response.status_code not in retry_statuses:
                break
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < 2:
            await asyncio.sleep(1)

    return None, last_error or "Coinbase request failed"


async def fetch_current_coinbase_candle(
    client: httpx.AsyncClient,
) -> tuple[Optional[dict[str, Any]], datetime, Optional[str], int, dict[str, Any]]:
    attempts = COINBASE_MISSING_CANDLE_RETRY_COUNT + 1
    last_error = None
    last_now = now_utc()
    last_params = coinbase_candle_params(last_now)

    for attempt in range(attempts):
        last_now = now_utc()
        last_params = coinbase_candle_params(last_now)
        candles, fetch_error = await fetch_coinbase_candles(client, last_params)
        if fetch_error or candles is None:
            last_error = fetch_error or "Coinbase candles missing"
        else:
            candle, candle_error = select_current_coinbase_candle(candles, last_now)
            if candle:
                return candle, last_now, None, attempt + 1, last_params
            last_error = candle_error or "Coinbase candle selection failed"

        if last_error == "Current Coinbase candle not found" and attempt < attempts - 1:
            await asyncio.sleep(COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS)
            continue
        break

    return None, last_now, last_error, min(attempts, attempt + 1), last_params


def select_current_coinbase_candle(
    candles: list[Any],
    now_dt: datetime,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    current_bucket_epoch = floor_to_epoch(now_dt, COINBASE_CANDLE_GRANULARITY_SECONDS)

    for candle in candles:
        if not isinstance(candle, list) or len(candle) < 6:
            continue

        try:
            candle_time = int(float(candle[0]))
            volume = float(candle[5])
        except (TypeError, ValueError):
            continue

        if volume < 0:
            return None, "Coinbase candle volume is negative"

        if candle_time == current_bucket_epoch:
            return {
                "candle_start_epoch": candle_time,
                "candle_start_at": iso_from_epoch(candle_time),
                "volume_btc_cumulative": volume,
            }, None

    return None, "Current Coinbase candle not found"


def get_latest_valid_coinbase_sample(
    conn: sqlite3.Connection,
    product_id: str,
) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT
            sampled_at,
            candle_start_at,
            volume_btc_cumulative,
            status
        FROM btc_volume_log
        WHERE
            product_id = ?
            AND status IN ('success', 'baseline')
            AND volume_btc_cumulative IS NOT NULL
        ORDER BY sampled_at DESC
        LIMIT 1
    """, (product_id,)).fetchone()


def calculate_coinbase_delta(
    previous_sample: Optional[sqlite3.Row],
    candle_start_at: str,
    sampled_at_dt: datetime,
    volume_btc_cumulative: float,
) -> tuple[Optional[float], Optional[float], str, Optional[str]]:
    if not previous_sample:
        return None, None, "baseline", "no previous valid sample"

    previous_sampled_at = parse_iso_datetime(previous_sample["sampled_at"])
    previous_candle_start_at = previous_sample["candle_start_at"]
    previous_volume = to_float(previous_sample["volume_btc_cumulative"])

    if not previous_sampled_at or previous_volume is None:
        return None, None, "baseline", "previous sample is incomplete"

    seconds_since_previous = (sampled_at_dt - previous_sampled_at).total_seconds()

    if previous_candle_start_at != candle_start_at:
        return None, seconds_since_previous, "baseline", "new candle"

    if seconds_since_previous > COINBASE_MAX_DELTA_GAP_SECONDS:
        return None, seconds_since_previous, "baseline", "gap exceeded max delta threshold"

    delta = volume_btc_cumulative - previous_volume
    if delta < 0:
        return None, seconds_since_previous, "baseline", "cumulative volume decreased within same candle"

    return round(delta, 8), seconds_since_previous, "success", None


def current_active_market_snapshot() -> tuple[Optional[str], Optional[str]]:
    market = active_market
    if not market:
        return None, None
    return market.get("event_slug"), market.get("condition_id")


def insert_btc_volume_log(row: dict[str, Any]) -> bool:
    inserted = False
    with connect_db() as conn:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO btc_volume_log (
                sampled_at,
                sample_bucket_at,
                candle_start_at,
                product_id,
                granularity_seconds,
                volume_btc_cumulative,
                volume_btc_delta,
                seconds_since_previous_sample,
                event_slug,
                condition_id,
                source,
                status,
                error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.get("sampled_at"),
            row.get("sample_bucket_at"),
            row.get("candle_start_at"),
            row.get("product_id"),
            row.get("granularity_seconds"),
            row.get("volume_btc_cumulative"),
            row.get("volume_btc_delta"),
            row.get("seconds_since_previous_sample"),
            row.get("event_slug"),
            row.get("condition_id"),
            row.get("source"),
            row.get("status"),
            row.get("error"),
        ))
        inserted = cursor.rowcount > 0
        cursor.close()
        conn.commit()
    conn.close()
    return inserted


def get_latest_coinbase_health() -> dict[str, Optional[str]]:
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute("""
            SELECT sampled_at, status, error
            FROM btc_volume_log
            ORDER BY sampled_at DESC
            LIMIT 1
        """).fetchone()
        latest_success = conn.execute("""
            SELECT sampled_at
            FROM btc_volume_log
            WHERE status = 'success'
            ORDER BY sampled_at DESC
            LIMIT 1
        """).fetchone()

    if not latest:
        return {
            "last_sample_at": coinbase_volume_state.get("last_sample_at"),
            "last_success_at": coinbase_volume_state.get("last_success_at"),
            "status": coinbase_volume_state.get("status") or "starting",
            "last_error": coinbase_volume_state.get("last_error"),
        }

    status = "ok" if latest["status"] in ("success", "baseline") else "degraded"
    latest_dt = parse_iso_datetime(latest["sampled_at"])
    if latest_dt and (now_utc() - latest_dt).total_seconds() > COINBASE_VOLUME_POLL_INTERVAL_SECONDS * 3:
        status = "stale"

    return {
        "last_sample_at": latest["sampled_at"],
        "last_success_at": latest_success["sampled_at"] if latest_success else None,
        "status": status,
        "last_error": truncate_text(latest["error"]),
    }


async def collect_coinbase_volume_sample(client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    owns_client = client is None
    event_slug, condition_id = current_active_market_snapshot()

    def build_error_row(sampled_at_dt: datetime, error: str) -> dict[str, Any]:
        sample_bucket_epoch = floor_to_epoch(sampled_at_dt, COINBASE_VOLUME_POLL_INTERVAL_SECONDS)
        return {
            "sampled_at": sampled_at_dt.isoformat(),
            "sample_bucket_at": iso_from_epoch(sample_bucket_epoch),
            "candle_start_at": iso_from_epoch(floor_to_epoch(sampled_at_dt, COINBASE_CANDLE_GRANULARITY_SECONDS)),
            "product_id": COINBASE_PRODUCT_ID,
            "granularity_seconds": COINBASE_CANDLE_GRANULARITY_SECONDS,
            "volume_btc_cumulative": None,
            "volume_btc_delta": None,
            "seconds_since_previous_sample": None,
            "event_slug": event_slug,
            "condition_id": condition_id,
            "source": "coinbase_exchange",
            "status": "error",
            "error": truncate_text(error),
        }

    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=COINBASE_REQUEST_TIMEOUT_SECONDS)

        assert client is not None
        candle, sampled_at_dt, fetch_error, attempts, request_params = await fetch_current_coinbase_candle(client)
        sample_bucket_epoch = floor_to_epoch(sampled_at_dt, COINBASE_VOLUME_POLL_INTERVAL_SECONDS)
        if fetch_error or candle is None:
            row = build_error_row(sampled_at_dt, f"{fetch_error or 'Coinbase candle selection failed'}; attempts={attempts}")
        else:
            with connect_db() as conn:
                previous_sample = get_latest_valid_coinbase_sample(conn, COINBASE_PRODUCT_ID)

            delta, seconds_since_previous, status, delta_error = calculate_coinbase_delta(
                previous_sample,
                candle["candle_start_at"],
                sampled_at_dt,
                candle["volume_btc_cumulative"],
            )

            row = {
                "sampled_at": sampled_at_dt.isoformat(),
                "sample_bucket_at": iso_from_epoch(sample_bucket_epoch),
                "candle_start_at": candle["candle_start_at"],
                "product_id": COINBASE_PRODUCT_ID,
                "granularity_seconds": COINBASE_CANDLE_GRANULARITY_SECONDS,
                "volume_btc_cumulative": candle["volume_btc_cumulative"],
                "volume_btc_delta": delta,
                "seconds_since_previous_sample": seconds_since_previous,
                "event_slug": event_slug,
                "condition_id": condition_id,
                "source": "coinbase_exchange",
                "status": status,
                "error": delta_error,
            }

        inserted = insert_btc_volume_log(row)
        coinbase_volume_state["last_sample_at"] = row["sampled_at"]
        coinbase_volume_state["status"] = "ok" if row["status"] in ("success", "baseline") else "degraded"
        coinbase_volume_state["last_error"] = row.get("error")
        if row["status"] == "success":
            coinbase_volume_state["last_success_at"] = row["sampled_at"]

        log_prefix = (
            "Coinbase BTC volume sample saved"
            if row["status"] == "success"
            else "Coinbase BTC volume baseline saved"
            if row["status"] == "baseline"
            else "Coinbase BTC volume collection failed"
        )
        print(
            f"{log_prefix}: candle_start={row['candle_start_at']} "
            f"cumulative_volume={row['volume_btc_cumulative']} "
            f"delta={row['volume_btc_delta']} event_slug={row['event_slug']} "
            f"inserted={inserted}",
            flush=True,
        )
        return row
    finally:
        if owns_client and client is not None:
            await client.aclose()


def empty_volume_metrics() -> dict[str, Any]:
    return {
        "up_volume_shares_10s": 0.0,
        "down_volume_shares_10s": 0.0,
        "up_volume_usdc_10s": 0.0,
        "down_volume_usdc_10s": 0.0,
        "trades_count_10s": 0,
    }


def side_ask(row: dict[str, Any] | sqlite3.Row, side: str) -> Any:
    return row["up_best_ask"] if side == "yes" else row["down_best_ask"]


def side_bid(row: dict[str, Any] | sqlite3.Row, side: str) -> Any:
    return row["up_best_bid"] if side == "yes" else row["down_best_bid"]


def count_entries(conn: sqlite3.Connection, rule_id: int, event_id: str, side: str) -> int:
    return int(conn.execute("""
        SELECT COUNT(*)
        FROM deals
        WHERE rule_id = ? AND event_id = ? AND side = ?
    """, (rule_id, event_id, side)).fetchone()[0])


def has_open_deal(conn: sqlite3.Connection, rule_id: int) -> bool:
    row = conn.execute("""
        SELECT id
        FROM deals
        WHERE rule_id = ? AND result = 'open'
        LIMIT 1
    """, (rule_id,)).fetchone()
    return row is not None


def process_demo_exits(
    conn: sqlite3.Connection,
    orderbook_row: dict[str, Any],
    orderbook_log_id: int,
) -> set[int]:
    closed_rule_ids: set[int] = set()
    open_deals = conn.execute("""
        SELECT
            deals.*,
            rules.stop_loss_price,
            rules.take_profit_price
        FROM deals
        JOIN rules ON rules.id = deals.rule_id
        WHERE deals.result = 'open'
    """).fetchall()

    for deal in open_deals:
        bid = side_bid(orderbook_row, deal["side"])
        bid_price = decimal_from_db(bid)
        if bid_price is None:
            continue

        stop_loss = decimal_from_db(deal["stop_loss_price"])
        take_profit = decimal_from_db(deal["take_profit_price"])
        if stop_loss is None or take_profit is None:
            continue

        # Stop loss wins if both thresholds are considered hit in the same processing pass.
        if bid_price <= stop_loss:
            close_deal(
                conn,
                deal,
                "loss",
                "stop_loss",
                stop_loss,
                orderbook_row["sampled_at"],
                orderbook_log_id,
            )
            closed_rule_ids.add(int(deal["rule_id"]))
            print(
                f"[deals] closed id={deal['id']} rule_id={deal['rule_id']} "
                f"reason=stop_loss bid={bid} exit_price={stop_loss}",
                flush=True,
            )
            continue

        if bid_price >= take_profit:
            close_deal(
                conn,
                deal,
                "win",
                "take_profit",
                take_profit,
                orderbook_row["sampled_at"],
                orderbook_log_id,
            )
            closed_rule_ids.add(int(deal["rule_id"]))
            print(
                f"[deals] closed id={deal['id']} rule_id={deal['rule_id']} "
                f"reason=take_profit bid={bid} exit_price={take_profit}",
                flush=True,
            )

    return closed_rule_ids


def process_demo_entries(
    conn: sqlite3.Connection,
    orderbook_row: dict[str, Any],
    orderbook_log_id: int,
    closed_rule_ids: set[int],
) -> None:
    event_id = orderbook_row.get("event_slug")
    if not event_id:
        return

    rules = conn.execute("""
        SELECT *
        FROM rules
        WHERE status = 'active'
        ORDER BY id ASC
    """).fetchall()

    for rule in rules:
        rule_id = int(rule["id"])
        if rule_id in closed_rule_ids:
            continue

        if rule["eligible_after_event_id"] and rule["eligible_after_event_id"] == event_id:
            print(f"[rules] rule id={rule_id} waits for next event after {event_id}", flush=True)
            continue

        yes_match = prices_equal(side_ask(orderbook_row, "yes"), rule["entry_price"])
        no_match = prices_equal(side_ask(orderbook_row, "no"), rule["entry_price"])

        if yes_match and no_match:
            print(
                f"[deals] both sides match entry price; no deal opened "
                f"rule_id={rule_id} event_id={event_id} orderbook_log_id={orderbook_log_id}",
                flush=True,
            )
            continue

        side = "yes" if yes_match else "no" if no_match else None
        if side is None:
            continue

        if has_open_deal(conn, rule_id):
            print(f"[deals] open deal already exists for rule_id={rule_id}; entry skipped", flush=True)
            continue

        max_entries = (
            int(rule["max_yes_entries_per_event"])
            if side == "yes"
            else int(rule["max_no_entries_per_event"])
        )
        current_entries = count_entries(conn, rule_id, event_id, side)
        if current_entries >= max_entries:
            print(
                f"[deals] entry quota reached rule_id={rule_id} event_id={event_id} "
                f"side={side} quota={max_entries}",
                flush=True,
            )
            continue

        created_at = now_iso()
        try:
            cursor = conn.execute("""
                INSERT INTO deals (
                    rule_id,
                    rule_name,
                    event_id,
                    side,
                    result,
                    entry_at,
                    entry_price,
                    entry_orderbook_log_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """, (
                rule_id,
                rule["name"],
                event_id,
                side,
                orderbook_row["sampled_at"],
                rule["entry_price"],
                orderbook_log_id,
                created_at,
                created_at,
            ))
        except sqlite3.IntegrityError as exc:
            print(
                f"[deals] duplicate or concurrent entry prevented rule_id={rule_id} "
                f"event_id={event_id} side={side} orderbook_log_id={orderbook_log_id}: {exc}",
                flush=True,
            )
            continue

        print(
            f"[deals] opened id={cursor.lastrowid} rule_id={rule_id} "
            f"event_id={event_id} side={side} entry_price={rule['entry_price']} "
            f"orderbook_log_id={orderbook_log_id}",
            flush=True,
        )


def process_demo_trading_for_orderbook(
    conn: sqlite3.Connection,
    orderbook_row: dict[str, Any],
    orderbook_log_id: int,
) -> None:
    closed_rule_ids = process_demo_exits(conn, orderbook_row, orderbook_log_id)
    process_demo_entries(conn, orderbook_row, orderbook_log_id, closed_rule_ids)


def insert_orderbook_log(row: dict[str, Any]) -> int:
    with connect_db() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("""
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
        orderbook_log_id = int(cursor.lastrowid)
        process_demo_trading_for_orderbook(conn, row, orderbook_log_id)
        conn.commit()
        return orderbook_log_id


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

            async with httpx.AsyncClient(timeout=10) as client:
                up_book, up_error = await fetch_book(client, up_token_id)
                down_book, down_error = await fetch_book(client, down_token_id)

            errors = []
            if up_error:
                errors.append(f"up_error={up_error}")
            if down_error:
                errors.append(f"down_error={down_error}")

            up_best_bid = best_bid(up_book) if up_book else None
            up_best_ask = best_ask(up_book) if up_book else None
            down_best_bid = best_bid(down_book) if down_book else None
            down_best_ask = best_ask(down_book) if down_book else None
            volume_metrics = empty_volume_metrics()

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
                "up_volume_shares_10s": volume_metrics["up_volume_shares_10s"],
                "down_volume_shares_10s": volume_metrics["down_volume_shares_10s"],
                "up_volume_usdc_10s": volume_metrics["up_volume_usdc_10s"],
                "down_volume_usdc_10s": volume_metrics["down_volume_usdc_10s"],
                "trades_count_10s": volume_metrics["trades_count_10s"],
                "trades_window_start": None,
                "trades_window_start_local": None,
                "trades_window_end": None,
                "trades_window_end_local": None,
                "trades_error": None,
                "status": status,
                "error": " | ".join(errors) if errors else None,
            }

            insert_orderbook_log(row)

            print(
                f"[book] {row['event_slug']} status={status} "
                f"up={up_best_bid}/{up_best_ask} down={down_best_bid}/{down_best_ask} "
                "volume_collection=disabled",
                flush=True,
            )
        except Exception as e:
            log_error("book loop", e)

        finally:
            await sleep_until_next_tick(BOOK_CHECK_INTERVAL_SECONDS, tick_started_at)


async def coinbase_volume_collector_loop() -> None:
    while True:
        tick_started_at = asyncio.get_running_loop().time()

        try:
            await collect_coinbase_volume_sample()
        except Exception as e:
            coinbase_volume_state["status"] = "degraded"
            coinbase_volume_state["last_error"] = truncate_text(f"{type(e).__name__}: {e}")
            log_error("coinbase volume loop", e)
        finally:
            await sleep_until_next_tick(COINBASE_VOLUME_POLL_INTERVAL_SECONDS, tick_started_at)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(event_collector_loop())
    asyncio.create_task(orderbook_collector_loop())
    asyncio.create_task(coinbase_volume_collector_loop())


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


def display_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def format_storage_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def render_storage_status() -> str:
    usage = shutil.disk_usage(APP_DIR)
    used_percent = (usage.used / usage.total * 100) if usage.total else 0
    return (
        f"Storage: {format_storage_size(usage.free)} free of "
        f"{format_storage_size(usage.total)} ({used_percent:.1f}% used)"
    )


def render_btc_volume_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "<p>No Coinbase BTC volume data yet.</p>"

    headers = [
        "Sampled At",
        "Candle Start",
        "Product",
        "Cumulative Volume BTC",
        "Volume Delta BTC",
        "Seconds Since Previous Sample",
        "Event Slug",
        "Status",
        "Error",
    ]
    fields = [
        "sampled_at",
        "candle_start_at",
        "product_id",
        "volume_btc_cumulative",
        "volume_btc_delta",
        "seconds_since_previous_sample",
        "event_slug",
        "status",
        "error",
    ]

    thead = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = ""
    for row in rows:
        cells = ""
        for field in fields:
            value = display_value(row[field])
            if field == "error" and len(value) > 140:
                value = f"{value[:137]}..."
            cells += f"<td>{html.escape(value)}</td>"
        body += f"<tr>{cells}</tr>"

    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr>{thead}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>
    """


def render_btc_volume_summary(summary: Optional[sqlite3.Row], health_row: dict[str, Optional[str]]) -> str:
    latest_cumulative = display_value(summary["volume_btc_cumulative"]) if summary else "-"
    latest_delta = display_value(summary["volume_btc_delta"]) if summary else "-"
    last_success = display_value(health_row.get("last_success_at"))
    status = display_value(health_row.get("status"))

    return f"""
    <p class="muted">
        Latest cumulative volume: {html.escape(latest_cumulative)} BTC |
        Latest delta: {html.escape(latest_delta)} BTC |
        Last successful sample: {html.escape(last_success)} |
        Collector status: {html.escape(status)}
    </p>
    """


def latest_export_path() -> Optional[Path]:
    state_path = export_state.get("path")
    if state_path:
        path = Path(state_path)
        if path.exists():
            return path

    if not EXPORT_DIR.exists():
        return None

    exports = sorted(
        EXPORT_DIR.glob(f"{EXPORT_FILE_PREFIX}*{EXPORT_FILE_SUFFIX}"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return exports[0] if exports else None


def write_xlsx_export() -> tuple[Path, dict[str, int]]:
    init_db()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = now_utc().strftime("%Y%m%d_%H%M%S")
    final_path = EXPORT_DIR / f"{EXPORT_FILE_PREFIX}{timestamp}{EXPORT_FILE_SUFFIX}"
    temp_path = EXPORT_DIR / f".{final_path.stem}.tmp{EXPORT_FILE_SUFFIX}"
    row_counts: dict[str, int] = {}

    workbook = Workbook(write_only=True)
    conn = connect_db()
    try:
        for sheet_name, headers, query in EXPORT_SHEETS:
            worksheet = workbook.create_sheet(sheet_name)
            worksheet.append(headers)
            cursor = conn.execute(query)
            try:
                count = 0
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    for row in rows:
                        worksheet.append(list(row))
                    count += len(rows)
                row_counts[sheet_name] = count
            finally:
                cursor.close()
    finally:
        conn.close()

    workbook.save(temp_path)
    temp_path.replace(final_path)
    return final_path, row_counts


def run_xlsx_export() -> None:
    try:
        export_path, row_counts = write_xlsx_export()
        with export_state_lock:
            export_state.update({
                "status": "ready",
                "finished_at": now_iso(),
                "filename": export_path.name,
                "path": str(export_path),
                "error": None,
                "row_counts": json.dumps(row_counts, ensure_ascii=False),
            })
        print(f"[export] Excel ready: {export_path} rows={row_counts}", flush=True)
    except Exception as e:
        with export_state_lock:
            export_state.update({
                "status": "error",
                "finished_at": now_iso(),
                "error": truncate_text(f"{type(e).__name__}: {e}", 500),
            })
        log_error("excel export", e)


def render_export_actions() -> str:
    with export_state_lock:
        state = dict(export_state)

    latest_path = latest_export_path()
    status = state.get("status") or "idle"
    started_at = display_value(state.get("started_at"))
    finished_at = display_value(state.get("finished_at"))
    filename = state.get("filename") or (latest_path.name if latest_path else None)
    error = state.get("error")
    row_counts = state.get("row_counts")
    generate_disabled = " disabled" if status == "running" else ""
    download_disabled = "" if latest_path else " disabled"
    download_href = "/download.xlsx" if latest_path else "#"
    summary_parts = [f"Status: {status}"]
    if status == "running":
        summary_parts.append(f"Started: {started_at}")
    if status == "ready":
        summary_parts.append(f"Ready: {finished_at}")
    if filename:
        summary_parts.append(f"File: {filename}")
    if row_counts:
        summary_parts.append(f"Rows: {row_counts}")
    if error:
        summary_parts.append(f"Error: {error}")
    summary = " | ".join(summary_parts)

    return f"""
    <form method="post" action="/generate.xlsx" class="inline-form">
        <button class="button" type="submit"{generate_disabled}>Generate Excel</button>
    </form>
    <a class="button{download_disabled}" href="{download_href}">Download Excel</a>
    <div class="muted export-status">{html.escape(summary)}</div>
    """


def render_rule_actions() -> str:
    return """
    <button class="button" type="button" onclick="openRuleModal()">Create Rule / יצירת חוק</button>
    <button class="button secondary" type="button" onclick="openDeactivateModal()">Deactivate Rule / השבתת חוק</button>

    <div id="rule-modal" class="modal" hidden>
      <div class="modal-panel">
        <h2>Create Rule / יצירת חוק</h2>
        <label>Name <input id="rule-name" type="text"></label>
        <label>Entry price <input id="rule-entry" type="number" step="0.01" min="0.01" max="0.99"></label>
        <label>Stop loss <input id="rule-stop" type="number" step="0.01" min="0.01" max="0.99"></label>
        <label>Take profit <input id="rule-take" type="number" step="0.01" min="0.01" max="0.99"></label>
        <label>Max YES entries <input id="rule-max-yes" type="number" step="1" min="0" value="1"></label>
        <label>Max NO entries <input id="rule-max-no" type="number" step="1" min="0" value="1"></label>
        <label>Status
          <select id="rule-status">
            <option value="active">active</option>
            <option value="inactive">inactive</option>
          </select>
        </label>
        <div id="rule-error" class="error"></div>
        <div class="modal-actions">
          <button class="button" type="button" onclick="submitRule()">Save</button>
          <button class="button secondary" type="button" onclick="closeRuleModal()">Cancel</button>
        </div>
      </div>
    </div>

    <div id="deactivate-modal" class="modal" hidden>
      <div class="modal-panel small">
        <h2>Deactivate Rule / השבתת חוק</h2>
        <label>Rule ID <input id="deactivate-rule-id" type="number" step="1" min="1"></label>
        <div id="deactivate-error" class="error"></div>
        <div class="modal-actions">
          <button class="button" type="button" onclick="submitDeactivate()">Deactivate</button>
          <button class="button secondary" type="button" onclick="closeDeactivateModal()">Cancel</button>
        </div>
      </div>
    </div>
    """


def render_dashboard_scripts() -> str:
    return """
    <script>
      let dashboardRefreshInFlight = false;

      function modalIsOpen() {
        return !document.getElementById("rule-modal").hidden ||
          !document.getElementById("deactivate-modal").hidden;
      }

      function userIsEditing() {
        const element = document.activeElement;
        if (!element) {
          return false;
        }
        return ["INPUT", "SELECT", "TEXTAREA"].includes(element.tagName);
      }

      function autoRefreshIsPaused() {
        return modalIsOpen() || userIsEditing();
      }

      async function refreshDashboardContent(force = false) {
        if (dashboardRefreshInFlight || (!force && autoRefreshIsPaused())) {
          return;
        }
        dashboardRefreshInFlight = true;
        try {
          const response = await fetch("/dashboard-content", {headers: {"X-Requested-With": "fetch"}});
          if (response.ok) {
            document.getElementById("dashboard-content").innerHTML = await response.text();
          }
        } finally {
          dashboardRefreshInFlight = false;
        }
      }

      function openRuleModal() {
        document.getElementById("rule-error").textContent = "";
        document.getElementById("rule-modal").hidden = false;
      }
      function closeRuleModal() {
        document.getElementById("rule-modal").hidden = true;
      }
      function openDeactivateModal() {
        document.getElementById("deactivate-error").textContent = "";
        document.getElementById("deactivate-modal").hidden = false;
      }
      function closeDeactivateModal() {
        document.getElementById("deactivate-modal").hidden = true;
      }
      async function submitRule() {
        const payload = {
          name: document.getElementById("rule-name").value,
          entry_price: document.getElementById("rule-entry").value,
          stop_loss_price: document.getElementById("rule-stop").value,
          take_profit_price: document.getElementById("rule-take").value,
          max_yes_entries_per_event: document.getElementById("rule-max-yes").value,
          max_no_entries_per_event: document.getElementById("rule-max-no").value,
          status: document.getElementById("rule-status").value
        };
        const response = await fetch("/rules", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) {
          document.getElementById("rule-error").textContent = data.detail || "Rule creation failed";
          return;
        }
        closeRuleModal();
        await refreshDashboardContent(true);
      }
      async function submitDeactivate() {
        const ruleId = document.getElementById("deactivate-rule-id").value;
        const response = await fetch(`/rules/${ruleId}/deactivate`, {method: "POST"});
        const data = await response.json();
        if (!response.ok) {
          document.getElementById("deactivate-error").textContent = data.detail || "Deactivate failed";
          return;
        }
        closeDeactivateModal();
        await refreshDashboardContent(true);
      }

      window.setInterval(() => refreshDashboardContent(false), 10000);
    </script>
    """


def load_dashboard_rows() -> tuple[
    list[sqlite3.Row],
    list[sqlite3.Row],
    list[sqlite3.Row],
    Optional[sqlite3.Row],
    list[sqlite3.Row],
    list[sqlite3.Row],
    dict[str, Optional[str]],
]:
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

        btc_volume_rows = conn.execute("""
            SELECT
                sampled_at,
                candle_start_at,
                product_id,
                volume_btc_cumulative,
                volume_btc_delta,
                seconds_since_previous_sample,
                event_slug,
                status,
                error
            FROM btc_volume_log
            ORDER BY sampled_at DESC
            LIMIT 50
        """).fetchall()

        btc_volume_summary = conn.execute("""
            SELECT
                volume_btc_cumulative,
                volume_btc_delta
            FROM btc_volume_log
            WHERE status IN ('success', 'baseline')
            ORDER BY sampled_at DESC
            LIMIT 1
        """).fetchone()

        rules = conn.execute("""
            SELECT
                id,
                name,
                entry_price,
                stop_loss_price,
                take_profit_price,
                max_yes_entries_per_event,
                max_no_entries_per_event,
                status,
                eligible_after_event_id,
                created_at,
                updated_at
            FROM rules
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

        deals = conn.execute("""
            SELECT
                id AS deal_id,
                rule_id,
                rule_name,
                event_id,
                side,
                result,
                entry_at,
                entry_price,
                exit_at,
                exit_price,
                exit_reason,
                market_result,
                price_change_points,
                return_percent
            FROM deals
            ORDER BY id DESC
            LIMIT 200
        """).fetchall()

    btc_health = get_latest_coinbase_health()
    return events, logs, btc_volume_rows, btc_volume_summary, rules, deals, btc_health


def render_dashboard_content() -> str:
    events, logs, btc_volume_rows, btc_volume_summary, rules, deals, btc_health = load_dashboard_rows()
    return f"""
        <div class="storage-status">{html.escape(render_storage_status())}</div>
        <div class="muted">Auto refresh every 10 seconds unless a form is open. Server time: {html.escape(now_iso())}</div>

        <div class="actions">
            {render_export_actions()}
            {render_rule_actions()}
        </div>

        <div class="card">
            <h2>Events / Markets</h2>
            {render_table(events)}
        </div>

        <div class="card">
            <h2>Coinbase BTC Volume</h2>
            {render_btc_volume_summary(btc_volume_summary, btc_health)}
            {render_btc_volume_table(btc_volume_rows)}
        </div>

        <div class="card">
            <h2>Rules</h2>
            {render_table(rules)}
        </div>

        <div class="card">
            <h2>Deals</h2>
            {render_table(deals)}
        </div>

        <div class="card">
            <h2>Orderbook Log</h2>
            {render_table(logs)}
        </div>
    """


@app.get("/dashboard-content", response_class=HTMLResponse)
def dashboard_content() -> str:
    return render_dashboard_content()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
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
            .inline-form {{
                display: inline-block;
                margin: 0 8px 8px 0;
            }}
            a.button, button.button {{
                display: inline-block;
                padding: 10px 14px;
                background: #111;
                color: white;
                text-decoration: none;
                border-radius: 6px;
                border: 0;
                cursor: pointer;
                font: inherit;
            }}
            a.button.disabled, button.button:disabled {{
                background: #888;
                pointer-events: none;
                cursor: default;
            }}
            button.secondary, a.secondary {{
                background: #555;
            }}
            .export-status {{
                margin-top: 4px;
            }}
            .storage-status {{
                margin: 0 0 6px 0;
                font-size: 14px;
                font-weight: 700;
            }}
            .modal[hidden] {{
                display: none;
            }}
            .modal {{
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.45);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 10;
            }}
            .modal-panel {{
                background: white;
                border-radius: 8px;
                padding: 18px;
                width: min(520px, calc(100vw - 32px));
                box-shadow: 0 8px 24px rgba(0,0,0,0.18);
            }}
            .modal-panel.small {{
                width: min(360px, calc(100vw - 32px));
            }}
            label {{
                display: block;
                margin: 10px 0;
                font-size: 13px;
            }}
            input, select {{
                box-sizing: border-box;
                width: 100%;
                margin-top: 4px;
                padding: 8px;
                border: 1px solid #ccc;
                border-radius: 6px;
                font: inherit;
            }}
            .modal-actions {{
                margin-top: 14px;
            }}
            .error {{
                color: #9b1c1c;
                min-height: 20px;
                font-size: 13px;
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
        <div id="dashboard-content">
            {render_dashboard_content()}
        </div>
        {render_dashboard_scripts()}
    </body>
    </html>
    """


@app.post("/rules")
def api_create_rule(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        row = create_rule(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Database error while creating rule")
    return row_to_dict(row)


@app.get("/rules")
def api_get_rules() -> list[dict[str, Any]]:
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT *
                FROM rules
                ORDER BY id DESC
            """).fetchall()
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Database error while loading rules")
    return [row_to_dict(row) for row in rows]


@app.post("/rules/{rule_id}/deactivate")
def api_deactivate_rule(rule_id: int) -> dict[str, Any]:
    try:
        row, message = deactivate_rule(rule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} does not exist")
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Database error while deactivating rule")
    return {"message": message, "rule": row_to_dict(row)}


@app.get("/deals")
def api_get_deals() -> list[dict[str, Any]]:
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT *
                FROM deals
                ORDER BY id DESC
            """).fetchall()
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="Database error while loading deals")
    return [row_to_dict(row) for row in rows]


@app.post("/generate.xlsx")
def generate_xlsx(background_tasks: BackgroundTasks) -> RedirectResponse:
    with export_state_lock:
        if export_state.get("status") != "running":
            export_state.update({
                "status": "running",
                "started_at": now_iso(),
                "finished_at": None,
                "error": None,
                "row_counts": None,
            })
            background_tasks.add_task(run_xlsx_export)

    return RedirectResponse("/", status_code=303)


@app.get("/download.xlsx")
def download_xlsx():
    export_path = latest_export_path()
    if not export_path:
        return HTMLResponse(
            "<p>No Excel export is ready yet. Generate one first.</p>",
            status_code=404,
        )

    return FileResponse(
        export_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=export_path.name,
    )


@app.get("/health")
def health() -> dict[str, Any]:
    coinbase_health = get_latest_coinbase_health()
    return {
        "ok": True,
        "time": now_iso(),
        "db": str(DB_PATH),
        "active_market": active_market.get("event_slug") if active_market else None,
        "coinbase_volume_last_sample_at": coinbase_health.get("last_sample_at"),
        "coinbase_volume_last_success_at": coinbase_health.get("last_success_at"),
        "coinbase_volume_collector_status": coinbase_health.get("status"),
        "coinbase_volume_last_error": coinbase_health.get("last_error"),
    }
