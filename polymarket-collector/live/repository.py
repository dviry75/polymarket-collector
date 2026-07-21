import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "unmatched", "failed"}
OPEN_DEAL_STATUSES = {"created", "entry_pending", "open", "partially_open", "exit_pending"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class LiveRepository:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc_value, traceback) -> bool:
                result = super().__exit__(exc_type, exc_value, traceback)
                self.close()
                return result

        conn = sqlite3.connect(self.db_path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self, kill_switch_default: bool = True) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_markets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    condition_id TEXT NOT NULL UNIQUE,
                    yes_token_id TEXT,
                    no_token_id TEXT,
                    gamma_yes_token_id TEXT,
                    gamma_no_token_id TEXT,
                    token_mapping_status TEXT NOT NULL DEFAULT 'unknown',
                    min_order_size REAL,
                    min_tick_size REAL,
                    maker_base_fee REAL,
                    taker_base_fee REAL,
                    fee_details TEXT,
                    rfq_enabled INTEGER,
                    itode INTEGER,
                    accepting_orders INTEGER,
                    one_dollar_valid INTEGER,
                    minimum_viable_amount_usd REAL,
                    best_bid REAL,
                    best_ask REAL,
                    orderbook_depth_json TEXT,
                    market_resolved INTEGER NOT NULL DEFAULT 0,
                    winning_asset_id TEXT,
                    winning_outcome TEXT,
                    source TEXT NOT NULL DEFAULT 'public_rest',
                    last_update_at TEXT NOT NULL,
                    raw_market_info TEXT,
                    raw_orderbook TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss_price REAL NOT NULL,
                    take_profit_price REAL NOT NULL,
                    entries_yes_count INTEGER NOT NULL DEFAULT 0,
                    entries_no_count INTEGER NOT NULL DEFAULT 0,
                    requested_amount_usd REAL NOT NULL DEFAULT 1,
                    entry_order_type TEXT NOT NULL DEFAULT 'FOK',
                    max_entry_slippage REAL NOT NULL DEFAULT 0.01,
                    max_exit_slippage REAL NOT NULL DEFAULT 0.02,
                    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
                    eligible_after_event_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    live_rule_id INTEGER,
                    event_id TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    side TEXT CHECK (side IS NULL OR side IN ('buy', 'sell')),
                    status TEXT NOT NULL,
                    requested_amount_usd REAL,
                    requested_size REAL,
                    filled_size REAL NOT NULL DEFAULT 0,
                    remaining_size REAL NOT NULL DEFAULT 0,
                    average_entry_fill_price REAL,
                    average_exit_fill_price REAL,
                    entry_status TEXT,
                    exit_status TEXT,
                    trigger_price REAL,
                    requested_exit_price REAL,
                    realized_pnl_usd REAL NOT NULL DEFAULT 0,
                    fees_usd REAL NOT NULL DEFAULT 0,
                    slippage_usd REAL NOT NULL DEFAULT 0,
                    exit_reason TEXT,
                    resolved_outcome TEXT,
                    winning_asset_id TEXT,
                    redeemable_at TEXT,
                    redeemed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (live_rule_id) REFERENCES live_rules(id)
                );

                CREATE TABLE IF NOT EXISTS live_orders (
                    local_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    polymarket_order_id TEXT,
                    live_deal_id INTEGER,
                    live_rule_id INTEGER,
                    event_id TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    order_type TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    requested_price REAL,
                    requested_amount_usd REAL,
                    requested_size REAL,
                    filled_size REAL NOT NULL DEFAULT 0,
                    remaining_size REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL,
                    status TEXT NOT NULL,
                    submitted_at TEXT,
                    matched_at TEXT,
                    confirmed_at TEXT,
                    cancel_requested_at TEXT,
                    cancelled_at TEXT,
                    failed_at TEXT,
                    failure_reason TEXT,
                    raw_response TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (live_deal_id) REFERENCES live_deals(id),
                    FOREIGN KEY (live_rule_id) REFERENCES live_rules(id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_orders_polymarket_order_id
                ON live_orders(polymarket_order_id) WHERE polymarket_order_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders(status);
                CREATE INDEX IF NOT EXISTS idx_live_orders_deal ON live_orders(live_deal_id);
                CREATE INDEX IF NOT EXISTS idx_live_orders_event ON live_orders(event_id);
                CREATE INDEX IF NOT EXISTS idx_live_orders_rule ON live_orders(live_rule_id);

                CREATE TABLE IF NOT EXISTS live_order_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    live_order_id INTEGER NOT NULL,
                    polymarket_trade_id TEXT,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    matched_at TEXT,
                    confirmed_at TEXT,
                    raw_message TEXT,
                    message_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (live_order_id) REFERENCES live_orders(local_order_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_order_fills_trade
                ON live_order_fills(polymarket_trade_id) WHERE polymarket_trade_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_order_fills_hash
                ON live_order_fills(message_hash) WHERE message_hash IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_live_order_fills_order ON live_order_fills(live_order_id);

                CREATE TABLE IF NOT EXISTS live_websocket_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    event_type TEXT,
                    condition_id TEXT,
                    asset_id TEXT,
                    polymarket_order_id TEXT,
                    polymarket_trade_id TEXT,
                    message_hash TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    status TEXT NOT NULL,
                    raw_message TEXT
                );

                CREATE TABLE IF NOT EXISTS live_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    size REAL NOT NULL DEFAULT 0,
                    average_price REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    redeemable_at TEXT,
                    redeemed_at TEXT,
                    source TEXT NOT NULL DEFAULT 'local',
                    raw_payload TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at TEXT NOT NULL,
                    configured_profile_address TEXT,
                    account_login_type TEXT,
                    resolved_proxy_wallet TEXT,
                    expected_funder_candidate TEXT,
                    account_identity_status TEXT,
                    public_positions_count INTEGER NOT NULL DEFAULT 0,
                    public_positions_value REAL,
                    public_closed_positions_count INTEGER NOT NULL DEFAULT 0,
                    public_activity_count INTEGER NOT NULL DEFAULT 0,
                    balance_usd REAL,
                    allowance_usd REAL,
                    raw_payload TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS live_dry_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    final_decision TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_daily_limits (
                    day_key TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    realized_pnl_usd REAL NOT NULL DEFAULT 0,
                    consecutive_failed_orders INTEGER NOT NULL DEFAULT 0,
                    consecutive_losing_deals INTEGER NOT NULL DEFAULT 0,
                    kill_switch_triggered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    gaps_count INTEGER NOT NULL DEFAULT 0,
                    gaps_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS live_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    details_json TEXT
                );

                CREATE TABLE IF NOT EXISTS live_system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            existing = conn.execute("SELECT value FROM live_system_state WHERE key = 'kill_switch'").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO live_system_state (key, value, updated_at) VALUES ('kill_switch', ?, ?)",
                    ("true" if kill_switch_default else "false", now_iso()),
                )
            conn.commit()

        self._ensure_live_columns()

    def _ensure_live_columns(self) -> None:
        additions = {
            "live_account_snapshots": {
                "configured_profile_address": "TEXT",
                "account_login_type": "TEXT",
                "resolved_proxy_wallet": "TEXT",
                "expected_funder_candidate": "TEXT",
                "account_identity_status": "TEXT",
                "public_positions_count": "INTEGER NOT NULL DEFAULT 0",
                "public_positions_value": "REAL",
                "public_closed_positions_count": "INTEGER NOT NULL DEFAULT 0",
                "public_activity_count": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        with self.connect() as conn:
            for table, columns in additions.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, column_type in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            conn.commit()

    def audit(self, actor: str, action: str, status: str, reason: str = "", details: Optional[dict[str, Any]] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_audit_log (occurred_at, actor, action, status, reason, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), actor, action, status, reason, json_dumps(details or {})),
            )
            conn.commit()

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM live_system_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str, actor: str = "system") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now_iso()),
            )
            conn.commit()
        self.audit(actor, f"set_{key}", "ok", details={"value": value})

    def kill_switch_active(self) -> bool:
        return self.get_state("kill_switch", "true").lower() == "true"

    def upsert_market(self, data: dict[str, Any]) -> None:
        ts = now_iso()
        condition_id = str(data["condition_id"])
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_markets (
                    event_id, condition_id, yes_token_id, no_token_id, gamma_yes_token_id,
                    gamma_no_token_id, token_mapping_status, min_order_size, min_tick_size,
                    maker_base_fee, taker_base_fee, fee_details, rfq_enabled, itode,
                    accepting_orders, one_dollar_valid, minimum_viable_amount_usd,
                    best_bid, best_ask, orderbook_depth_json, market_resolved,
                    winning_asset_id, winning_outcome, source, last_update_at,
                    raw_market_info, raw_orderbook, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    yes_token_id=excluded.yes_token_id,
                    no_token_id=excluded.no_token_id,
                    gamma_yes_token_id=excluded.gamma_yes_token_id,
                    gamma_no_token_id=excluded.gamma_no_token_id,
                    token_mapping_status=excluded.token_mapping_status,
                    min_order_size=excluded.min_order_size,
                    min_tick_size=excluded.min_tick_size,
                    maker_base_fee=excluded.maker_base_fee,
                    taker_base_fee=excluded.taker_base_fee,
                    fee_details=excluded.fee_details,
                    rfq_enabled=excluded.rfq_enabled,
                    itode=excluded.itode,
                    accepting_orders=excluded.accepting_orders,
                    one_dollar_valid=excluded.one_dollar_valid,
                    minimum_viable_amount_usd=excluded.minimum_viable_amount_usd,
                    best_bid=excluded.best_bid,
                    best_ask=excluded.best_ask,
                    orderbook_depth_json=excluded.orderbook_depth_json,
                    market_resolved=excluded.market_resolved,
                    winning_asset_id=excluded.winning_asset_id,
                    winning_outcome=excluded.winning_outcome,
                    source=excluded.source,
                    last_update_at=excluded.last_update_at,
                    raw_market_info=excluded.raw_market_info,
                    raw_orderbook=excluded.raw_orderbook,
                    updated_at=excluded.updated_at
                """,
                (
                    data.get("event_id"),
                    condition_id,
                    data.get("yes_token_id"),
                    data.get("no_token_id"),
                    data.get("gamma_yes_token_id"),
                    data.get("gamma_no_token_id"),
                    data.get("token_mapping_status", "unknown"),
                    data.get("min_order_size"),
                    data.get("min_tick_size"),
                    data.get("maker_base_fee"),
                    data.get("taker_base_fee"),
                    json_dumps(data.get("fee_details")),
                    1 if data.get("rfq_enabled") else 0,
                    1 if data.get("itode") else 0,
                    1 if data.get("accepting_orders") else 0,
                    1 if data.get("one_dollar_valid") else 0,
                    data.get("minimum_viable_amount_usd"),
                    data.get("best_bid"),
                    data.get("best_ask"),
                    json_dumps(data.get("orderbook_depth") or {}),
                    1 if data.get("market_resolved") else 0,
                    data.get("winning_asset_id"),
                    data.get("winning_outcome"),
                    data.get("source", "public_rest"),
                    data.get("last_update_at") or ts,
                    json_dumps(data.get("raw_market_info") or {}),
                    json_dumps(data.get("raw_orderbook") or {}),
                    ts,
                    ts,
                ),
            )
            conn.commit()

    def create_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO live_rules (
                    name, entry_price, stop_loss_price, take_profit_price,
                    requested_amount_usd, entry_order_type, max_entry_slippage,
                    max_exit_slippage, status, eligible_after_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["entry_price"],
                    payload["stop_loss_price"],
                    payload["take_profit_price"],
                    payload.get("requested_amount_usd", 1),
                    payload.get("entry_order_type", "FOK"),
                    payload.get("max_entry_slippage", 0.01),
                    payload.get("max_exit_slippage", 0.02),
                    payload.get("status", "inactive"),
                    payload.get("eligible_after_event_id"),
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM live_rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
            conn.commit()
        self.audit("operator", "create_live_rule", "ok", details={"rule_id": row["id"]})
        return row_to_dict(row) or {}

    def update_rule_status(self, rule_id: int, status: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("UPDATE live_rules SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), rule_id))
            row = conn.execute("SELECT * FROM live_rules WHERE id = ?", (rule_id,)).fetchone()
            conn.commit()
        if row is None:
            raise KeyError(rule_id)
        self.audit("operator", "update_live_rule_status", "ok", details={"rule_id": rule_id, "status": status})
        return row_to_dict(row) or {}

    def create_deal(self, payload: dict[str, Any]) -> int:
        ts = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO live_deals (
                    live_rule_id, event_id, condition_id, token_id, outcome, side, status,
                    requested_amount_usd, requested_size, filled_size, remaining_size,
                    entry_status, trigger_price, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("live_rule_id"),
                    payload.get("event_id"),
                    payload.get("condition_id"),
                    payload.get("token_id"),
                    payload.get("outcome"),
                    payload.get("side", "buy"),
                    payload.get("status", "entry_pending"),
                    payload.get("requested_amount_usd"),
                    payload.get("requested_size"),
                    payload.get("requested_size") or 0,
                    payload.get("entry_status", "created"),
                    payload.get("trigger_price"),
                    ts,
                    ts,
                ),
            )
            deal_id = int(cursor.lastrowid)
            conn.commit()
        return deal_id

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = now_iso()
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO live_orders (
                        idempotency_key, live_deal_id, live_rule_id, event_id, condition_id,
                        token_id, outcome, side, order_type, purpose, requested_price,
                        requested_amount_usd, requested_size, remaining_size, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?)
                    """,
                    (
                        payload["idempotency_key"],
                        payload.get("live_deal_id"),
                        payload.get("live_rule_id"),
                        payload.get("event_id"),
                        payload.get("condition_id"),
                        payload.get("token_id"),
                        payload.get("outcome"),
                        payload["side"],
                        payload["order_type"],
                        payload["purpose"],
                        payload.get("requested_price"),
                        payload.get("requested_amount_usd"),
                        payload.get("requested_size"),
                        payload.get("requested_size") or 0,
                        ts,
                        ts,
                    ),
                )
                row = conn.execute("SELECT * FROM live_orders WHERE local_order_id = ?", (cursor.lastrowid,)).fetchone()
                conn.commit()
                return row_to_dict(row) or {}
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT * FROM live_orders WHERE idempotency_key = ?", (payload["idempotency_key"],)).fetchone()
                existing = row_to_dict(row) or {}
                existing["_duplicate"] = True
                return existing

    def update_order(self, local_order_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        if not updates:
            with self.connect() as conn:
                row = conn.execute("SELECT * FROM live_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
            return row_to_dict(row) or {}
        updates = dict(updates)
        updates["updated_at"] = now_iso()
        parts = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as conn:
            conn.execute(f"UPDATE live_orders SET {parts} WHERE local_order_id = ?", (*updates.values(), local_order_id))
            row = conn.execute("SELECT * FROM live_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def add_fill(self, local_order_id: int, fill: dict[str, Any]) -> bool:
        raw = fill.get("raw_message") or fill
        message_hash = fill.get("message_hash") or sha256_text(json_dumps(raw))
        ts = now_iso()
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO live_order_fills (
                        live_order_id, polymarket_trade_id, price, size, fee, status,
                        matched_at, confirmed_at, raw_message, message_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_order_id,
                        fill.get("polymarket_trade_id"),
                        fill["price"],
                        fill["size"],
                        fill.get("fee", 0),
                        fill.get("status", "matched"),
                        fill.get("matched_at"),
                        fill.get("confirmed_at"),
                        json_dumps(raw),
                        message_hash,
                        ts,
                        ts,
                    ),
                )
                self._recalculate_order(conn, local_order_id)
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def _recalculate_order(self, conn: sqlite3.Connection, local_order_id: int) -> None:
        fills = conn.execute(
            "SELECT price, size, fee FROM live_order_fills WHERE live_order_id = ?",
            (local_order_id,),
        ).fetchall()
        filled = sum(Decimal(str(row["size"])) for row in fills)
        notional = sum(Decimal(str(row["price"])) * Decimal(str(row["size"])) for row in fills)
        avg = (notional / filled) if filled else None
        order = conn.execute("SELECT requested_size, requested_amount_usd, order_type FROM live_orders WHERE local_order_id = ?", (local_order_id,)).fetchone()
        requested_size = Decimal(str(order["requested_size"] or 0)) if order else Decimal("0")
        remaining = max(Decimal("0"), requested_size - filled)
        if filled and remaining:
            status = "partially_filled"
        elif filled:
            status = "filled"
        else:
            status = "submitted"
        conn.execute(
            """
            UPDATE live_orders
            SET filled_size = ?, remaining_size = ?, average_fill_price = ?,
                status = ?, matched_at = COALESCE(matched_at, ?), updated_at = ?
            WHERE local_order_id = ?
            """,
            (float(filled), float(remaining), float(avg) if avg is not None else None, status, now_iso(), now_iso(), local_order_id),
        )

    def store_ws_event(self, channel: str, message: dict[str, Any], status: str = "received") -> bool:
        message_hash = sha256_text(json_dumps(message))
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO live_websocket_events (
                        channel, event_type, condition_id, asset_id, polymarket_order_id,
                        polymarket_trade_id, message_hash, received_at, processed_at, status, raw_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel,
                        message.get("event_type") or message.get("type"),
                        message.get("condition_id") or message.get("market"),
                        message.get("asset_id") or message.get("asset_id"),
                        message.get("order_id"),
                        message.get("trade_id"),
                        message_hash,
                        now_iso(),
                        now_iso() if status == "processed" else None,
                        status,
                        json_dumps(message),
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def start_reconciliation(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO live_reconciliation_runs (started_at, status) VALUES (?, 'running')",
                (now_iso(),),
            )
            run_id = int(cursor.lastrowid)
            conn.commit()
        return run_id

    def finish_reconciliation(self, run_id: int, status: str, gaps: list[dict[str, Any]], error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_reconciliation_runs
                SET finished_at = ?, status = ?, gaps_count = ?, gaps_json = ?, error = ?
                WHERE id = ?
                """,
                (now_iso(), status, len(gaps), json_dumps(gaps), error, run_id),
            )
            conn.execute(
                """
                INSERT INTO live_system_state (key, value, updated_at)
                VALUES ('last_reconciliation_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now_iso(), now_iso()),
            )
            if gaps:
                conn.execute(
                    """
                    INSERT INTO live_system_state (key, value, updated_at)
                    VALUES ('live_blocked_by_reconciliation', 'true', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (now_iso(),),
                )
            conn.commit()

    def store_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_account_snapshots (
                    sampled_at, configured_profile_address, account_login_type,
                    resolved_proxy_wallet, expected_funder_candidate, account_identity_status,
                    public_positions_count, public_positions_value, public_closed_positions_count,
                    public_activity_count, balance_usd, allowance_usd, raw_payload, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.get("sampled_at") or now_iso(),
                    snapshot.get("configured_profile_address"),
                    snapshot.get("account_login_type"),
                    snapshot.get("resolved_proxy_wallet"),
                    snapshot.get("expected_funder_candidate"),
                    snapshot.get("account_identity_status") or snapshot.get("status"),
                    snapshot.get("public_positions_count", 0),
                    snapshot.get("public_positions_value"),
                    snapshot.get("public_closed_positions_count", 0),
                    snapshot.get("public_activity_count", 0),
                    snapshot.get("balance_usd"),
                    snapshot.get("allowance_usd"),
                    json_dumps(snapshot.get("raw_public_payload") or snapshot.get("raw_payload") or {}),
                    snapshot.get("status", "unknown"),
                    snapshot.get("error", ""),
                ),
            )
            conn.execute(
                """
                INSERT INTO live_system_state (key, value, updated_at)
                VALUES ('account_identity_status', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (snapshot.get("account_identity_status") or snapshot.get("status", "unknown"), now_iso()),
            )
            conn.commit()

    def latest_account_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM live_account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return row_to_dict(row)

    def store_dry_run(self, preview: dict[str, Any], actor: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_dry_runs (created_at, actor, intent_json, final_decision, reason_codes_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    preview.get("timestamp") or now_iso(),
                    actor,
                    json_dumps(preview),
                    preview.get("final_decision", "BLOCKED"),
                    json_dumps(preview.get("reason_codes") or []),
                ),
            )
            conn.commit()

    def current_daily_limit(self, day_key: str, tz_name: str) -> dict[str, Any]:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_daily_limits (day_key, timezone, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day_key) DO NOTHING
                """,
                (day_key, tz_name, ts, ts),
            )
            row = conn.execute("SELECT * FROM live_daily_limits WHERE day_key = ?", (day_key,)).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def add_failed_order_counter(self, day_key: str, tz_name: str) -> dict[str, Any]:
        self.current_daily_limit(day_key, tz_name)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_daily_limits
                SET consecutive_failed_orders = consecutive_failed_orders + 1, updated_at = ?
                WHERE day_key = ?
                """,
                (now_iso(), day_key),
            )
            row = conn.execute("SELECT * FROM live_daily_limits WHERE day_key = ?", (day_key,)).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def list_table(self, table: str, limit: int = 100) -> list[dict[str, Any]]:
        allowed = {
            "live_markets", "live_rules", "live_deals", "live_orders", "live_order_fills",
            "live_positions", "live_account_snapshots", "live_reconciliation_runs",
            "live_audit_log", "live_websocket_events", "live_dry_runs", "live_daily_limits",
        }
        if table not in allowed:
            raise ValueError(table)
        order_col = "id"
        if table == "live_orders":
            order_col = "local_order_id"
        if table == "live_daily_limits":
            order_col = "day_key"
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "open_orders": int(conn.execute(
                    "SELECT COUNT(*) FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed')"
                ).fetchone()[0]),
                "open_deals": int(conn.execute(
                    "SELECT COUNT(*) FROM live_deals WHERE status IN ('created','entry_pending','open','partially_open','exit_pending')"
                ).fetchone()[0]),
                "markets": int(conn.execute("SELECT COUNT(*) FROM live_markets").fetchone()[0]),
                "rules": int(conn.execute("SELECT COUNT(*) FROM live_rules").fetchone()[0]),
                "active_rules": int(conn.execute("SELECT COUNT(*) FROM live_rules WHERE status = 'active'").fetchone()[0]),
            }

    def current_exposure_usd(self) -> Decimal:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(requested_amount_usd, 0)), 0)
                FROM live_orders
                WHERE status NOT IN ('filled','cancelled','unmatched','failed','blocked')
                """
            ).fetchone()
            deals = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(requested_amount_usd, 0)), 0)
                FROM live_deals
                WHERE status IN ('open','partially_open','exit_pending')
                """
            ).fetchone()
        return Decimal(str(row[0] or 0)) + Decimal(str(deals[0] or 0))

    def latest_market(self, condition_id: Optional[str] = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            if condition_id:
                row = conn.execute("SELECT * FROM live_markets WHERE condition_id = ?", (condition_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM live_markets ORDER BY last_update_at DESC LIMIT 1").fetchone()
        return row_to_dict(row)

    def non_final_orders(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed')"
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]
