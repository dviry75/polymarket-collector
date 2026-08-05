from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import re
import sqlite3
import uuid
from typing import Any, Iterable

from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso, row_to_dict


FINAL_INTENT_STATES = {
    "FILLED", "PARTIAL_FINAL", "ZERO_FILL", "CANCELED", "REJECTED", "FAILED", "SETTLED", "REDEEMED"
}
OPEN_POSITION_STATES = {"OPEN", "TP_OPEN", "EXITING", "EXIT_RECONCILIATION_REQUIRED"}
SENSITIVE_KEYS = {
    "private_key", "apikey", "api_key", "api_secret", "secret", "passphrase",
    "signature", "authorization", "cookie", "operator_token", "session_secret",
    "csrf_token", "x_live_operator_token", "x_live_csrf_token",
}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS or lowered.endswith("_private_key"):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, str):
        return re.sub(
            r"(?i)(private[_ -]?key|api[_ -]?secret|passphrase|authorization|signature|operator[_ -]?token|session[_ -]?secret|cookie|csrf[_ -]?token)(\s*[=:]\s*|\s+)[^\s,;]+",
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            value,
        )
    return value


def stable_id(kind: str, identity: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"polymarket-live:{kind}:{identity}"))


class StrategyRepository:
    def __init__(self, base: LiveRepository):
        self.base = base

    def migrate(self, *, pause_entries_default: bool = True) -> None:
        with self.base.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_event_states (
                    event_id TEXT PRIMARY KEY,
                    condition_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    locked_side TEXT,
                    locked_token_id TEXT,
                    lock_reason TEXT NOT NULL,
                    entry_intent_id TEXT UNIQUE,
                    locked_at TEXT NOT NULL,
                    resolved_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_strategy_intents (
                    intent_id TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    position_id TEXT,
                    action TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    token_id TEXT,
                    side TEXT,
                    state TEXT NOT NULL,
                    order_type TEXT,
                    requested_amount_text TEXT,
                    requested_shares_text TEXT,
                    price_limit_text TEXT,
                    max_spend_text TEXT,
                    filled_shares_text TEXT NOT NULL DEFAULT '0',
                    average_price_text TEXT,
                    fee_text TEXT NOT NULL DEFAULT '0',
                    remaining_shares_text TEXT NOT NULL DEFAULT '0',
                    remote_order_id TEXT UNIQUE,
                    transaction_hash TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_book_hash TEXT,
                    reason_code TEXT,
                    normalized_error TEXT,
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    final_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES live_event_states(event_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_strategy_one_entry
                ON live_strategy_intents(event_id)
                WHERE action = 'ENTRY';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_strategy_one_active_exit
                ON live_strategy_intents(position_id)
                WHERE action = 'EXIT'
                  AND state NOT IN ('FILLED','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED');

                CREATE TABLE IF NOT EXISTS live_strategy_fills (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    remote_trade_id TEXT UNIQUE,
                    shares_text TEXT NOT NULL,
                    price_text TEXT NOT NULL,
                    fee_text TEXT NOT NULL DEFAULT '0',
                    status TEXT NOT NULL,
                    transaction_hash TEXT,
                    matched_at TEXT,
                    settled_at TEXT,
                    raw_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES live_strategy_intents(intent_id)
                );

                CREATE TABLE IF NOT EXISTS live_strategy_positions (
                    position_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    state TEXT NOT NULL,
                    acquired_shares_text TEXT NOT NULL,
                    remaining_shares_text TEXT NOT NULL,
                    sellable_shares_text TEXT NOT NULL,
                    dust_shares_text TEXT NOT NULL DEFAULT '0',
                    average_entry_price_text TEXT NOT NULL,
                    cost_all_in_text TEXT NOT NULL,
                    entry_fees_text TEXT NOT NULL DEFAULT '0',
                    exit_value_text TEXT NOT NULL DEFAULT '0',
                    exit_fees_text TEXT NOT NULL DEFAULT '0',
                    realized_pnl_text TEXT NOT NULL DEFAULT '0',
                    stop_stage INTEGER NOT NULL DEFAULT 0,
                    tp_intent_id TEXT,
                    active_exit_intent_id TEXT,
                    last_exit_book_hash TEXT,
                    redeem_intent_id TEXT,
                    resolved_winner INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS live_strategy_deals (
                    deal_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    position_id TEXT,
                    state TEXT NOT NULL,
                    outcome TEXT,
                    trigger_price_text TEXT,
                    entry_intent_id TEXT,
                    total_fees_text TEXT NOT NULL DEFAULT '0',
                    realized_pnl_text TEXT NOT NULL DEFAULT '0',
                    final_reason TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_audit_timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    category TEXT NOT NULL,
                    component TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_id TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    rule_id TEXT,
                    deal_id TEXT,
                    correlation_id TEXT,
                    intent_id TEXT,
                    order_id TEXT,
                    fill_id TEXT,
                    transaction_hash TEXT,
                    requested_action TEXT,
                    reason_code TEXT,
                    previous_state TEXT,
                    new_state TEXT,
                    result_status TEXT NOT NULL,
                    requested_amount_text TEXT,
                    requested_shares_text TEXT,
                    filled_shares_text TEXT,
                    average_price_text TEXT,
                    fees_text TEXT,
                    remaining_shares_text TEXT,
                    pnl_text TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_live_timeline_time ON live_audit_timeline(id DESC);
                CREATE INDEX IF NOT EXISTS idx_live_timeline_event ON live_audit_timeline(event_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_live_timeline_order ON live_audit_timeline(order_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_live_timeline_filters
                ON live_audit_timeline(severity, category, result_status, reason_code, id DESC);

                CREATE TABLE IF NOT EXISTS live_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    message TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(fingerprint, active)
                );

                CREATE TABLE IF NOT EXISTS live_archive_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_day TEXT NOT NULL,
                    object_name TEXT,
                    local_path TEXT,
                    manifest_path TEXT,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    compressed_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,
                    upload_generation TEXT,
                    readback_verified INTEGER NOT NULL DEFAULT 0,
                    local_rows_deleted INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_live_strategy_one_active_exit")
            conn.execute(
                """
                CREATE UNIQUE INDEX idx_live_strategy_one_active_exit
                ON live_strategy_intents(position_id)
                WHERE action = 'EXIT'
                  AND state NOT IN (
                    'FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED'
                  )
                """
            )
            defaults = {
                "pause_entries": "true" if pause_entries_default else "false",
                "canary_armed": "false",
                "canary_consumed": "false",
                "strategy_readiness": "NOT_READY",
                "strategy_block_reason": "STARTUP_RECONCILIATION_REQUIRED",
                "order_heartbeat_status": "DISABLED",
                "last_successful_reconciliation_at": "",
                "last_archive_at": "",
            }
            for key, value in defaults.items():
                conn.execute(
                    """
                    INSERT INTO live_system_state(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, value, now_iso()),
                )
            conn.commit()

    def pause_entries(self) -> bool:
        return self.base.get_state("pause_entries", "true").lower() == "true"

    def set_pause_entries(self, paused: bool, actor: str, reason: str) -> None:
        previous = self.pause_entries()
        self.base.set_state("pause_entries", "true" if paused else "false", actor)
        self.timeline(
            severity="WARNING" if paused else "INFO",
            category="OPERATOR",
            component="strategy",
            source=actor,
            requested_action="PAUSE_ENTRIES" if paused else "RESUME_ENTRIES",
            reason_code=reason,
            previous_state=str(previous).lower(),
            new_state=str(paused).lower(),
            result_status="ACK",
        )

    def reserve_event_entry(
        self,
        *,
        event_id: str,
        condition_id: str,
        token_id: str | None,
        side: str | None,
        simultaneous: bool,
        reason_code: str,
    ) -> dict[str, Any]:
        ts = now_iso()
        intent_id = stable_id("entry", event_id)
        correlation_id = stable_id("correlation", event_id)
        status = "SKIPPED_SIMULTANEOUS_TRIGGER" if simultaneous else "ENTRY_INTENT_RESERVED"
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return {**(row_to_dict(existing) or {}), "_duplicate": True}
            conn.execute(
                """
                INSERT INTO live_event_states(
                    event_id,condition_id,status,locked_side,locked_token_id,lock_reason,
                    entry_intent_id,locked_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id, condition_id, status, side, token_id, reason_code,
                    None if simultaneous else intent_id, ts, ts,
                ),
            )
            if not simultaneous:
                conn.execute(
                    """
                    INSERT INTO live_strategy_intents(
                        intent_id,correlation_id,event_id,condition_id,action,purpose,
                        token_id,side,state,order_type,requested_amount_text,
                        price_limit_text,max_spend_text,reason_code,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'FAK','5','0.76','5',?,?,?)
                    """,
                    (
                        intent_id, correlation_id, event_id, condition_id, "ENTRY", "ENTRY",
                        token_id, side, "RESERVED", reason_code, ts, ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO live_strategy_deals(
                        deal_id,event_id,state,outcome,trigger_price_text,entry_intent_id,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        stable_id("deal", event_id), event_id, "ENTRY_PENDING", side,
                        "0.74", intent_id, ts, ts,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id = ?", (event_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def consume_canary(self, actor: str = "strategy") -> None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, value in {
                "pause_entries": "true",
                "canary_armed": "false",
                "canary_consumed": "true",
            }.items():
                conn.execute(
                    """
                    INSERT INTO live_system_state(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                    """,
                    (key, value, ts),
                )
            conn.commit()
        self.timeline(
            severity="WARNING", category="CANARY", component="strategy", source=actor,
            requested_action="AUTO_DISARM", reason_code="FIRST_ENTRY_INTENT_RESERVED",
            new_state="PAUSED_DISARMED", result_status="ACK",
        )

    def lock_event_skip(
        self, *, event_id: str, condition_id: str, reason_code: str
    ) -> dict[str, Any]:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO live_event_states(
                        event_id,condition_id,status,lock_reason,locked_at,updated_at
                    ) VALUES(?,?,'SKIPPED',?,?,?)
                    """,
                    (event_id, condition_id, reason_code, ts, ts),
                )
            row = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id=?", (event_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def event_state(self, event_id: str) -> dict[str, Any] | None:
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id=?", (event_id,)
            ).fetchone()
        return row_to_dict(row)

    def update_intent(self, intent_id: str, **updates: Any) -> dict[str, Any]:
        allowed = {
            "position_id", "state", "requested_shares_text", "filled_shares_text",
            "average_price_text", "fee_text", "remaining_shares_text", "remote_order_id",
            "transaction_hash", "retry_count", "last_book_hash", "reason_code",
            "normalized_error", "submitted_at", "final_at",
        }
        clean = {key: value for key, value in updates.items() if key in allowed}
        clean["updated_at"] = now_iso()
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self.base.connect() as conn:
            conn.execute(
                f"UPDATE live_strategy_intents SET {assignments} WHERE intent_id=?",
                (*clean.values(), intent_id),
            )
            row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            conn.commit()
        if row is None:
            raise KeyError(intent_id)
        return row_to_dict(row) or {}

    def intent(self, intent_id: str) -> dict[str, Any] | None:
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return row_to_dict(row)

    def add_fill(
        self,
        *,
        intent_id: str,
        remote_trade_id: str | None,
        shares: Decimal,
        price: Decimal,
        fee: Decimal,
        status: str,
        transaction_hash: str | None = None,
        matched_at: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> bool:
        fill_id = stable_id("fill", remote_trade_id or f"{intent_id}:{shares}:{price}:{matched_at}")
        ts = now_iso()
        try:
            with self.base.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO live_strategy_fills(
                        fill_id,intent_id,remote_trade_id,shares_text,price_text,fee_text,
                        status,transaction_hash,matched_at,raw_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fill_id, intent_id, remote_trade_id, canonical_decimal(shares),
                        canonical_decimal(price), canonical_decimal(fee), status,
                        transaction_hash, matched_at, json.dumps(sanitize(raw or {}), sort_keys=True),
                        ts, ts,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def open_position(
        self,
        *,
        event_id: str,
        condition_id: str,
        token_id: str,
        outcome: str,
        shares: Decimal,
        average_price: Decimal,
        cost_all_in: Decimal,
        fees: Decimal,
        sellable_shares: Decimal | None = None,
        min_sellable: Decimal = Decimal("0"),
    ) -> dict[str, Any]:
        position_id = stable_id("position", event_id)
        deal_id = stable_id("deal", event_id)
        sellable = shares if sellable_shares is None else sellable_shares
        is_dust = shares > 0 and min_sellable > 0 and sellable < min_sellable
        if is_dust:
            sellable = Decimal("0")
        position_state = "DUST" if is_dust else "OPEN"
        dust = shares if is_dust else Decimal("0")
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO live_strategy_positions(
                    position_id,event_id,condition_id,token_id,outcome,state,
                    acquired_shares_text,remaining_shares_text,sellable_shares_text,
                    dust_shares_text,average_entry_price_text,cost_all_in_text,entry_fees_text,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(position_id) DO UPDATE SET
                    remaining_shares_text=excluded.remaining_shares_text,
                    sellable_shares_text=excluded.sellable_shares_text,
                    average_entry_price_text=excluded.average_entry_price_text,
                    cost_all_in_text=excluded.cost_all_in_text,
                    entry_fees_text=excluded.entry_fees_text,
                    dust_shares_text=excluded.dust_shares_text,
                    state=excluded.state,updated_at=excluded.updated_at
                """,
                (
                    position_id, event_id, condition_id, token_id, outcome, position_state,
                    canonical_decimal(shares), canonical_decimal(shares),
                    canonical_decimal(sellable), canonical_decimal(dust),
                    canonical_decimal(average_price), canonical_decimal(cost_all_in),
                    canonical_decimal(fees), ts, ts,
                ),
            )
            conn.execute(
                """
                UPDATE live_strategy_intents
                SET position_id=?,state='FILLED',filled_shares_text=?,
                    average_price_text=?,fee_text=?,remaining_shares_text='0',
                    final_at=?,updated_at=?
                WHERE intent_id=?
                """,
                (
                    position_id, canonical_decimal(shares), canonical_decimal(average_price),
                    canonical_decimal(fees), ts, ts, stable_id("entry", event_id),
                ),
            )
            conn.execute(
                """
                UPDATE live_strategy_deals SET
                    position_id=?,state=?,outcome=?,opened_at=?,updated_at=?
                WHERE deal_id=?
                """,
                (position_id, position_state, outcome, ts, ts, deal_id),
            )
            conn.execute(
                """
                UPDATE live_event_states SET status=?,updated_at=?
                WHERE event_id=?
                """,
                (position_state, ts, event_id),
            )
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def mark_zero_fill(self, event_id: str, reason: str) -> None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE live_strategy_intents
                SET state='ZERO_FILL',reason_code=?,final_at=?,updated_at=?
                WHERE intent_id=?
                """,
                (reason, ts, ts, stable_id("entry", event_id)),
            )
            conn.execute(
                "UPDATE live_event_states SET status='ENTRY_ZERO_FILL',updated_at=? WHERE event_id=?",
                (ts, event_id),
            )
            conn.execute(
                """
                UPDATE live_strategy_deals SET state='CLOSED',final_reason=?,
                    closed_at=?,updated_at=? WHERE event_id=?
                """,
                (reason, ts, ts, event_id),
            )
            conn.commit()

    def active_positions(self, token_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM live_strategy_positions WHERE state IN "
            "('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED')"
        )
        params: tuple[Any, ...] = ()
        if token_id is not None:
            query += " AND token_id=?"
            params = (token_id,)
        query += " ORDER BY created_at"
        with self.base.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def exposure(self) -> Decimal:
        total = Decimal("0")
        for position in self.active_positions():
            remaining = decimal_value(position["remaining_shares_text"]) or Decimal("0")
            acquired = decimal_value(position["acquired_shares_text"]) or Decimal("0")
            cost = decimal_value(position["cost_all_in_text"]) or Decimal("0")
            if remaining > 0 and acquired > 0:
                total += cost * remaining / acquired
        return total

    def reserve_position_intent(
        self,
        position: dict[str, Any],
        *,
        action: str,
        purpose: str,
        order_type: str,
        shares: Decimal,
        price_limit: Decimal,
        book_hash: str,
    ) -> dict[str, Any]:
        position_id = str(position["position_id"])
        identity = f"{position_id}:{purpose}:{position.get('stop_stage', 0)}:{book_hash}"
        intent_id = stable_id("intent", identity)
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """
                SELECT * FROM live_strategy_intents
                WHERE position_id=? AND action=?
                  AND state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED')
                ORDER BY created_at DESC LIMIT 1
                """,
                (position_id, action),
            ).fetchone()
            if active is not None:
                conn.rollback()
                return {**(row_to_dict(active) or {}), "_duplicate": True}
            conn.execute(
                """
                INSERT INTO live_strategy_intents(
                    intent_id,correlation_id,event_id,condition_id,position_id,action,purpose,
                    token_id,side,state,order_type,requested_shares_text,price_limit_text,
                    last_book_hash,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'RESERVED',?,?,?,?,?,?)
                """,
                (
                    intent_id, stable_id("correlation", identity), position["event_id"],
                    position["condition_id"], position_id, action, purpose,
                    position["token_id"], "SELL", order_type, canonical_decimal(shares),
                    canonical_decimal(price_limit), book_hash, ts, ts,
                ),
            )
            column = "tp_intent_id" if purpose == "TAKE_PROFIT" else "active_exit_intent_id"
            state = "TP_OPEN" if purpose == "TAKE_PROFIT" else "EXITING"
            conn.execute(
                f"UPDATE live_strategy_positions SET {column}=?,state=?,updated_at=? WHERE position_id=?",
                (intent_id, state, ts, position_id),
            )
            row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def cancel_tp(self, position_id: str, reason: str) -> dict[str, Any] | None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            position = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if position is None or not position["tp_intent_id"]:
                conn.rollback()
                return None
            intent_id = position["tp_intent_id"]
            conn.execute(
                """
                UPDATE live_strategy_intents SET state='CANCEL_REQUESTED',reason_code=?,
                    updated_at=? WHERE intent_id=?
                """,
                (reason, ts, intent_id),
            )
            row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(row)

    def finalize_cancel(self, intent_id: str, success: bool, reason: str) -> None:
        state = "CANCELED" if success else "CANCEL_UNCERTAIN"
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT position_id FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE live_strategy_intents SET state=?,reason_code=?,final_at=?,updated_at=?
                WHERE intent_id=?
                """,
                (state, reason, ts if success else None, ts, intent_id),
            )
            if row and row["position_id"]:
                conn.execute(
                    """
                    UPDATE live_strategy_positions SET tp_intent_id=NULL,
                        state=CASE WHEN ? THEN 'OPEN' ELSE 'EXIT_RECONCILIATION_REQUIRED' END,
                        updated_at=? WHERE position_id=?
                    """,
                    (1 if success else 0, ts, row["position_id"]),
                )
            conn.commit()

    def apply_exit_fill(
        self,
        *,
        position_id: str,
        intent_id: str,
        sold_shares: Decimal,
        average_price: Decimal,
        fees: Decimal,
        final_state: str,
        min_sellable: Decimal,
        purpose: str,
        book_hash: str,
    ) -> dict[str, Any]:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(position_id)
            remaining_before = decimal_value(row["remaining_shares_text"]) or Decimal("0")
            actual_sold = min(max(Decimal("0"), sold_shares), remaining_before)
            remaining = remaining_before - actual_sold
            exit_value_before = decimal_value(row["exit_value_text"]) or Decimal("0")
            exit_fees_before = decimal_value(row["exit_fees_text"]) or Decimal("0")
            exit_value = exit_value_before + actual_sold * average_price
            exit_fees = exit_fees_before + fees
            cost = decimal_value(row["cost_all_in_text"]) or Decimal("0")
            acquired = decimal_value(row["acquired_shares_text"]) or Decimal("0")
            allocated_cost = cost * (acquired - remaining) / acquired if acquired > 0 else Decimal("0")
            pnl = exit_value - exit_fees - allocated_cost
            dust = remaining if Decimal("0") < remaining < min_sellable else Decimal("0")
            if remaining == 0:
                position_state = "CLOSED"
            elif dust > 0:
                position_state = "DUST"
            elif final_state in {"UNKNOWN", "CANCEL_UNCERTAIN"}:
                position_state = "EXIT_RECONCILIATION_REQUIRED"
            elif purpose == "TAKE_PROFIT":
                position_state = "TP_OPEN"
            else:
                position_state = "OPEN"
            stop_stage = int(row["stop_stage"] or 0)
            if purpose == "STOP_066":
                stop_stage = max(stop_stage, 1)
            elif purpose in {"EMERGENCY_060", "EMERGENCY_OPERATOR"}:
                stop_stage = max(stop_stage, 2)
            conn.execute(
                """
                UPDATE live_strategy_positions SET
                    remaining_shares_text=?,sellable_shares_text=?,dust_shares_text=?,
                    exit_value_text=?,exit_fees_text=?,realized_pnl_text=?,state=?,
                    stop_stage=?,active_exit_intent_id=NULL,
                    tp_intent_id=CASE WHEN ?='TAKE_PROFIT' AND ?=1 THEN tp_intent_id
                                      WHEN ?='TAKE_PROFIT' THEN NULL ELSE tp_intent_id END,
                    last_exit_book_hash=?,
                    updated_at=?,closed_at=CASE WHEN ?='CLOSED' THEN ? ELSE closed_at END
                WHERE position_id=?
                """,
                (
                    canonical_decimal(remaining), canonical_decimal(max(Decimal("0"), remaining-dust)),
                    canonical_decimal(dust), canonical_decimal(exit_value),
                    canonical_decimal(exit_fees), canonical_decimal(pnl), position_state,
                    stop_stage, purpose, 1 if remaining > 0 else 0, purpose, book_hash,
                    ts, position_state, ts, position_id,
                ),
            )
            conn.execute(
                """
                UPDATE live_strategy_intents SET state=?,filled_shares_text=?,
                    average_price_text=?,fee_text=?,remaining_shares_text=?,
                    final_at=?,updated_at=? WHERE intent_id=?
                """,
                (
                    final_state, canonical_decimal(actual_sold), canonical_decimal(average_price),
                    canonical_decimal(fees), canonical_decimal(remaining),
                    ts if final_state in FINAL_INTENT_STATES else None, ts, intent_id,
                ),
            )
            if position_state in {"CLOSED", "DUST"}:
                deal_state = "CLOSED" if position_state == "CLOSED" else "DUST"
                conn.execute(
                    """
                    UPDATE live_strategy_deals SET state=?,total_fees_text=?,
                        realized_pnl_text=?,final_reason=?,closed_at=?,updated_at=?
                    WHERE event_id=?
                    """,
                    (
                        deal_state,
                        canonical_decimal(
                            (decimal_value(row["entry_fees_text"]) or Decimal("0")) + exit_fees
                        ),
                        canonical_decimal(pnl), purpose, ts, ts, row["event_id"],
                    ),
                )
                conn.execute(
                    "UPDATE live_event_states SET status=?,updated_at=? WHERE event_id=?",
                    (position_state, ts, row["event_id"]),
                )
            updated = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(updated) or {}

    def mark_position_resolved(
        self, position_id: str, *, winner: bool, redeem_pending: bool
    ) -> dict[str, Any]:
        ts = now_iso()
        state = "REDEEM_PENDING" if winner and redeem_pending else ("RESOLVED_WINNER" if winner else "RESOLVED_LOSER")
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(position_id)
            remaining = decimal_value(row["remaining_shares_text"]) or Decimal("0")
            value = remaining if winner else Decimal("0")
            cost = decimal_value(row["cost_all_in_text"]) or Decimal("0")
            pnl = (decimal_value(row["exit_value_text"]) or Decimal("0")) + value - cost
            conn.execute(
                """
                UPDATE live_strategy_positions SET state=?,resolved_winner=?,
                    realized_pnl_text=?,updated_at=? WHERE position_id=?
                """,
                (state, 1 if winner else 0, canonical_decimal(pnl), ts, position_id),
            )
            conn.execute(
                "UPDATE live_event_states SET status=?,resolved_at=?,updated_at=? WHERE event_id=?",
                (state, ts, ts, row["event_id"]),
            )
            updated = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(updated) or {}

    def unresolved_positions(self, condition_id: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM live_strategy_positions
            WHERE state NOT IN ('CLOSED','RESOLVED_LOSER','REDEEMED')
        """
        params: tuple[Any, ...] = ()
        if condition_id is not None:
            query += " AND condition_id=?"
            params = (condition_id,)
        query += " ORDER BY created_at"
        with self.base.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def mark_position_redeemed(self, position_id: str, transaction_hash: str) -> dict[str, Any]:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(position_id)
            conn.execute(
                """
                UPDATE live_strategy_positions SET state='REDEEMED',
                    remaining_shares_text='0',sellable_shares_text='0',dust_shares_text='0',
                    updated_at=?,closed_at=? WHERE position_id=?
                """,
                (ts, ts, position_id),
            )
            conn.execute(
                """
                UPDATE live_strategy_deals SET state='CLOSED',final_reason='REDEEMED',
                    realized_pnl_text=(SELECT realized_pnl_text FROM live_strategy_positions WHERE position_id=?),
                    closed_at=?,updated_at=? WHERE event_id=?
                """,
                (position_id, ts, ts, row["event_id"]),
            )
            conn.execute(
                "UPDATE live_event_states SET status='CLOSED',updated_at=? WHERE event_id=?",
                (ts, row["event_id"]),
            )
            updated = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            conn.commit()
        return row_to_dict(updated) or {}

    def intent_by_remote_order(self, remote_order_id: str) -> dict[str, Any] | None:
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE remote_order_id=?",
                (remote_order_id,),
            ).fetchone()
        return row_to_dict(row)

    def position_for_token(self, token_id: str) -> dict[str, Any] | None:
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE token_id=? ORDER BY created_at DESC LIMIT 1",
                (token_id,),
            ).fetchone()
        return row_to_dict(row)

    def reconcile_remote_position(
        self,
        *,
        event_id: str,
        condition_id: str,
        token_id: str,
        outcome: str,
        remote_shares: Decimal,
        average_price: Decimal,
        source: str = "account_reconciliation",
    ) -> tuple[dict[str, Any], bool]:
        """Apply positive remote account truth; returns (position, changed)."""
        ts = now_iso()
        position_id = stable_id("position", event_id)
        changed = False
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE token_id=? ORDER BY created_at DESC LIMIT 1",
                (token_id,),
            ).fetchone()
            if existing is None:
                changed = True
                conn.execute(
                    """
                    INSERT INTO live_event_states(
                        event_id,condition_id,status,locked_side,locked_token_id,lock_reason,
                        locked_at,updated_at
                    ) VALUES(?,?,'RECOVERED_REMOTE_POSITION',?,?,?, ?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        status='RECOVERED_REMOTE_POSITION',updated_at=excluded.updated_at
                    """,
                    (event_id, condition_id, outcome, token_id, source, ts, ts),
                )
                cost = remote_shares * average_price
                conn.execute(
                    """
                    INSERT INTO live_strategy_positions(
                        position_id,event_id,condition_id,token_id,outcome,state,
                        acquired_shares_text,remaining_shares_text,sellable_shares_text,
                        average_entry_price_text,cost_all_in_text,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'OPEN',?,?,?,?,?,?,?)
                    """,
                    (
                        position_id,event_id,condition_id,token_id,outcome,
                        canonical_decimal(remote_shares),canonical_decimal(remote_shares),
                        canonical_decimal(remote_shares),canonical_decimal(average_price),
                        canonical_decimal(cost),ts,ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO live_strategy_deals(
                        deal_id,event_id,position_id,state,outcome,total_fees_text,
                        realized_pnl_text,final_reason,opened_at,created_at,updated_at
                    ) VALUES(?,?,?,'OPEN',?,'0','0','RECOVERED_REMOTE_POSITION',?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        position_id=excluded.position_id,state='OPEN',updated_at=excluded.updated_at
                    """,
                    (stable_id("deal", event_id),event_id,position_id,outcome,ts,ts,ts),
                )
            else:
                position_id = str(existing["position_id"])
                local = decimal_value(existing["remaining_shares_text"]) or Decimal("0")
                if local != remote_shares:
                    changed = True
                    acquired = max(
                        decimal_value(existing["acquired_shares_text"]) or Decimal("0"),
                        remote_shares,
                    )
                    conn.execute(
                        """
                        UPDATE live_strategy_positions SET
                            acquired_shares_text=?,remaining_shares_text=?,sellable_shares_text=?,
                            dust_shares_text='0',average_entry_price_text=?,state='OPEN',updated_at=?
                        WHERE position_id=?
                        """,
                        (
                            canonical_decimal(acquired),canonical_decimal(remote_shares),
                            canonical_decimal(remote_shares),canonical_decimal(average_price),
                            ts,position_id,
                        ),
                    )
            row = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            conn.commit()
        if changed:
            self.timeline(
                severity="CRITICAL", category="RECONCILIATION", component="reconciliation",
                source=source, event_id=event_id, condition_id=condition_id,
                token_id=token_id, side=outcome, deal_id=stable_id("deal", event_id),
                requested_action="APPLY_REMOTE_POSITION_TRUTH",
                reason_code="REMOTE_POSITION_CORRECTION", result_status="CORRECTED",
                remaining_shares_text=canonical_decimal(remote_shares),
                average_price_text=canonical_decimal(average_price),
            )
        return row_to_dict(row) or {}, changed

    def set_reconciliation_state(self, *, ready: bool, reason: str, actor: str) -> None:
        self.base.set_state(
            "reconciliation_readiness", "READY" if ready else "NOT_READY", actor
        )
        self.base.set_state(
            "reconciliation_block_reason", "" if ready else reason, actor
        )
        self.base.set_state(
            "live_blocked_by_reconciliation", "false" if ready else "true", actor
        )
        if ready:
            self.base.set_state("last_successful_reconciliation_at", now_iso(), actor)
        else:
            self.set_pause_entries(True, actor, reason)

    def unresolved_intents(self) -> list[dict[str, Any]]:
        with self.base.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM live_strategy_intents
                WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED')
                ORDER BY created_at
                """
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def timeline(self, **event: Any) -> int:
        columns = [
            "occurred_at", "severity", "category", "component", "source",
            "event_id", "condition_id", "token_id", "side", "rule_id", "deal_id",
            "correlation_id", "intent_id", "order_id", "fill_id", "transaction_hash",
            "requested_action", "reason_code", "previous_state", "new_state",
            "result_status", "requested_amount_text", "requested_shares_text",
            "filled_shares_text", "average_price_text", "fees_text",
            "remaining_shares_text", "pnl_text", "retry_count", "parameters_json",
            "error_code", "error_message",
        ]
        safe = sanitize(event)
        defaults = {
            "occurred_at": now_iso(), "severity": "INFO", "category": "SYSTEM",
            "component": "strategy", "source": "system", "result_status": "INFO",
            "retry_count": 0, "parameters_json": "{}",
        }
        values = {**defaults, **safe}
        parameters = values.get("parameters_json")
        if not isinstance(parameters, str):
            values["parameters_json"] = json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True)
        placeholders = ",".join("?" for _ in columns)
        with self.base.connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO live_audit_timeline({','.join(columns)}) VALUES({placeholders})",
                tuple(values.get(column) for column in columns),
            )
            conn.commit()
        return int(cursor.lastrowid)

    def list_timeline(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        for key in (
            "severity", "category", "event_id", "side", "deal_id", "order_id",
            "result_status", "reason_code",
        ):
            value = (filters or {}).get(key)
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        from_time = (filters or {}).get("from_time")
        to_time = (filters or {}).get("to_time")
        if from_time:
            clauses.append("occurred_at >= ?")
            params.append(from_time)
        if to_time:
            clauses.append("occurred_at <= ?")
            params.append(to_time)
        search = (filters or {}).get("search")
        if search:
            clauses.append(
                "(reason_code LIKE ? OR error_message LIKE ? OR parameters_json LIKE ? OR intent_id LIKE ?)"
            )
            needle = f"%{search}%"
            params.extend([needle, needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.base.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM live_audit_timeline {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def alert(
        self,
        *,
        alert_type: str,
        severity: str,
        reason_code: str,
        message: str,
        entity_type: str = "",
        entity_id: str = "",
    ) -> int:
        fingerprint = stable_id(
            "alert", f"{alert_type}:{reason_code}:{entity_type}:{entity_id}"
        )
        ts = now_iso()
        with self.base.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM live_alerts WHERE fingerprint=? AND active=1", (fingerprint,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE live_alerts SET last_seen_at=?,occurrence_count=occurrence_count+1,
                        severity=?,message=? WHERE id=?
                    """,
                    (ts, severity, message, existing["id"]),
                )
                alert_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO live_alerts(
                        fingerprint,severity,alert_type,reason_code,entity_type,entity_id,
                        message,first_seen_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fingerprint, severity, alert_type, reason_code, entity_type,
                        entity_id, message, ts, ts,
                    ),
                )
                alert_id = int(cursor.lastrowid)
            conn.commit()
        return alert_id

    def acknowledge_alert(self, alert_id: int, actor: str) -> dict[str, Any]:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute(
                """
                UPDATE live_alerts SET active=0,acknowledged_at=?,acknowledged_by=?
                WHERE id=? AND active=1
                """,
                (ts, actor, alert_id),
            )
            row = conn.execute("SELECT * FROM live_alerts WHERE id=?", (alert_id,)).fetchone()
            conn.commit()
        if row is None:
            raise KeyError(alert_id)
        self.timeline(
            severity="INFO", category="ALERT", component="ui", source=actor,
            requested_action="ACKNOWLEDGE_ALERT", reason_code=str(row["reason_code"]),
            new_state="ACKNOWLEDGED", result_status="ACK",
            parameters_json={"alert_id": alert_id},
        )
        return row_to_dict(row) or {}

    def active_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.base.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM live_alerts WHERE active=1 ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def daily_pnl(self) -> Decimal:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.base.connect() as conn:
            rows = conn.execute(
                """
                SELECT realized_pnl_text FROM live_strategy_positions
                WHERE substr(COALESCE(closed_at, updated_at),1,10)=?
                """,
                (today,),
            ).fetchall()
        return sum(
            (decimal_value(row["realized_pnl_text"]) or Decimal("0") for row in rows),
            Decimal("0"),
        )

    def strategy_status(self) -> dict[str, Any]:
        positions = self.active_positions()
        with self.base.connect() as conn:
            event = conn.execute(
                "SELECT * FROM live_event_states ORDER BY locked_at DESC LIMIT 1"
            ).fetchone()
            alerts = conn.execute(
                "SELECT COUNT(*) FROM live_alerts WHERE active=1"
            ).fetchone()[0]
            archive = conn.execute(
                "SELECT * FROM live_archive_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "pause_entries": self.pause_entries(),
            "canary_armed": self.base.get_state("canary_armed", "false").lower() == "true",
            "canary_consumed": self.base.get_state("canary_consumed", "false").lower() == "true",
            "readiness": (
                "READY" if self.base.get_state("strategy_readiness", "NOT_READY") == "READY"
                and self.base.get_state("reconciliation_readiness", "NOT_READY") == "READY"
                else "NOT_READY"
            ),
            "block_reason": (
                self.base.get_state("strategy_block_reason", "UNKNOWN")
                if self.base.get_state("strategy_readiness", "NOT_READY") != "READY"
                else self.base.get_state("reconciliation_block_reason", "UNKNOWN")
            ),
            "market_data_readiness": self.base.get_state("strategy_readiness", "NOT_READY"),
            "reconciliation_readiness": self.base.get_state("reconciliation_readiness", "NOT_READY"),
            "event": row_to_dict(event),
            "positions": positions,
            "exposure_text": canonical_decimal(self.exposure()),
            "daily_pnl_text": canonical_decimal(self.daily_pnl()),
            "active_alerts": int(alerts),
            "heartbeat_status": self.base.get_state("order_heartbeat_status", "DISABLED"),
            "last_reconciliation": self.base.get_state("last_successful_reconciliation_at", ""),
            "last_archive": row_to_dict(archive),
        }
