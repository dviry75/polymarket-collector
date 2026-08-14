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
    def __init__(self, db_path: Path | str, *, query_only: bool = False):
        self.db_path = Path(db_path)
        self.query_only = bool(query_only)

    def connect(self) -> sqlite3.Connection:
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc_value, traceback) -> bool:
                result = super().__exit__(exc_type, exc_value, traceback)
                self.close()
                return result

        target = (
            f"file:{self.db_path}?mode=ro"
            if self.query_only
            else str(self.db_path)
        )
        conn = sqlite3.connect(
            target, factory=ClosingConnection, timeout=30.0,
            uri=self.query_only,
        )
        conn.row_factory = sqlite3.Row
        # A bounded wait avoids transient writer contention surfacing as an
        # application error. NORMAL is durable with WAL while avoiding one
        # filesystem sync for every high-frequency market-data transaction.
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.query_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def migrate(self, kill_switch_default: bool = True) -> None:
        if self.query_only:
            raise RuntimeError("query-only repository cannot run migrations")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # WAL permits readers while the single writer commits market frames.
            # The setting is persistent for the database and is applied before
            # migrations start any transaction.
            conn.execute("PRAGMA journal_mode = WAL")
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

                CREATE TABLE IF NOT EXISTS live_backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS live_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id TEXT NOT NULL,
                    event_id TEXT,
                    asset_id TEXT NOT NULL,
                    outcome TEXT,
                    event_type TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    best_bid_size REAL,
                    best_ask_size REAL,
                    bids_json TEXT,
                    asks_json TEXT,
                    market_timestamp TEXT,
                    received_at TEXT NOT NULL,
                    latency_ms INTEGER,
                    source TEXT NOT NULL DEFAULT 'POLYMARKET_MARKET_WS',
                    message_hash TEXT NOT NULL UNIQUE,
                    raw_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_live_market_snapshots_asset
                ON live_market_snapshots(asset_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_live_market_snapshots_condition
                ON live_market_snapshots(condition_id, id DESC);

                CREATE TABLE IF NOT EXISTS live_rule_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    live_rule_id INTEGER NOT NULL,
                    market_snapshot_id INTEGER NOT NULL,
                    event_id TEXT,
                    condition_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    outcome TEXT,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observed_best_bid REAL,
                    observed_best_ask REAL,
                    entry_price REAL,
                    evaluated_at TEXT NOT NULL,
                    rule_snapshot_json TEXT NOT NULL,
                    UNIQUE(live_rule_id, market_snapshot_id),
                    FOREIGN KEY (live_rule_id) REFERENCES live_rules(id),
                    FOREIGN KEY (market_snapshot_id) REFERENCES live_market_snapshots(id)
                );
                CREATE INDEX IF NOT EXISTS idx_live_rule_evaluations_rule
                ON live_rule_evaluations(live_rule_id, id DESC);
                """
            )
            existing = conn.execute("SELECT value FROM live_system_state WHERE key = 'kill_switch'").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO live_system_state (key, value, updated_at) VALUES ('kill_switch', ?, ?)",
                    ("true" if kill_switch_default else "false", now_iso()),
                )
            session_version = conn.execute("SELECT value FROM live_system_state WHERE key = 'session_version'").fetchone()
            if session_version is None:
                conn.execute(
                    "INSERT INTO live_system_state (key, value, updated_at) VALUES ('session_version', '1', ?)",
                    (now_iso(),),
                )
            conn.commit()

        self._ensure_live_columns()

    def _ensure_live_columns(self) -> None:
        additions = {
            "live_websocket_events": {
                "message_type": "TEXT", "message_status": "TEXT", "outcome": "TEXT",
                "side": "TEXT", "price": "REAL", "original_size": "REAL",
                "matched_size": "REAL", "remaining_size": "REAL", "liquidity_role": "TEXT",
                "transaction_hash": "TEXT", "event_timestamp": "TEXT", "correlation_json": "TEXT",
            },
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
            "live_markets": {
                "yes_best_bid": "REAL", "yes_best_ask": "REAL",
                "no_best_bid": "REAL", "no_best_ask": "REAL",
                "market_timestamp": "TEXT", "market_received_at": "TEXT",
            },
            "live_rules": {
                "execution_mode": "TEXT NOT NULL DEFAULT 'READ_ONLY'",
                "last_evaluated_at": "TEXT", "last_decision": "TEXT", "last_reason": "TEXT",
                "max_yes_entries_per_event": "INTEGER NOT NULL DEFAULT 1",
                "max_no_entries_per_event": "INTEGER NOT NULL DEFAULT 1",
                "entry_window_start_seconds_before_end": "INTEGER",
                "entry_window_end_seconds_before_end": "INTEGER",
                "schedule_timezone": "TEXT NOT NULL DEFAULT 'Asia/Jerusalem'",
                "inactive_windows_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_demo_rule_id": "INTEGER",
                "source_rule_snapshot_json": "TEXT",
            },
            "live_deals": {
                "execution_mode": "TEXT NOT NULL DEFAULT 'READ_ONLY'",
                "price_source": "TEXT", "entry_snapshot_id": "INTEGER", "exit_snapshot_id": "INTEGER",
                "entry_reason": "TEXT", "gross_pnl_usd": "REAL NOT NULL DEFAULT 0",
                "net_pnl_usd": "REAL NOT NULL DEFAULT 0", "roi_percent": "REAL NOT NULL DEFAULT 0",
                "opened_at": "TEXT", "closed_at": "TEXT", "rule_snapshot_json": "TEXT",
                "paper_fill_status": "TEXT",
                "fee_rate": "REAL NOT NULL DEFAULT 0", "fee_source": "TEXT",
                "fee_version": "TEXT",
            },
        }
        with self.connect() as conn:
            for table, columns in additions.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, column_type in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            conn.commit()
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_paper_open_rule_event
                ON live_deals(live_rule_id, event_id)
                WHERE execution_mode = 'PAPER_TRADING'
                  AND status IN ('created','entry_pending','open','partially_open','exit_pending')
                """
            )
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
        if key == "kill_switch" and actor != "operator":
            raise PermissionError("kill_switch is operator-owned")
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

    def set_states_on_connection(
        self,
        conn: sqlite3.Connection,
        values: dict[str, str],
        actor: str = "system",
    ) -> None:
        """Write coalescible states using an existing transaction."""
        if not values:
            return
        if "kill_switch" in values and actor != "operator":
            raise PermissionError("kill_switch is operator-owned")

        ts = now_iso()

        for key, value in values.items():
            existing = conn.execute(
                "SELECT value FROM live_system_state WHERE key = ?",
                (key,),
            ).fetchone()

            if existing and str(existing["value"]) == str(value):
                continue

            conn.execute(
                """
                INSERT INTO live_system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, str(value), ts),
            )

            conn.execute(
                """
                INSERT INTO live_audit_log
                    (occurred_at, actor, action, status, reason, details_json)
                VALUES (?, ?, ?, 'ok', '', ?)
                """,
                (
                    ts,
                    actor,
                    f"set_{key}",
                    json_dumps({"value": str(value)}),
                ),
            )

    def set_states(self, values: dict[str, str], actor: str = "system") -> None:
        """Persist coalescible status values in one transaction."""
        if not values:
            return

        with self.connect() as conn:
            self.set_states_on_connection(
                conn,
                values,
                actor,
            )
            conn.commit()

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
                    max_exit_slippage, status, eligible_after_event_id, execution_mode,
                    max_yes_entries_per_event, max_no_entries_per_event,
                    entry_window_start_seconds_before_end,
                    entry_window_end_seconds_before_end, schedule_timezone,
                    inactive_windows_json, source_demo_rule_id, source_rule_snapshot_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload.get("execution_mode", "READ_ONLY"),
                    payload.get("max_yes_entries_per_event", 1),
                    payload.get("max_no_entries_per_event", 1),
                    payload.get("entry_window_start_seconds_before_end"),
                    payload.get("entry_window_end_seconds_before_end"),
                    payload.get("schedule_timezone", "Asia/Jerusalem"),
                    json_dumps(payload.get("inactive_windows") or []),
                    payload.get("source_demo_rule_id"),
                    json_dumps(payload.get("source_rule_snapshot") or {}),
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM live_rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
            conn.commit()
        self.audit("operator", "create_live_rule", "ok", details={"rule_id": row["id"]})
        return row_to_dict(row) or {}

    def market_ws_asset_ids(self) -> list[str]:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        current_event_start = (now_epoch // 300) * 300
        current_event_id = f"btc-updown-5m-{current_event_start}"

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT yes_token_id, no_token_id
                FROM live_markets
                WHERE event_id = ?
                  AND market_resolved = 0
                  AND accepting_orders = 1
                LIMIT 1
                """,
                (current_event_id,),
            ).fetchall()

        return list(dict.fromkeys(
            str(asset_id)
            for row in rows
            for asset_id in (row["yes_token_id"], row["no_token_id"])
            if asset_id
        ))

    def market_for_asset(self, asset_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM live_markets
                WHERE yes_token_id = ? OR no_token_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(asset_id), str(asset_id)),
            ).fetchone()
        return row_to_dict(row)

    def mark_market_resolved(
        self, condition_id: str, winning_asset_id: str | None, winning_outcome: str | None
    ) -> None:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_markets SET
                    market_resolved = 1, winning_asset_id = ?, winning_outcome = ?,
                    source = 'POLYMARKET_MARKET_WS', last_update_at = ?, updated_at = ?
                WHERE condition_id = ?
                """,
                (winning_asset_id, winning_outcome, ts, ts, str(condition_id)),
            )
            conn.commit()

    def store_market_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.store_market_snapshots([snapshot])
        return rows[0] if rows else None

    def store_market_snapshots_on_connection(
        self,
        conn: sqlite3.Connection,
        snapshots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Store snapshots using an existing SQLite transaction."""
        stored: list[dict[str, Any]] = []

        for snapshot in snapshots:
            raw = snapshot.get("raw_message") or snapshot
            message_hash = (
                snapshot.get("message_hash")
                or sha256_text(json_dumps(raw))
            )
            received_at = (
                snapshot.get("received_at")
                or now_iso()
            )

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO live_market_snapshots (
                        condition_id,event_id,asset_id,outcome,event_type,
                        best_bid,best_ask,best_bid_size,best_ask_size,
                        bids_json,asks_json,market_timestamp,received_at,
                        latency_ms,source,message_hash,raw_message
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot["condition_id"],
                        snapshot.get("event_id"),
                        snapshot["asset_id"],
                        snapshot.get("outcome"),
                        snapshot.get("event_type", "unknown"),
                        snapshot.get("best_bid"),
                        snapshot.get("best_ask"),
                        snapshot.get("best_bid_size"),
                        snapshot.get("best_ask_size"),
                        json_dumps(snapshot.get("bids") or []),
                        json_dumps(snapshot.get("asks") or []),
                        snapshot.get("market_timestamp"),
                        received_at,
                        snapshot.get("latency_ms"),
                        snapshot.get(
                            "source",
                            "POLYMARKET_MARKET_WS",
                        ),
                        message_hash,
                        json_dumps(raw),
                    ),
                )
            except sqlite3.IntegrityError:
                continue

            row = conn.execute(
                """
                SELECT *
                FROM live_market_snapshots
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()

            market = conn.execute(
                """
                SELECT yes_token_id, no_token_id
                FROM live_markets
                WHERE condition_id = ?
                """,
                (snapshot["condition_id"],),
            ).fetchone()

            if market:
                side_columns = (
                    ("yes_best_bid", "yes_best_ask")
                    if str(market["yes_token_id"])
                    == str(snapshot["asset_id"])
                    else ("no_best_bid", "no_best_ask")
                    if str(market["no_token_id"])
                    == str(snapshot["asset_id"])
                    else (None, None)
                )

                if side_columns[0]:
                    conn.execute(
                        f"""
                        UPDATE live_markets SET
                            {side_columns[0]} = ?,
                            {side_columns[1]} = ?,
                            best_bid = ?,
                            best_ask = ?,
                            orderbook_depth_json = ?,
                            source = 'POLYMARKET_MARKET_WS',
                            market_timestamp = ?,
                            market_received_at = ?,
                            last_update_at = ?,
                            updated_at = ?
                        WHERE condition_id = ?
                        """,
                        (
                            snapshot.get("best_bid"),
                            snapshot.get("best_ask"),
                            snapshot.get("best_bid"),
                            snapshot.get("best_ask"),
                            json_dumps({
                                "bids": snapshot.get("bids") or [],
                                "asks": snapshot.get("asks") or [],
                            }),
                            snapshot.get("market_timestamp"),
                            received_at,
                            received_at,
                            received_at,
                            snapshot["condition_id"],
                        ),
                    )

            converted = row_to_dict(row)

            if converted:
                stored.append(converted)

        return stored

    def store_market_snapshots(
        self,
        snapshots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Store a coalesced batch in one SQLite transaction."""
        if not snapshots:
            return []

        with self.connect() as conn:
            stored = self.store_market_snapshots_on_connection(
                conn,
                snapshots,
            )
            conn.commit()

        return stored

    def latest_market_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_market_snapshots WHERE asset_id = ? ORDER BY id DESC LIMIT 1",
                (str(asset_id),),
            ).fetchone()
        return row_to_dict(row)

    def active_paper_rules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM live_rules
                WHERE status = 'active' AND execution_mode = 'PAPER_TRADING'
                ORDER BY id ASC
                """
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def open_paper_deals(self, *, asset_id: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT d.*, r.stop_loss_price, r.take_profit_price
            FROM live_deals d
            JOIN live_rules r ON r.id = d.live_rule_id
            WHERE d.execution_mode = 'PAPER_TRADING'
              AND d.status IN ('open','partially_open','exit_pending')
        """
        params: tuple[Any, ...] = ()
        if asset_id is not None:
            query += " AND d.token_id = ?"
            params = (str(asset_id),)
        query += " ORDER BY d.id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def count_paper_entries(self, rule_id: int, event_id: str, outcome: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM live_deals
                WHERE live_rule_id = ? AND event_id = ? AND outcome = ?
                  AND execution_mode = 'PAPER_TRADING'
                """,
                (rule_id, event_id, outcome),
            ).fetchone()
        return int(row[0] if row else 0)

    def record_rule_evaluation(
        self, rule: dict[str, Any], snapshot: dict[str, Any], decision: str, reason: str
    ) -> dict[str, Any] | None:
        evaluated_at = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO live_rule_evaluations (
                        live_rule_id,market_snapshot_id,event_id,condition_id,asset_id,outcome,
                        decision,reason,observed_best_bid,observed_best_ask,entry_price,
                        evaluated_at,rule_snapshot_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rule["id"], snapshot["id"], snapshot.get("event_id"),
                        snapshot["condition_id"], snapshot["asset_id"], snapshot.get("outcome"),
                        decision, reason, snapshot.get("best_bid"), snapshot.get("best_ask"),
                        rule["entry_price"], evaluated_at, json_dumps(rule),
                    ),
                )
                conn.execute(
                    """
                    UPDATE live_rules
                    SET last_evaluated_at = ?, last_decision = ?, last_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (evaluated_at, decision, reason, evaluated_at, rule["id"]),
                )
                row = conn.execute(
                    "SELECT * FROM live_rule_evaluations WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                conn.commit()
            return row_to_dict(row)
        except sqlite3.IntegrityError:
            return None

    def create_paper_deal(
        self,
        rule: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        reason: str,
        fee_rate: Decimal = Decimal("0"),
    ) -> dict[str, Any] | None:
        amount = Decimal(str(rule.get("requested_amount_usd") or 1))
        price = Decimal(str(snapshot["best_ask"]))
        if price <= 0:
            return None
        size = amount / price
        entry_fee = (size * fee_rate * price * (Decimal("1") - price)).quantize(Decimal("0.00001"))
        ts = now_iso()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO live_deals (
                        live_rule_id,event_id,condition_id,token_id,outcome,side,status,
                        requested_amount_usd,requested_size,filled_size,remaining_size,
                        average_entry_fill_price,entry_status,trigger_price,realized_pnl_usd,
                        fees_usd,slippage_usd,created_at,updated_at,execution_mode,price_source,
                        entry_snapshot_id,entry_reason,gross_pnl_usd,net_pnl_usd,roi_percent,
                        opened_at,rule_snapshot_json,paper_fill_status,fee_rate,fee_source,fee_version
                    ) VALUES (?,?,?,?,?,'buy','open',?,?,?,?,?,'filled',?,0,?,0,?,?,
                              'PAPER_TRADING','POLYMARKET_MARKET_WS',?,?,0,0,0,?,?, 'full',?,
                              'SIMULATED_CRYPTO_DEFAULT','paper-fee-v1')
                    """,
                    (
                        rule["id"], snapshot.get("event_id"), snapshot["condition_id"],
                        snapshot["asset_id"], snapshot.get("outcome"),
                        float(amount), float(size), float(size), 0,
                        float(price), rule["entry_price"], float(entry_fee), ts, ts,
                        snapshot["id"], reason, ts, json_dumps(rule), float(fee_rate),
                    ),
                )
                row = conn.execute("SELECT * FROM live_deals WHERE id = ?", (cursor.lastrowid,)).fetchone()
                conn.commit()
            deal = row_to_dict(row)
            self.audit("paper_engine", "paper_deal_opened", "ok", reason, {
                "deal_id": deal["id"] if deal else None,
                "rule_id": rule["id"], "snapshot_id": snapshot["id"],
            })
            return deal
        except sqlite3.IntegrityError:
            return None

    def close_paper_deal(
        self,
        deal: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        reason: str,
        trigger_price: Decimal,
        exit_price: Decimal,
        fill_method: str,
    ) -> dict[str, Any]:
        entry_price = Decimal(str(deal["average_entry_fill_price"]))
        shares = Decimal(str(deal["filled_size"]))
        exit_value = shares * exit_price
        amount = Decimal(str(deal["requested_amount_usd"]))
        gross = exit_value - amount
        fee_rate = Decimal(str(deal.get("fee_rate") or 0))
        exit_fee = (
            shares * fee_rate * exit_price * (Decimal("1") - exit_price)
        ).quantize(Decimal("0.00001"))
        total_fees = Decimal(str(deal.get("fees_usd") or 0)) + exit_fee
        net = gross - total_fees - Decimal(str(deal.get("slippage_usd") or 0))
        roi = (net / amount * Decimal("100")) if amount else Decimal("0")
        ts = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_deals SET
                    status='closed', average_exit_fill_price=?, exit_status='filled',
                    requested_exit_price=?, realized_pnl_usd=?, gross_pnl_usd=?,
                    net_pnl_usd=?, roi_percent=?, fees_usd=?, exit_reason=?, exit_snapshot_id=?,
                    closed_at=?, updated_at=?
                WHERE id = ? AND execution_mode = 'PAPER_TRADING'
                """,
                (
                    float(exit_price), float(trigger_price), float(net), float(gross), float(net),
                    float(roi), float(total_fees), reason, snapshot["id"], ts, ts, deal["id"],
                ),
            )
            row = conn.execute("SELECT * FROM live_deals WHERE id = ?", (deal["id"],)).fetchone()
            conn.commit()
        result = row_to_dict(row) or {}
        self.audit("paper_engine", "paper_deal_closed", "ok", reason, {
            "deal_id": deal["id"],
            "snapshot_id": snapshot["id"],
            "trigger_price": float(trigger_price),
            "best_bid": snapshot.get("best_bid"),
            "execution_price": float(exit_price),
            "fill_method": fill_method,
            "net_pnl_usd": float(net),
        })
        return result

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

    def fail_deal(self, deal_id: int, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE live_deals SET status='failed',entry_status='blocked',exit_reason=?,updated_at=? WHERE id=?",
                (reason, now_iso(), deal_id),
            )
            conn.commit()

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
                conn.execute("""INSERT INTO live_websocket_events (
                    channel,event_type,condition_id,asset_id,polymarket_order_id,polymarket_trade_id,
                    message_hash,received_at,processed_at,status,raw_message,message_type,message_status,
                    outcome,side,price,original_size,matched_size,remaining_size,liquidity_role,
                    transaction_hash,event_timestamp,correlation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (channel,message.get("event_type"),message.get("condition_id"),message.get("asset_id"),
                     message.get("order_id"),message.get("trade_id"),message_hash,now_iso(),now_iso() if status=="processed" else None,
                     status,json_dumps(message),message.get("message_type"),message.get("message_status"),message.get("outcome"),
                     message.get("side"),message.get("price"),message.get("original_size"),message.get("matched_size"),
                     message.get("remaining_size"),message.get("liquidity_role"),message.get("transaction_hash"),
                     message.get("event_timestamp"),json_dumps(message.get("correlation") or {})))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def user_ws_condition_ids(self) -> list[str]:
        with self.connect() as conn:
            rows=conn.execute("SELECT condition_id FROM live_markets WHERE market_resolved=0 ORDER BY accepting_orders DESC,id DESC LIMIT 2").fetchall()
            old=conn.execute("SELECT DISTINCT condition_id FROM live_orders WHERE condition_id IS NOT NULL AND status NOT IN ('filled','cancelled','unmatched','failed')").fetchall()
        return list(dict.fromkeys(str(row[0]) for row in [*rows,*old] if row[0]))

    def outcome_for_asset(self, condition_id: str | None, asset_id: str | None) -> str | None:
        market=self.latest_market(condition_id) if condition_id and asset_id else None
        if not market: return None
        if str(market.get("yes_token_id"))==str(asset_id): return "YES"
        if str(market.get("no_token_id"))==str(asset_id): return "NO"
        return None

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
            "live_backups", "live_market_snapshots", "live_rule_evaluations",
            "live_event_states", "live_strategy_intents", "live_strategy_fills",
            "live_strategy_positions", "live_strategy_deals", "live_audit_timeline",
            "live_alerts", "live_archive_runs",
        }
        if table not in allowed:
            raise ValueError(table)
        order_col = "id"
        if table == "live_orders":
            order_col = "local_order_id"
        if table == "live_daily_limits":
            order_col = "day_key"
        if table == "live_backups":
            order_col = "id"
        if table == "live_event_states":
            order_col = "locked_at"
        if table == "live_strategy_intents":
            order_col = "created_at"
        if table == "live_strategy_fills":
            order_col = "created_at"
        if table == "live_strategy_positions":
            order_col = "created_at"
        if table == "live_strategy_deals":
            order_col = "created_at"
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "open_orders": int(conn.execute(
                    "SELECT COUNT(*) FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed','blocked')"
                ).fetchone()[0]),
                "open_deals": int(conn.execute(
                    "SELECT COUNT(*) FROM live_deals WHERE status IN ('created','entry_pending','open','partially_open','exit_pending')"
                ).fetchone()[0]),
                "markets": int(conn.execute("SELECT COUNT(*) FROM live_markets").fetchone()[0]),
                "rules": int(conn.execute("SELECT COUNT(*) FROM live_rules").fetchone()[0]),
                "active_rules": int(conn.execute("SELECT COUNT(*) FROM live_rules WHERE status = 'active'").fetchone()[0]),
                "paper_rules": int(conn.execute(
                    "SELECT COUNT(*) FROM live_rules WHERE execution_mode = 'PAPER_TRADING'"
                ).fetchone()[0]),
                "active_paper_rules": int(conn.execute(
                    "SELECT COUNT(*) FROM live_rules WHERE status = 'active' AND execution_mode = 'PAPER_TRADING'"
                ).fetchone()[0]),
                "open_paper_deals": int(conn.execute(
                    """
                    SELECT COUNT(*) FROM live_deals
                    WHERE execution_mode = 'PAPER_TRADING'
                      AND status IN ('open','partially_open','exit_pending')
                    """
                ).fetchone()[0]),
                "closed_paper_deals": int(conn.execute(
                    "SELECT COUNT(*) FROM live_deals WHERE execution_mode = 'PAPER_TRADING' AND status = 'closed'"
                ).fetchone()[0]),
                "paper_realized_pnl_usd": float(conn.execute(
                    """
                    SELECT COALESCE(SUM(net_pnl_usd), 0) FROM live_deals
                    WHERE execution_mode = 'PAPER_TRADING' AND status = 'closed'
                    """
                ).fetchone()[0]),
                "market_snapshots": int(conn.execute(
                    "SELECT COUNT(*) FROM live_market_snapshots"
                ).fetchone()[0]),
            }

    def revoke_all_sessions(self, actor: str = "operator") -> str:
        current = self.get_state("session_version", "1")
        try:
            next_version = str(int(current) + 1)
        except ValueError:
            next_version = "1"
        self.set_state("session_version", next_version, actor)
        self.audit(actor, "revoke_all_sessions", "ok")
        return next_version

    def maintenance_status(self) -> dict[str, Any]:
        mode = self.get_state("maintenance_mode", "RUNNING")
        requested_at = self.get_state("maintenance_requested_at", "")
        reason = self.get_state("maintenance_delay_reason", "")
        stop_ready = self.get_state("maintenance_stop_ready", "false").lower() == "true"
        counts = self.counts()
        exposure = self.current_exposure_usd()
        return {
            "mode": mode,
            "requested_at": requested_at,
            "phase": self.get_state("maintenance_phase", "running"),
            "stop_ready": stop_ready,
            "delay_reason": reason,
            "estimated_wait": self.get_state("maintenance_estimated_wait", "not scheduled"),
            "open_orders": counts["open_orders"],
            "open_deals": counts["open_deals"],
            "exposure_usd": str(exposure),
        }

    def request_maintenance_drain(self, actor: str = "operator") -> dict[str, Any]:
        ts = now_iso()
        with self.connect() as conn:
            for key, value in {
                "maintenance_mode": "DRAINING",
                "maintenance_requested_at": ts,
                "maintenance_phase": "draining_current_event",
                "maintenance_stop_ready": "false",
                "maintenance_delay_reason": "waiting for exposure/orders/deals to settle",
                "maintenance_estimated_wait": "after current BTC 5-minute event and final reconciliation",
            }.items():
                conn.execute(
                    """
                    INSERT INTO live_system_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, ts),
                )
            conn.commit()
        self.audit(actor, "maintenance_drain_requested", "ok")
        return self.refresh_maintenance_readiness(actor)

    def cancel_maintenance_drain(self, actor: str = "operator") -> dict[str, Any]:
        with self.connect() as conn:
            mode = self.get_state("maintenance_mode", "RUNNING")
            stop_ready = self.get_state("maintenance_stop_ready", "false").lower() == "true"
            if mode == "DRAINING" and not stop_ready:
                for key, value in {
                    "maintenance_mode": "RUNNING",
                    "maintenance_phase": "running",
                    "maintenance_stop_ready": "false",
                    "maintenance_delay_reason": "cancelled by admin before stop-ready",
                    "maintenance_estimated_wait": "not scheduled",
                }.items():
                    conn.execute(
                        """
                        INSERT INTO live_system_state (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (key, value, now_iso()),
                    )
                conn.commit()
                self.audit(actor, "maintenance_drain_cancelled", "ok")
            else:
                self.audit(actor, "maintenance_drain_cancelled", "blocked", "already stop-ready or not draining")
        return self.maintenance_status()

    def refresh_maintenance_readiness(self, actor: str = "system") -> dict[str, Any]:
        status = self.maintenance_status()
        exposure = Decimal(str(status["exposure_usd"]))
        ready = status["mode"] == "DRAINING" and exposure == 0 and status["open_orders"] == 0 and status["open_deals"] == 0
        phase = "stop_ready" if ready else ("waiting_for_positions_or_orders" if status["mode"] == "DRAINING" else "running")
        delay = "" if ready else "exposure, open orders, or open deals still exist"
        with self.connect() as conn:
            for key, value in {
                "maintenance_stop_ready": "true" if ready else "false",
                "maintenance_phase": phase,
                "maintenance_delay_reason": delay,
                "maintenance_estimated_wait": "ready for controlled service stop" if ready else status["estimated_wait"],
            }.items():
                conn.execute(
                    """
                    INSERT INTO live_system_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, now_iso()),
                )
            conn.commit()
        self.audit(actor, "maintenance_readiness_checked", "ok" if ready else "blocked", delay)
        return self.maintenance_status()

    def record_backup(self, path: str, status: str, size_bytes: int = 0, checksum: str = "", reason: str = "", error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_backups (created_at, finished_at, path, size_bytes, sha256, status, reason, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), now_iso(), path, size_bytes, checksum, status, reason, error),
            )
            conn.commit()
        self.audit("system", "backup_recorded", status, reason or error, {"path": path, "size_bytes": size_bytes})

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
