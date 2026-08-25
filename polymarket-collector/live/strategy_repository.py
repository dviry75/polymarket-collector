from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import sqlite3
import uuid
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .order_book import canonical_decimal, decimal_value
from .recovery_policy import (
    GLOBAL_HARD_STOP_REASONS, UNKNOWN_OBSERVATION_WINDOW_SECONDS,
    IncidentScope, PauseState, ReleasePolicy, SafetyTier, is_known_reason,
    recovery_policy,
)
from .repository import LiveRepository, now_iso, row_to_dict


FINAL_INTENT_STATES = {
    "FILLED", "PARTIAL_FINAL", "ZERO_FILL", "CANCELED", "REJECTED", "FAILED", "SETTLED", "REDEEMED"
}
OPEN_POSITION_STATES = {"OPEN", "TP_OPEN", "EXITING", "EXIT_RECONCILIATION_REQUIRED"}
SENSITIVE_KEYS = {
    "private_key", "apikey", "api_key", "api_secret", "secret", "passphrase",
    "signature", "authorization", "cookie", "operator_token", "session_secret",
    "csrf_token", "x_live_operator_token", "x_live_csrf_token", "password",
    "credential", "credentials", "token_secret", "signing_secret", "mnemonic",
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
            r"(?i)(private[_ -]?key|api[_ -]?secret|passphrase|authorization|signature|operator[_ -]?token|session[_ -]?secret|cookie|csrf[_ -]?token|password|credentials?|token[_ -]?secret|signing[_ -]?secret|mnemonic)(\s*[=:]\s*|\s+)[^\s,;]+",
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            value,
        )
    return value


def stable_id(kind: str, identity: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"polymarket-live:{kind}:{identity}"))


class StrategyRepository:
    def __init__(self, base: LiveRepository):
        self.base = base
        self._inflight_submission_intents: set[str] = set()

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
                DROP INDEX IF EXISTS idx_live_strategy_one_entry;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_strategy_one_unresolved_entry
                ON live_strategy_intents(event_id)
                WHERE action = 'ENTRY'
                  AND state NOT IN (
                    'FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED',
                    'FAILED','SETTLED','REDEEMED'
                  );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_strategy_one_active_exit
                ON live_strategy_intents(position_id)
                WHERE action = 'EXIT'
                  AND state NOT IN ('FILLED','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED');

                CREATE TABLE IF NOT EXISTS live_order_attempts (
                    record_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    event_id TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    intent_id TEXT,
                    position_id TEXT,
                    deal_id TEXT,
                    operation TEXT NOT NULL,
                    purpose TEXT,
                    side TEXT,
                    order_type TEXT,
                    requested_price_text TEXT,
                    requested_size_text TEXT,
                    requested_amount_text TEXT,
                    max_price_text TEXT,
                    max_spend_text TEXT,
                    intent_state_before TEXT,
                    intent_state_after TEXT,
                    result_status TEXT NOT NULL,
                    success INTEGER,
                    remote_order_id TEXT,
                    transaction_hash TEXT,
                    exception_type TEXT,
                    exception_message TEXT,
                    error_code TEXT,
                    http_status INTEGER,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    normalized_json TEXT,
                    response_json TEXT,
                    error_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_live_order_attempts_attempt
                ON live_order_attempts(attempt_id, occurred_at, record_id);
                CREATE INDEX IF NOT EXISTS idx_live_order_attempts_event
                ON live_order_attempts(event_id, occurred_at, record_id);
                CREATE INDEX IF NOT EXISTS idx_live_order_attempts_intent
                ON live_order_attempts(intent_id, occurred_at, record_id);
                CREATE INDEX IF NOT EXISTS idx_live_order_attempts_remote
                ON live_order_attempts(remote_order_id, occurred_at, record_id);

                CREATE TABLE IF NOT EXISTS live_strategy_fills (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    remote_trade_id TEXT UNIQUE,
                    shares_text TEXT NOT NULL,
                    price_text TEXT NOT NULL,
                    fee_text TEXT NOT NULL DEFAULT '0',
                    fee_verification_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    fee_source TEXT,
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

                CREATE TABLE IF NOT EXISTS live_quarantines (
                    quarantine_id TEXT PRIMARY KEY,
                    incident_scope TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    position_id TEXT,
                    token_id TEXT,
                    event_id TEXT,
                    condition_id TEXT,
                    reason_code TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    operator_action_required INTEGER NOT NULL DEFAULT 0,
                    global_entry_halt_required INTEGER NOT NULL DEFAULT 0,
                    before_state TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_reason TEXT,
                    occurrence_count INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_quarantine_open
                ON live_quarantines(
                    incident_scope,entity_type,entity_id,reason_code
                ) WHERE status='OPEN';
                CREATE INDEX IF NOT EXISTS idx_live_quarantine_position
                ON live_quarantines(position_id,status,last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_live_quarantine_status
                ON live_quarantines(status,last_seen_at);

                CREATE TABLE IF NOT EXISTS live_strategy_deals (
                    deal_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    position_id TEXT,
                    state TEXT NOT NULL,
                    outcome TEXT,
                    trigger_price_text TEXT,
                    entry_intent_id TEXT,
                    total_fees_text TEXT NOT NULL DEFAULT '0',
                    fee_verification_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    fee_source TEXT,
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
                "pause_owner": "STARTUP" if pause_entries_default else "NONE",
                "pause_reason": "CONFIGURED_STARTUP_PAUSE" if pause_entries_default else "",
                "pause_auto_recoverable": "false",
                "pause_last_release_reason": "",
                "canary_armed": "false",
                "canary_consumed": "false",
                "strategy_readiness": "NOT_READY",
                "strategy_block_reason": "STARTUP_RECONCILIATION_REQUIRED",
                "order_heartbeat_status": "DISABLED",
                "last_successful_reconciliation_at": "",
                "last_archive_at": "",
                "operator_action_required": "false",
                "operator_action_reason": "",
                "global_entry_halt_required": "false",
                "global_entry_halt_reason": "",
                "incident_scope": "UNKNOWN",
                "quarantined_positions_count": "0",
                "quarantine_last_at": "",
                "auto_repair_last_at": "",
                "auto_repair_count_24h": "0",
                "unknown_cause_first_seen_at": "",
                "unknown_cause_reason": "",
            }
            for key, value in defaults.items():
                conn.execute(
                    """
                    INSERT INTO live_system_state(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, value, now_iso()),
                )
            pause_rows = conn.execute(
                "SELECT key,value,updated_at FROM live_system_state "
                "WHERE key IN ('pause_entries','pause_owner','pause_reason')"
            ).fetchall()
            pause_legacy = {
                str(row["key"]): {
                    "value": str(row["value"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in pause_rows
            }
            is_paused = (
                pause_legacy.get("pause_entries", {}).get("value", "true").lower()
                == "true"
            )
            legacy_reason = pause_legacy.get("pause_reason", {}).get("value", "")
            legacy_policy = recovery_policy(legacy_reason)
            legacy_acquired_at = pause_legacy.get(
                "pause_entries", {}
            ).get("updated_at", now_iso())
            recovery_defaults = {
                "pause_state": (
                    PauseState.PAUSED_MANUAL_ONLY
                    if is_paused
                    and legacy_policy.release_policy == ReleasePolicy.MANUAL_ONLY
                    else PauseState.PAUSED_RECOVERING
                    if is_paused
                    else PauseState.TRADING
                ),
                "pause_cause": legacy_reason,
                "release_policy": legacy_policy.release_policy,
                "pause_generation": "1" if is_paused else "0",
                "pause_acquired_at": legacy_acquired_at if is_paused else "",
                "pause_updated_at": legacy_acquired_at if is_paused else "",
                "pause_eligible_since": "",
                "pause_last_recovery_attempt_at": "",
                "pause_last_release_at": "",
                "recovery_attempt_count": "0",
                "recovery_status": "PAUSED" if is_paused else "HEALTHY",
                "recovery_engine_status": "STARTING",
                "recovery_blockers_json": "[]",
                "recovery_stability_elapsed_ms": "0",
                "recovery_last_action": "",
                "recovery_last_result": "",
                "recovery_financial_verified_generation": "",
                "last_successful_heartbeat_at": "",
                "last_auto_recovery_at": "",
            }
            for key, value in recovery_defaults.items():
                conn.execute(
                    "INSERT INTO live_system_state(key,value,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                    (key, str(value), now_iso()),
                )
            bug_rows = conn.execute(
                "SELECT key,value FROM live_system_state WHERE key IN "
                "('pause_entries','pause_cause','release_policy',"
                "'pause_generation','recovery_attempt_count')"
            ).fetchall()
            bug_state = {
                str(row["key"]): str(row["value"]) for row in bug_rows
            }
            if (
                bug_state.get("pause_entries", "false").lower() == "true"
                and bug_state.get("pause_cause") == "STRATEGY_NOT_READY"
                and bug_state.get("release_policy")
                == ReleasePolicy.MANUAL_ONLY
            ):
                repaired_at = now_iso()
                generation = int(
                    bug_state.get("pause_generation", "0") or 0
                ) + 1
                attempts = int(
                    bug_state.get("recovery_attempt_count", "0") or 0
                ) + 1
                self.base.set_states_on_connection(
                    conn,
                    {
                        "pause_cause": "MARKET_DATA_NOT_READY",
                        "pause_reason": "MARKET_DATA_NOT_READY",
                        "release_policy": ReleasePolicy.AUTO_WHEN_CLEAN,
                        "pause_state": PauseState.PAUSED_RECOVERING,
                        "pause_generation": str(generation),
                        "pause_acquired_at": repaired_at,
                        "pause_updated_at": repaired_at,
                        "pause_eligible_since": "",
                        "pause_auto_recoverable": "true",
                        "recovery_attempt_count": str(attempts),
                        "recovery_status": "RECOVERING",
                        "recovery_blockers_json": "[]",
                        "recovery_last_action": (
                            "MIGRATE_STRATEGY_READINESS_CLASSIFICATION"
                        ),
                        "recovery_last_result": "RECLASSIFIED_TRANSIENT",
                    },
                    "migration",
                )
                conn.execute(
                    "INSERT INTO live_audit_log "
                    "(occurred_at,actor,action,status,reason,details_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        repaired_at, "migration",
                        "pause_policy_reclassified", "ok",
                        "STRATEGY_NOT_READY",
                        json.dumps(
                            {
                                "new_reason": "MARKET_DATA_NOT_READY",
                                "generation": generation,
                                "release_policy": (
                                    ReleasePolicy.AUTO_WHEN_CLEAN
                                ),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            conn.commit()

    def repair_terminal_dust_slots(
        self, *, actor: str = "startup_reconciliation"
    ) -> list[dict[str, Any]]:
        """Close legacy post-exit dust without hiding unresolved exposure.

        A DUST position is eligible only when the matching deal already has a
        durable terminal timestamp, no shares are sellable, and every intent
        attached to the position is final. Entry-created dust deliberately has
        no terminal deal timestamp and therefore remains fail-closed.
        """
        repaired: list[dict[str, Any]] = []
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT
                    p.position_id,
                    p.event_id,
                    p.condition_id,
                    p.token_id,
                    p.outcome,
                    p.remaining_shares_text,
                    p.sellable_shares_text,
                    d.closed_at AS terminal_closed_at
                FROM live_strategy_positions AS p
                JOIN live_strategy_deals AS d
                  ON d.event_id = p.event_id
                WHERE p.state = 'DUST'
                  AND p.closed_at IS NULL
                  AND CAST(COALESCE(p.sellable_shares_text, '0') AS REAL) <= 0
                  AND d.state = 'DUST'
                  AND d.closed_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM live_strategy_intents AS i
                      WHERE i.position_id = p.position_id
                        AND i.state NOT IN (
                            'FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED',
                            'REJECTED','FAILED','SETTLED','REDEEMED'
                        )
                  )
                ORDER BY p.created_at
                """
            ).fetchall()
            for row in rows:
                closed_at = str(row["terminal_closed_at"])
                cursor = conn.execute(
                    """
                    UPDATE live_strategy_positions
                    SET closed_at = ?, updated_at = ?
                    WHERE position_id = ?
                      AND state = 'DUST'
                      AND closed_at IS NULL
                      AND CAST(COALESCE(sellable_shares_text, '0') AS REAL) <= 0
                    """,
                    (closed_at, now_iso(), str(row["position_id"])),
                )
                if cursor.rowcount:
                    repaired.append(row_to_dict(row) or {})
            conn.commit()

        for row in repaired:
            self.timeline(
                severity="WARNING",
                category="RECONCILIATION",
                component="strategy_state_repair",
                source=actor,
                event_id=str(row.get("event_id") or ""),
                condition_id=str(row.get("condition_id") or ""),
                token_id=str(row.get("token_id") or ""),
                side=str(row.get("outcome") or ""),
                deal_id=stable_id("deal", str(row.get("event_id") or "")),
                requested_action="CLOSE_TERMINAL_DUST_SLOT",
                reason_code="TERMINAL_DUST_CLOSED_AT_REPAIRED",
                previous_state="DUST_OPEN_SLOT",
                new_state="DUST_CLOSED_SLOT",
                result_status="REPAIRED",
                remaining_shares_text=str(
                    row.get("remaining_shares_text") or "0"
                ),
                parameters_json={
                    "position_id": row.get("position_id"),
                    "sellable_shares_text": row.get("sellable_shares_text"),
                    "closed_at": row.get("terminal_closed_at"),
                },
            )
        if repaired:
            self.base.audit(
                actor,
                "repair_terminal_dust_slots",
                "ok",
                details={"repaired": len(repaired)},
            )
        return repaired

    def hot_state_snapshot(self) -> dict[str, Any]:
        """Load latency-sensitive strategy state using one SQLite connection.

        This is intended for periodic background refresh. Market-data frames
        must consume the returned RAM snapshot rather than opening SQLite
        connections for pause/kill/event-lock/exposure checks.
        """
        state_keys = (
            "pause_entries",
            "kill_switch",
            "canary_armed",
            "canary_consumed",
            "reconciliation_readiness",
        )

        with self.base.connect() as conn:
            placeholders = ",".join("?" for _ in state_keys)

            state_rows = conn.execute(
                f"""
                SELECT key,value
                FROM live_system_state
                WHERE key IN ({placeholders})
                """,
                state_keys,
            ).fetchall()

            # Only recent event locks are required by the live 5-minute
            # strategy. This avoids growing an unbounded RAM set over time.
            event_rows = conn.execute(
                """
                SELECT event_id
                FROM live_event_states
                WHERE status != 'ENTRY_ZERO_FILL'
                ORDER BY locked_at DESC
                LIMIT 64
                """
            ).fetchall()

            position_rows = conn.execute(
                """
                SELECT
                    p.*,
                    tp.state AS tp_intent_state,
                    active_i.state AS active_exit_intent_state
                FROM live_strategy_positions AS p
                LEFT JOIN live_strategy_intents AS tp
                    ON tp.intent_id = p.tp_intent_id
                LEFT JOIN live_strategy_intents AS active_i
                    ON active_i.intent_id = p.active_exit_intent_id
                WHERE p.state IN (
                    'OPEN',
                    'TP_OPEN',
                    'EXITING',
                    'EXIT_RECONCILIATION_REQUIRED'
                )
                ORDER BY p.created_at
                """
            ).fetchall()

        states = {
            str(row["key"]): str(row["value"])
            for row in state_rows
        }

        exposure = Decimal("0")
        positions_by_token: dict[
            str, list[dict[str, Any]]
        ] = {}

        for row in position_rows:
            position = row_to_dict(row) or {}

            token_id = str(
                position.get("token_id") or ""
            )

            if token_id:
                positions_by_token.setdefault(
                    token_id, []
                ).append(position)

            remaining = (
                decimal_value(
                    position.get("remaining_shares_text")
                )
                or Decimal("0")
            )
            acquired = (
                decimal_value(
                    position.get("acquired_shares_text")
                )
                or Decimal("0")
            )
            cost = (
                decimal_value(
                    position.get("cost_all_in_text")
                )
                or Decimal("0")
            )

            if remaining > 0 and acquired > 0:
                exposure += cost * remaining / acquired

        return {
            "pause_entries": (
                states.get("pause_entries", "true").lower() == "true"
            ),
            "kill_switch": (
                states.get("kill_switch", "true").lower() == "true"
            ),
            "canary_armed": (
                states.get("canary_armed", "false").lower() == "true"
            ),
            "canary_consumed": (
                states.get("canary_consumed", "false").lower() == "true"
            ),
            "reconciliation_readiness": states.get(
                "reconciliation_readiness",
                "NOT_READY",
            ),
            "locked_event_ids": {
                str(row["event_id"])
                for row in event_rows
            },
            "active_exposure": exposure,
            "positions_by_token": positions_by_token,
            "loaded_at": now_iso(),
        }

    PAUSE_STATE_KEYS = (
        "pause_entries", "pause_state", "pause_owner", "pause_cause",
        "pause_reason", "release_policy", "pause_generation",
        "pause_acquired_at", "pause_updated_at", "pause_eligible_since",
        "pause_last_recovery_attempt_at", "pause_last_release_at",
        "pause_last_release_reason", "pause_source_reconciliation_run_id",
        "pause_source_event_id", "pause_source_order_id",
        "pause_source_position_id", "pause_auto_recoverable",
        "recovery_financial_verified_generation", "recovery_blockers_json",
        "recovery_attempt_count", "operator_action_required",
        "operator_action_reason", "global_entry_halt_required",
        "global_entry_halt_reason", "incident_scope",
        "unknown_cause_first_seen_at", "unknown_cause_reason",
    )

    def pause_entries(self) -> bool:
        return self.base.get_state("pause_entries", "true").lower() == "true"

    @staticmethod
    def _state_map_on_connection(
        conn: sqlite3.Connection, keys: Iterable[str]
    ) -> dict[str, str]:
        key_list = tuple(keys)
        placeholders = ",".join("?" for _ in key_list)
        rows = conn.execute(
            f"SELECT key,value FROM live_system_state WHERE key IN ({placeholders})",
            key_list,
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def pause_record(self) -> dict[str, Any]:
        with self.base.connect() as conn:
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
        return {
            **state,
            "pause_entries": state.get("pause_entries", "true").lower() == "true",
            "pause_generation": int(state.get("pause_generation", "0") or 0),
        }

    def escalate_unknown_pause_if_expired(
        self,
        *,
        actor: str,
        timeout_seconds: float = UNKNOWN_OBSERVATION_WINDOW_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        observed_now = now or datetime.now(timezone.utc)
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            reason = str(state.get("pause_cause") or "").upper()
            if (
                state.get("pause_entries", "false").lower() != "true"
                or not reason
                or is_known_reason(reason)
                or state.get("release_policy") == ReleasePolicy.MANUAL_ONLY
            ):
                conn.rollback()
                return False
            raw_first = (
                state.get("unknown_cause_first_seen_at")
                or state.get("pause_acquired_at")
            )
            try:
                first = datetime.fromisoformat(
                    str(raw_first).replace("Z", "+00:00")
                )
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                first = observed_now
            if (observed_now - first).total_seconds() < float(timeout_seconds):
                conn.rollback()
                return False
            ts = observed_now.astimezone(timezone.utc).isoformat()
            generation = int(state.get("pause_generation", "0") or 0) + 1
            self.base.set_states_on_connection(conn, {
                "pause_state": PauseState.PAUSED_MANUAL_ONLY,
                "release_policy": ReleasePolicy.MANUAL_ONLY,
                "pause_generation": str(generation),
                "pause_updated_at": ts,
                "recovery_status": "MANUAL_ONLY",
                "recovery_last_action": "OPERATOR_REVIEW_UNKNOWN_CAUSE",
                "recovery_last_result": "UNKNOWN_CLASSIFICATION_TIMEOUT",
                "operator_action_required": "true",
                "operator_action_reason": reason,
                "global_entry_halt_required": "true",
                "global_entry_halt_reason": reason,
                "incident_scope": IncidentScope.UNKNOWN,
            }, actor)
            conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    ts, actor, "unknown_cause_escalated", "blocked", reason,
                    json.dumps({
                        "generation": generation,
                        "incident_scope": IncidentScope.UNKNOWN,
                        "safety_tier": SafetyTier.GLOBAL_MANUAL_HARD_STOP,
                        "timeout_seconds": float(timeout_seconds),
                    }, sort_keys=True),
                ),
            )
            conn.commit()
        return True

    def reclassify_pause_as_scoped(
        self,
        *,
        actor: str,
        incident_scope: str,
        source_position_id: str | None,
        operator_action_required: bool,
        reason: str,
    ) -> bool:
        scope = str(incident_scope).upper()
        if scope not in {
            IncidentScope.POSITION,
            IncidentScope.TOKEN,
            IncidentScope.EVENT,
        }:
            raise ValueError("scope is not safely isolatable")
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            if state.get("pause_entries", "false").lower() != "true":
                conn.rollback()
                return False
            generation = int(state.get("pause_generation", "0") or 0) + 1
            self.base.set_states_on_connection(conn, {
                "pause_state": PauseState.PAUSED_RECOVERING,
                "pause_cause": "RECONCILIATION_GAP",
                "pause_reason": "RECONCILIATION_GAP",
                "release_policy": ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION,
                "pause_generation": str(generation),
                "pause_updated_at": ts,
                "pause_eligible_since": "",
                "pause_auto_recoverable": "false",
                "recovery_financial_verified_generation": "",
                "pause_source_position_id": str(source_position_id or ""),
                "recovery_status": "RECOVERING",
                "recovery_last_action": "SCOPED_REPAIR_VERIFY",
                "recovery_last_result": str(reason),
                "operator_action_required": (
                    "true" if operator_action_required else "false"
                ),
                "operator_action_reason": (
                    str(reason) if operator_action_required else ""
                ),
                "global_entry_halt_required": "false",
                "global_entry_halt_reason": "",
                "incident_scope": scope,
                "unknown_cause_first_seen_at": "",
                "unknown_cause_reason": "",
            }, actor)
            conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    ts, actor, "pause_reclassified_scoped", "ok", reason,
                    json.dumps({
                        "generation": generation,
                        "incident_scope": scope,
                        "source_position_id": source_position_id,
                        "operator_action_required": operator_action_required,
                        "safety_tier": SafetyTier.SCOPED_QUARANTINE,
                    }, sort_keys=True),
                ),
            )
            conn.commit()
        return True

    def acquire_pause(
        self,
        *,
        actor: str,
        reason: str,
        owner: str | None = None,
        source_reconciliation_run_id: str | int | None = None,
        source_event_id: str | None = None,
        source_order_id: str | None = None,
        source_position_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        reason = str(reason or "UNKNOWN").upper()
        resolved_owner = (
            owner or ("OPERATOR" if actor == "operator" else "MACHINE")
        ).upper()
        policy = recovery_policy(reason)
        now = now_iso()

        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            previous = current.get("pause_entries", "true").lower() == "true"
            current_owner = current.get("pause_owner", "NONE").upper()
            current_reason = current.get(
                "pause_cause", current.get("pause_reason", "")
            ).upper()
            try:
                current_policy = ReleasePolicy(
                    current.get(
                        "release_policy",
                        recovery_policy(current_reason).release_policy,
                    )
                )
            except ValueError:
                current_policy = ReleasePolicy.MANUAL_ONLY
            current_generation = int(
                current.get("pause_generation", "0") or 0
            )
            recovery_attempts = int(
                current.get("recovery_attempt_count", "0") or 0
            )

            if previous:
                if (
                    current_owner in {"OPERATOR", "STARTUP"}
                    and resolved_owner not in {"OPERATOR", "STARTUP"}
                ):
                    conn.rollback()
                    return self.pause_record(), False
                if current_reason == reason and current_owner == resolved_owner:
                    conn.rollback()
                    return self.pause_record(), False

                rank = {
                    ReleasePolicy.AUTO_WHEN_CLEAN: 1,
                    ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION: 2,
                    ReleasePolicy.MANUAL_ONLY: 3,
                }
                if (
                    resolved_owner not in {"OPERATOR", "STARTUP"}
                    and rank[policy.release_policy] <= rank[current_policy]
                ):
                    conn.rollback()
                    return self.pause_record(), False

            generation = current_generation + 1
            pause_state = (
                PauseState.PAUSED_MANUAL_ONLY
                if policy.release_policy == ReleasePolicy.MANUAL_ONLY
                else PauseState.PAUSED_RECOVERING
            )
            values = {
                "pause_entries": "true",
                "pause_state": pause_state,
                "pause_owner": resolved_owner,
                "pause_cause": reason,
                "pause_reason": reason,
                "release_policy": policy.release_policy,
                "pause_generation": str(generation),
                "pause_acquired_at": now,
                "pause_updated_at": now,
                "pause_eligible_since": "",
                "pause_last_recovery_attempt_at": now,
                "recovery_attempt_count": str(recovery_attempts + 1),
                "pause_auto_recoverable": (
                    "true"
                    if policy.release_policy == ReleasePolicy.AUTO_WHEN_CLEAN
                    else "false"
                ),
                "recovery_financial_verified_generation": "",
                "pause_source_reconciliation_run_id": str(
                    source_reconciliation_run_id or ""
                ),
                "pause_source_event_id": str(source_event_id or ""),
                "pause_source_order_id": str(source_order_id or ""),
                "pause_source_position_id": str(source_position_id or ""),
                "recovery_status": (
                    "MANUAL_ONLY"
                    if policy.release_policy == ReleasePolicy.MANUAL_ONLY
                    else "RECOVERING"
                ),
                "recovery_stability_elapsed_ms": "0",
                "recovery_last_action": policy.remediation,
                "recovery_last_result": "PAUSE_ACQUIRED",
                "operator_action_required": (
                    "true"
                    if policy.safety_tier == SafetyTier.GLOBAL_MANUAL_HARD_STOP
                    else "false"
                ),
                "operator_action_reason": (
                    reason
                    if policy.safety_tier == SafetyTier.GLOBAL_MANUAL_HARD_STOP
                    else ""
                ),
                "global_entry_halt_required": "true",
                "global_entry_halt_reason": reason,
                "incident_scope": "UNKNOWN",
                "unknown_cause_first_seen_at": (
                    now if not is_known_reason(reason) else ""
                ),
                "unknown_cause_reason": (
                    reason if not is_known_reason(reason) else ""
                ),
            }
            self.base.set_states_on_connection(conn, values, actor)
            conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    now, actor, "pause_acquired", "ok", reason,
                    json.dumps(
                        {
                            "generation": generation,
                            "owner": resolved_owner,
                            "state": pause_state,
                            "classification": policy.classification,
                            "release_policy": policy.release_policy,
                            "remediation": policy.remediation,
                            "safety_tier": policy.safety_tier,
                            "incident_scope": "UNKNOWN",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()

        self.timeline(
            severity="WARNING",
            category=(
                "OPERATOR"
                if resolved_owner in {"OPERATOR", "STARTUP"}
                else "SAFETY"
            ),
            component="recovery",
            source=actor,
            requested_action="PAUSE_ENTRIES",
            reason_code=reason,
            previous_state=str(previous).lower(),
            new_state=pause_state,
            result_status="ACQUIRED",
            parameters_json={
                "generation": generation,
                "classification": policy.classification,
                "release_policy": policy.release_policy,
                "required_evidence": policy.required_evidence,
                "remediation": policy.remediation,
                "safety_tier": policy.safety_tier,
                "incident_scope": "UNKNOWN",
            },
        )
        return self.pause_record(), True

    def promote_repairable_pause(
        self,
        *,
        actor: str,
        reconciliation_run_id: str | int,
        clean_finished_at: str,
    ) -> bool:
        # A known scoped quarantine remains visible to accounting/risk, but it
        # must not keep an otherwise repaired global pause latched forever.
        unresolved = bool(self.entry_blocking_intents())
        uncertain_exposure = any(
            str(position.get("state") or "").upper()
            == "EXIT_RECONCILIATION_REQUIRED"
            for position in self.entry_blocking_positions()
        )
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            generation = int(state.get("pause_generation", "0") or 0)
            acquired_at = state.get("pause_acquired_at", "")
            eligible = (
                state.get("pause_entries", "true").lower() == "true"
                and state.get("pause_owner", "").upper()
                in {"RECONCILIATION", "MACHINE"}
                and state.get("release_policy")
                == ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION
                and bool(clean_finished_at)
                and bool(acquired_at)
                and clean_finished_at > acquired_at
                and not unresolved
                and not uncertain_exposure
            )
            if not eligible:
                conn.rollback()
                return False
            self.base.set_states_on_connection(
                conn,
                {
                    "pause_auto_recoverable": "true",
                    "recovery_financial_verified_generation": str(generation),
                    "pause_source_reconciliation_run_id": str(
                        reconciliation_run_id
                    ),
                    "pause_updated_at": now_iso(),
                    "recovery_last_action": "RECONCILIATION_VERIFIED",
                    "recovery_last_result": "CLEAN",
                },
                actor,
            )
            conn.commit()
        self.timeline(
            severity="INFO",
            category="RECONCILIATION",
            component="recovery",
            source=actor,
            requested_action="PROMOTE_RECOVERY_ELIGIBILITY",
            reason_code="CLEAN_EVIDENCE_AFTER_PAUSE",
            result_status="ALLOWED",
            parameters_json={
                "generation": generation,
                "run_id": reconciliation_run_id,
                "finished_at": clean_finished_at,
            },
        )
        return True

    def set_waiting_stability(
        self, *, expected_generation: int, eligible_since: str
    ) -> bool:
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            if (
                state.get("pause_entries", "true").lower() != "true"
                or int(state.get("pause_generation", "0") or 0)
                != expected_generation
            ):
                conn.rollback()
                return False
            already_waiting = (
                state.get("pause_state")
                == PauseState.PAUSED_WAITING_STABILITY
                and bool(state.get("pause_eligible_since"))
            )
            if already_waiting:
                conn.rollback()
                return True
            self.base.set_states_on_connection(
                conn,
                {
                    "pause_state": PauseState.PAUSED_WAITING_STABILITY,
                    "pause_eligible_since": eligible_since,
                    "pause_updated_at": now_iso(),
                    "recovery_status": "WAITING_STABILITY",
                    "recovery_stability_elapsed_ms": "0",
                },
                "pause_recovery",
            )
            conn.commit()
        self.timeline(
            severity="INFO",
            category="SAFETY",
            component="recovery",
            source="pause_recovery",
            requested_action="STABILITY_WINDOW_START",
            reason_code="ALL_RELEASE_GATES_CLEAN",
            result_status="STARTED",
            parameters_json={"generation": expected_generation},
        )
        return True

    def reset_stability(
        self, *, expected_generation: int, blockers: list[dict[str, Any]]
    ) -> bool:
        blockers_json = json.dumps(sanitize(blockers), sort_keys=True)
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            if (
                state.get("pause_entries", "true").lower() != "true"
                or int(state.get("pause_generation", "0") or 0)
                != expected_generation
            ):
                conn.rollback()
                return False
            was_waiting = (
                state.get("pause_state")
                == PauseState.PAUSED_WAITING_STABILITY
            )
            previous_blockers = state.get("recovery_blockers_json", "[]")
            target_state = (
                PauseState.PAUSED_MANUAL_ONLY
                if state.get("release_policy") == ReleasePolicy.MANUAL_ONLY
                else PauseState.PAUSED_RECOVERING
            )
            if (
                not was_waiting
                and previous_blockers == blockers_json
                and state.get("pause_state") == target_state
                and not state.get("pause_eligible_since")
            ):
                conn.rollback()
                return True
            values = {
                "pause_state": target_state,
                "pause_eligible_since": "",
                "pause_updated_at": now_iso(),
                "recovery_status": (
                    "MANUAL_ONLY"
                    if state.get("release_policy") == ReleasePolicy.MANUAL_ONLY
                    else "RECOVERING"
                ),
                "recovery_blockers_json": blockers_json,
                "recovery_stability_elapsed_ms": "0",
            }
            self.base.set_states_on_connection(
                conn, values, "pause_recovery"
            )
            conn.commit()
        if was_waiting or previous_blockers != blockers_json:
            self.timeline(
                severity="INFO",
                category="SAFETY",
                component="recovery",
                source="pause_recovery",
                requested_action=(
                    "STABILITY_WINDOW_RESET"
                    if was_waiting
                    else "RECOVERY_BLOCKERS_CHANGED"
                ),
                reason_code=(
                    str(blockers[0].get("code"))
                    if blockers
                    else "NO_BLOCKERS"
                ),
                result_status="BLOCKED" if blockers else "RESET",
                parameters_json={
                    "generation": expected_generation,
                    "blockers": blockers,
                },
            )
        return True

    def release_pause_cas(
        self,
        *,
        expected_generation: int,
        expected_owner: str,
        actor: str,
        reason: str,
    ) -> bool:
        if actor not in {"operator", "pause_recovery"}:
            return False
        released_at = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._state_map_on_connection(conn, self.PAUSE_STATE_KEYS)
            if (
                state.get("pause_entries", "true").lower() != "true"
                or int(state.get("pause_generation", "0") or 0)
                != expected_generation
                or state.get("pause_owner", "NONE").upper()
                != expected_owner.upper()
                or (
                    actor == "pause_recovery"
                    and state.get("release_policy")
                    == ReleasePolicy.MANUAL_ONLY
                )
            ):
                conn.rollback()
                return False
            self.base.set_states_on_connection(
                conn,
                {
                    "pause_entries": "false",
                    "pause_state": PauseState.TRADING,
                    "pause_owner": "NONE",
                    "pause_reason": "",
                    "pause_auto_recoverable": "false",
                    "pause_eligible_since": "",
                    "pause_updated_at": released_at,
                    "pause_last_release_at": released_at,
                    "pause_last_release_reason": reason,
                    "recovery_status": "HEALTHY",
                    "recovery_stability_elapsed_ms": "0",
                    "recovery_last_action": (
                        "AUTO_RESUME"
                        if actor == "pause_recovery"
                        else "MANUAL_RESUME"
                    ),
                    "recovery_last_result": "RELEASED",
                    "last_auto_recovery_at": (
                        released_at if actor == "pause_recovery" else ""
                    ),
                },
                actor,
            )
            conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    released_at, actor, "pause_released", "ok", reason,
                    json.dumps(
                        {
                            "generation": expected_generation,
                            "owner": expected_owner,
                            "mode": (
                                "AUTO"
                                if actor == "pause_recovery"
                                else "MANUAL"
                            ),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        self.timeline(
            severity="INFO",
            category="SAFETY" if actor == "pause_recovery" else "OPERATOR",
            component="recovery",
            source=actor,
            requested_action="RESUME_ENTRIES",
            reason_code=reason,
            previous_state="true",
            new_state="false",
            result_status="RELEASED",
            parameters_json={"generation": expected_generation},
        )
        return True

    def set_pause_entries(
        self, paused: bool, actor: str, reason: str, *,
        owner: str | None = None, auto_recoverable: bool = False,
    ) -> bool:
        # Compatibility facade. Recovery policy, not the caller-provided
        # boolean, is the source of truth for automatic release.
        if paused:
            _record, changed = self.acquire_pause(
                actor=actor, reason=reason, owner=owner
            )
            return changed
        record = self.pause_record()
        return self.release_pause_cas(
            expected_generation=int(record["pause_generation"]),
            expected_owner=str(record.get("pause_owner") or "NONE"),
            actor=actor,
            reason=reason,
        )

    def entry_slot_status(self) -> dict[str, Any]:
        """Return the durable single-entry-slot blocker without mutating it."""
        with self.base.connect() as conn:
            unresolved = conn.execute(
                """
                SELECT intent_id,event_id,state
                FROM live_strategy_intents
                WHERE state NOT IN (
                    'FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED',
                    'REJECTED','FAILED','SETTLED','REDEEMED'
                )
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
            active_position = conn.execute(
                """
                SELECT position_id,event_id,state,closed_at,
                       remaining_shares_text,sellable_shares_text
                FROM live_strategy_positions
                WHERE state IN (
                    'OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED'
                )
                   OR (state='DUST' AND closed_at IS NULL)
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
        blocker = unresolved if unresolved is not None else active_position
        if blocker is None:
            return {"available": True, "blocker_kind": None}
        blocker_kind = "INTENT" if unresolved is not None else "POSITION"
        return {
            "available": False,
            "blocker_kind": blocker_kind,
            "event_id": str(blocker["event_id"]),
            "state": str(blocker["state"]),
            "intent_id": (
                str(blocker["intent_id"]) if blocker_kind == "INTENT" else None
            ),
            "position_id": (
                str(blocker["position_id"]) if blocker_kind == "POSITION" else None
            ),
            "closed_at": (
                str(blocker["closed_at"])
                if blocker_kind == "POSITION" and blocker["closed_at"] else None
            ),
            "remaining_shares_text": (
                str(blocker["remaining_shares_text"])
                if blocker_kind == "POSITION" else None
            ),
            "sellable_shares_text": (
                str(blocker["sellable_shares_text"])
                if blocker_kind == "POSITION" else None
            ),
        }

    def reserve_event_entry(
        self,
        *,
        event_id: str,
        condition_id: str,
        token_id: str | None,
        side: str | None,
        simultaneous: bool,
        reason_code: str,
        consume_canary: bool = False,
        require_empty_slot: bool = False,
    ) -> dict[str, Any]:
        ts = now_iso()
        status = "SKIPPED_SIMULTANEOUS_TRIGGER" if simultaneous else "ENTRY_INTENT_RESERVED"
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            quarantine = conn.execute(
                "SELECT quarantine_id,incident_scope,reason_code FROM "
                "live_quarantines WHERE status='OPEN' AND "
                "((event_id IS NOT NULL AND event_id=?) OR "
                "(token_id IS NOT NULL AND token_id=?)) "
                "ORDER BY last_seen_at DESC LIMIT 1",
                (event_id, str(token_id or "")),
            ).fetchone()
            if quarantine is not None:
                conn.rollback()
                return {
                    "_blocked": True,
                    "reason": "SCOPED_QUARANTINE",
                    "quarantine_id": str(quarantine["quarantine_id"]),
                    "incident_scope": str(quarantine["incident_scope"]),
                    "reason_code": str(quarantine["reason_code"]),
                    "blocking_event_id": event_id,
                }
            existing = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if simultaneous and existing["status"] == "ENTRY_ZERO_FILL":
                    conn.execute(
                        """
                        UPDATE live_event_states
                        SET status='SKIPPED_SIMULTANEOUS_TRIGGER',locked_side=NULL,
                            locked_token_id=NULL,lock_reason=?,entry_intent_id=NULL,
                            locked_at=?,updated_at=?
                        WHERE event_id=?
                        """,
                        (reason_code, ts, ts, event_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM live_event_states WHERE event_id=?", (event_id,)
                    ).fetchone()
                    conn.commit()
                    return row_to_dict(row) or {}
                prior_non_zero_fill = conn.execute(
                    """
                    SELECT 1 FROM live_strategy_intents
                    WHERE event_id=? AND action='ENTRY' AND state!='ZERO_FILL'
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
                positive_fill = conn.execute(
                    """
                    SELECT 1
                    FROM live_strategy_intents AS i
                    WHERE i.event_id=? AND i.action='ENTRY'
                      AND (
                        CAST(COALESCE(i.filled_shares_text,'0') AS REAL) > 0
                        OR EXISTS (
                          SELECT 1 FROM live_strategy_fills AS f
                          WHERE f.intent_id=i.intent_id
                            AND CAST(COALESCE(f.shares_text,'0') AS REAL) > 0
                        )
                      )
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
                position = conn.execute(
                    "SELECT 1 FROM live_strategy_positions WHERE event_id=? LIMIT 1",
                    (event_id,),
                ).fetchone()
                if (
                    simultaneous
                    or existing["status"] != "ENTRY_ZERO_FILL"
                    or prior_non_zero_fill is not None
                    or positive_fill is not None
                    or position is not None
                ):
                    conn.rollback()
                    return {**(row_to_dict(existing) or {}), "_duplicate": True}
            attempt_number = int(conn.execute(
                "SELECT COUNT(*) FROM live_strategy_intents WHERE event_id=? AND action='ENTRY'",
                (event_id,),
            ).fetchone()[0]) + 1
            attempt_identity = (
                event_id if attempt_number == 1
                else f"{event_id}:attempt:{attempt_number}"
            )
            intent_id = stable_id("entry", attempt_identity)
            correlation_id = stable_id("correlation", attempt_identity)
            if require_empty_slot:
                unresolved = conn.execute(
                    "SELECT i.intent_id,i.event_id,i.state "
                    "FROM live_strategy_intents AS i "
                    "LEFT JOIN live_strategy_positions AS p "
                    "ON p.position_id=i.position_id "
                    "WHERE i.state NOT IN ('FILLED', 'PARTIAL_FINAL', 'ZERO_FILL', 'CANCELED', "
                    "'REJECTED', 'FAILED', 'SETTLED', 'REDEEMED') "
                    "AND COALESCE(p.state,'') != 'QUARANTINED' "
                    "AND NOT EXISTS (SELECT 1 FROM live_quarantines AS q "
                    "WHERE q.status='OPEN' AND ("
                    "(q.event_id IS NOT NULL AND q.event_id=i.event_id) OR "
                    "(q.token_id IS NOT NULL AND q.token_id=i.token_id))) "
                    "LIMIT 1"
                ).fetchone()
                active_position = conn.execute(
                    "SELECT position_id,event_id,state,closed_at,remaining_shares_text,"
                    "sellable_shares_text FROM live_strategy_positions "
                    "WHERE state IN ('OPEN', 'TP_OPEN', 'EXITING', "
                    "'EXIT_RECONCILIATION_REQUIRED') "
                    "OR (state='DUST' AND closed_at IS NULL) LIMIT 1"
                ).fetchone()
                if unresolved is not None or active_position is not None:
                    conn.rollback()
                    blocker = unresolved if unresolved is not None else active_position
                    blocker_kind = "INTENT" if unresolved is not None else "POSITION"
                    return {
                        "_blocked": True,
                        "reason": "ACTIVE_ENTRY_SLOT_OCCUPIED",
                        "blocker_kind": blocker_kind,
                        "blocking_intent_id": (
                            str(blocker["intent_id"])
                            if blocker_kind == "INTENT" else None
                        ),
                        "blocking_position_id": (
                            str(blocker["position_id"])
                            if blocker_kind == "POSITION" else None
                        ),
                        "blocking_event_id": str(blocker["event_id"]),
                        "blocking_state": str(blocker["state"]),
                        "blocking_closed_at": (
                            str(blocker["closed_at"])
                            if blocker_kind == "POSITION" and blocker["closed_at"]
                            else None
                        ),
                        "blocking_remaining_shares_text": (
                            str(blocker["remaining_shares_text"])
                            if blocker_kind == "POSITION" else None
                        ),
                        "blocking_sellable_shares_text": (
                            str(blocker["sellable_shares_text"])
                            if blocker_kind == "POSITION" else None
                        ),
                    }
            if consume_canary:
                state = {
                    row["key"]: row["value"]
                    for row in conn.execute(
                        "SELECT key,value FROM live_system_state WHERE key IN "
                        "('canary_armed','canary_consumed','kill_switch','pause_entries')"
                    ).fetchall()
                }
                if (
                    state.get("canary_armed") != "true"
                    or state.get("canary_consumed") == "true"
                    or state.get("kill_switch", "true") != "false"
                    or state.get("pause_entries", "true") != "false"
                ):
                    conn.rollback()
                    return {"_blocked": True, "reason": "CANARY_NOT_AVAILABLE"}
            if existing is None:
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
            else:
                conn.execute(
                    """
                    UPDATE live_event_states
                    SET condition_id=?,status=?,locked_side=?,locked_token_id=?,
                        lock_reason=?,entry_intent_id=?,locked_at=?,updated_at=?
                    WHERE event_id=?
                    """,
                    (
                        condition_id, status, side, token_id, reason_code,
                        intent_id, ts, ts, event_id,
                    ),
                )
            if not simultaneous:
                conn.execute(
                    """
                    INSERT INTO live_strategy_intents(
                        intent_id,correlation_id,event_id,condition_id,action,purpose,
                        token_id,side,state,order_type,requested_amount_text,requested_shares_text,
                        price_limit_text,max_spend_text,retry_count,reason_code,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'FAK','3.8','5','0.76','5',?,?,?,?)
                    """,
                    (
                        intent_id, correlation_id, event_id, condition_id, "ENTRY", "ENTRY",
                        token_id, side, "RESERVED", attempt_number - 1, reason_code, ts, ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO live_strategy_deals(
                        deal_id,event_id,state,outcome,trigger_price_text,entry_intent_id,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        state='ENTRY_PENDING',outcome=excluded.outcome,
                        trigger_price_text=excluded.trigger_price_text,
                        entry_intent_id=excluded.entry_intent_id,
                        position_id=NULL,final_reason=NULL,opened_at=NULL,closed_at=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        stable_id("deal", event_id), event_id, "ENTRY_PENDING", side,
                        "0.74", intent_id, ts, ts,
                    ),
                )
            if consume_canary:
                for key, value in {
                    "pause_entries": "true",
                    "pause_owner": "MACHINE",
                    "pause_reason": "CANARY_CONSUMED",
                    "pause_auto_recoverable": "false",
                    "canary_armed": "false",
                    "canary_consumed": "true",
                }.items():
                    conn.execute(
                        "INSERT INTO live_system_state(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (key, value, ts),
                    )
            row = conn.execute(
                "SELECT * FROM live_event_states WHERE event_id = ?", (event_id,)
            ).fetchone()
            conn.commit()
        if not simultaneous:
            self._inflight_submission_intents.add(intent_id)
        return row_to_dict(row) or {}

    def consume_canary(self, actor: str = "strategy") -> None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, value in {
                "pause_entries": "true",
                "pause_owner": "MACHINE",
                "pause_reason": "CANARY_CONSUMED",
                "pause_auto_recoverable": "false",
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
        result = row_to_dict(row) or {}
        state = str(result.get("state") or "").upper()
        if result.get("remote_order_id") or state not in {"RESERVED", "SUBMITTING"}:
            self._inflight_submission_intents.discard(intent_id)
        return result

    def intent_submission_inflight(self, intent_id: str) -> bool:
        return intent_id in self._inflight_submission_intents

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
        fee_verification_status: str = "UNKNOWN",
        fee_source: str | None = None,
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
                        fee_verification_status,fee_source,status,transaction_hash,matched_at,raw_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fill_id, intent_id, remote_trade_id, canonical_decimal(shares),
                        canonical_decimal(price), canonical_decimal(fee),
                        fee_verification_status, fee_source, status, transaction_hash, matched_at, json.dumps(sanitize(raw or {}), sort_keys=True),
                        ts, ts,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def fill_summary(self, intent_id: str) -> dict[str, Decimal]:
        """Return a deterministic aggregate of deduplicated durable fills."""
        with self.base.connect() as conn:
            rows = conn.execute(
                "SELECT shares_text,price_text,fee_text FROM live_strategy_fills WHERE intent_id=?",
                (intent_id,),
            ).fetchall()
        shares = Decimal("0")
        notional = Decimal("0")
        fees = Decimal("0")
        for row in rows:
            fill_shares = decimal_value(row["shares_text"]) or Decimal("0")
            fill_price = decimal_value(row["price_text"]) or Decimal("0")
            shares += fill_shares
            notional += fill_shares * fill_price
            fees += decimal_value(row["fee_text"]) or Decimal("0")
        return {
            "shares": shares,
            "notional": notional,
            "fees": fees,
            "average_price": notional / shares if shares > 0 else Decimal("0"),
        }

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
        entry_intent_id: str | None = None,
    ) -> dict[str, Any]:
        position_id = stable_id("position", event_id)
        deal_id = stable_id("deal", event_id)
        sellable = shares if sellable_shares is None else sellable_shares
        is_dust = shares > 0 and min_sellable > 0 and shares < min_sellable
        if is_dust:
            sellable = Decimal("0")
        position_state = "DUST" if is_dust else "OPEN"
        dust = shares if is_dust else Decimal("0")
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if entry_intent_id is None:
                event_row = conn.execute(
                    "SELECT entry_intent_id FROM live_event_states WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                entry_intent_id = (
                    str(event_row["entry_intent_id"])
                    if event_row is not None and event_row["entry_intent_id"]
                    else None
                )
            if not entry_intent_id:
                conn.rollback()
                raise RuntimeError(f"missing entry intent for event {event_id}")
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
                    canonical_decimal(fees), ts, ts, entry_intent_id,
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

    def mark_zero_fill(
        self, event_id: str, reason: str, *, intent_id: str | None = None
    ) -> None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if intent_id is None:
                event_row = conn.execute(
                    "SELECT entry_intent_id FROM live_event_states WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                intent_id = (
                    str(event_row["entry_intent_id"])
                    if event_row is not None and event_row["entry_intent_id"]
                    else None
                )
            intent = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=? AND event_id=?",
                (intent_id, event_id),
            ).fetchone()
            if intent is None:
                conn.rollback()
                raise KeyError(intent_id)
            fill_total = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(shares_text AS REAL)),0)
                FROM live_strategy_fills WHERE intent_id=?
                """,
                (intent_id,),
            ).fetchone()[0]
            if (
                (decimal_value(intent["filled_shares_text"]) or Decimal("0")) > 0
                or float(fill_total or 0) > 0
            ):
                conn.rollback()
                raise RuntimeError("positive fill cannot be finalized as ZERO_FILL")
            conn.execute(
                """
                UPDATE live_strategy_intents
                SET state='ZERO_FILL',reason_code=?,final_at=?,updated_at=?
                WHERE intent_id=?
                """,
                (reason, ts, ts, intent_id),
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

    def _positions_in_states(
        self, states: Iterable[str], token_id: str | None = None
    ) -> list[dict[str, Any]]:
        state_list = tuple(sorted({str(state).upper() for state in states}))
        placeholders = ",".join("?" for _ in state_list)
        query = (
            "SELECT * FROM live_strategy_positions "
            f"WHERE state IN ({placeholders})"
        )
        params: tuple[Any, ...] = state_list
        if token_id is not None:
            query += " AND token_id=?"
            params = (*params, str(token_id))
        query += " ORDER BY created_at"
        with self.base.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def risk_managed_positions(
        self, token_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._positions_in_states(
            {*OPEN_POSITION_STATES, "QUARANTINED"}, token_id
        )

    def reconciliation_positions(
        self, token_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.risk_managed_positions(token_id)

    def entry_blocking_positions(
        self, token_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._positions_in_states(OPEN_POSITION_STATES, token_id)

    def fast_reconciliation_positions(self) -> list[dict[str, Any]]:
        return self.entry_blocking_positions()

    def quarantined_positions(self) -> list[dict[str, Any]]:
        return self._positions_in_states({"QUARANTINED"})

    def active_positions(self, token_id: str | None = None) -> list[dict[str, Any]]:
        """Backward-compatible risk-managed view; quarantines stay visible."""
        return self.risk_managed_positions(token_id)

    def quarantine_records(
        self, *, status: str = "OPEN", limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.base.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM live_quarantines WHERE status=? "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (str(status).upper(), max(1, min(int(limit), 1000))),
            ).fetchall()
        result = []
        for row in rows:
            item = row_to_dict(row) or {}
            try:
                item["evidence"] = json.loads(item.get("evidence_json") or "{}")
            except (TypeError, ValueError):
                item["evidence"] = {}
            result.append(item)
        return result

    def _refresh_incident_state_on_connection(
        self, conn: sqlite3.Connection, actor: str
    ) -> None:
        open_rows = conn.execute(
            "SELECT * FROM live_quarantines WHERE status='OPEN' "
            "ORDER BY last_seen_at DESC"
        ).fetchall()
        operator_rows = [row for row in open_rows if row["operator_action_required"]]
        global_rows = [row for row in open_rows if row["global_entry_halt_required"]]
        state = self._state_map_on_connection(
            conn, ("kill_switch", "pause_cause")
        )
        hard_stop_reason = ""
        if state.get("kill_switch", "false").lower() == "true":
            hard_stop_reason = "KILL_SWITCH_ACTIVE"
        elif state.get("pause_cause", "").upper() in GLOBAL_HARD_STOP_REASONS:
            hard_stop_reason = state.get("pause_cause", "").upper()
        latest = open_rows[0] if open_rows else None
        operator = operator_rows[0] if operator_rows else None
        global_incident = global_rows[0] if global_rows else None
        self.base.set_states_on_connection(conn, {
            "operator_action_required": "true" if operator else "false",
            "operator_action_reason": (
                str(operator["reason_code"]) if operator else ""
            ),
            "global_entry_halt_required": (
                "true" if hard_stop_reason or global_incident else "false"
            ),
            "global_entry_halt_reason": (
                hard_stop_reason
                or (str(global_incident["reason_code"]) if global_incident else "")
            ),
            "incident_scope": (
                str(latest["incident_scope"]) if latest else "UNKNOWN"
            ),
            "quarantined_positions_count": str(len({
                str(row["position_id"])
                for row in open_rows if row["position_id"]
            })),
            "quarantine_last_at": (
                str(latest["last_seen_at"]) if latest else ""
            ),
        }, actor)

    def quarantine_incident(
        self,
        *,
        incident_scope: str,
        entity_type: str,
        entity_id: str,
        reason_code: str,
        evidence: dict[str, Any],
        actor: str,
        position_id: str | None = None,
        token_id: str | None = None,
        event_id: str | None = None,
        condition_id: str | None = None,
        operator_action_required: bool = True,
    ) -> dict[str, Any]:
        scope = str(incident_scope or IncidentScope.UNKNOWN).upper()
        if scope not in {item.value for item in IncidentScope}:
            raise ValueError(f"invalid incident scope: {scope}")
        scoped = scope in {
            IncidentScope.POSITION,
            IncidentScope.TOKEN,
            IncidentScope.EVENT,
        }
        ts = now_iso()
        safe_evidence = sanitize(evidence)
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            position = None
            before_state = None
            if position_id:
                position = conn.execute(
                    "SELECT * FROM live_strategy_positions WHERE position_id=?",
                    (str(position_id),),
                ).fetchone()
                if position is None:
                    conn.rollback()
                    raise KeyError(position_id)
                before_state = str(position["state"])
            existing = conn.execute(
                "SELECT * FROM live_quarantines WHERE incident_scope=? "
                "AND entity_type=? AND entity_id=? AND reason_code=? "
                "AND status='OPEN'",
                (scope, str(entity_type).upper(), str(entity_id), str(reason_code).upper()),
            ).fetchone()
            if existing is None:
                quarantine_id = stable_id(
                    "quarantine",
                    f"{scope}:{entity_type}:{entity_id}:{reason_code}:{ts}",
                )
                conn.execute(
                    "INSERT INTO live_quarantines "
                    "(quarantine_id,incident_scope,entity_type,entity_id,"
                    "position_id,token_id,event_id,condition_id,reason_code,status,"
                    "operator_action_required,global_entry_halt_required,before_state,"
                    "evidence_json,first_seen_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?)",
                    (
                        quarantine_id, scope, str(entity_type).upper(), str(entity_id),
                        position_id, token_id, event_id, condition_id,
                        str(reason_code).upper(),
                        1 if operator_action_required else 0,
                        0 if scoped else 1,
                        before_state,
                        json.dumps(safe_evidence, sort_keys=True, separators=(",", ":")),
                        ts, ts,
                    ),
                )
            else:
                quarantine_id = str(existing["quarantine_id"])
                before_state = str(existing["before_state"] or before_state or "")
                conn.execute(
                    "UPDATE live_quarantines SET evidence_json=?,last_seen_at=?,"
                    "occurrence_count=occurrence_count+1,"
                    "operator_action_required=MAX(operator_action_required,?) "
                    "WHERE quarantine_id=?",
                    (
                        json.dumps(safe_evidence, sort_keys=True, separators=(",", ":")),
                        ts, 1 if operator_action_required else 0, quarantine_id,
                    ),
                )
            if position is not None:
                conn.execute(
                    "UPDATE live_strategy_positions SET state='QUARANTINED',"
                    "updated_at=? WHERE position_id=?",
                    (ts, str(position_id)),
                )
                conn.execute(
                    "UPDATE live_event_states SET status='QUARANTINED',updated_at=? "
                    "WHERE event_id=?",
                    (ts, str(position["event_id"])),
                )
            self._refresh_incident_state_on_connection(conn, actor)
            conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    ts, actor, "incident_quarantined", "ok",
                    str(reason_code).upper(),
                    json.dumps(sanitize({
                        "quarantine_id": quarantine_id,
                        "scope": scope,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "position_id": position_id,
                        "before_state": before_state,
                        "global_entry_halt_required": not scoped,
                        "evidence": safe_evidence,
                    }), sort_keys=True),
                ),
            )
            row = conn.execute(
                "SELECT * FROM live_quarantines WHERE quarantine_id=?",
                (quarantine_id,),
            ).fetchone()
            conn.commit()
        return row_to_dict(row) or {}

    def quarantine_position(
        self,
        position_id: str,
        *,
        reason_code: str,
        evidence: dict[str, Any],
        actor: str,
        operator_action_required: bool = True,
    ) -> dict[str, Any]:
        with self.base.connect() as conn:
            position = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?",
                (str(position_id),),
            ).fetchone()
        if position is None:
            raise KeyError(position_id)
        return self.quarantine_incident(
            incident_scope=IncidentScope.POSITION,
            entity_type="POSITION",
            entity_id=str(position_id),
            reason_code=reason_code,
            evidence=evidence,
            actor=actor,
            position_id=str(position_id),
            token_id=str(position["token_id"]),
            event_id=str(position["event_id"]),
            condition_id=str(position["condition_id"]),
            operator_action_required=operator_action_required,
        )

    def resolve_position_quarantine(
        self,
        position_id: str,
        *,
        actor: str,
        reason: str,
    ) -> int:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE live_quarantines SET status='RESOLVED',resolved_at=?,"
                "resolution_reason=?,last_seen_at=? "
                "WHERE position_id=? AND status='OPEN'",
                (ts, str(reason), ts, str(position_id)),
            )
            resolved = int(cursor.rowcount)
            self._refresh_incident_state_on_connection(conn, actor)
            if resolved:
                conn.execute(
                    "INSERT INTO live_audit_log "
                    "(occurred_at,actor,action,status,reason,details_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        ts, actor, "incident_quarantine_resolved", "ok", reason,
                        json.dumps({"position_id": position_id, "resolved": resolved}),
                    ),
                )
            conn.commit()
        return resolved

    def is_quarantined(
        self, *, event_id: str | None = None, token_id: str | None = None
    ) -> bool:
        clauses = []
        params: list[str] = []
        if event_id:
            clauses.append("event_id=?")
            params.append(str(event_id))
        if token_id:
            clauses.append("token_id=?")
            params.append(str(token_id))
        if not clauses:
            return False
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM live_quarantines WHERE status='OPEN' AND ("
                + " OR ".join(clauses) + ") LIMIT 1",
                tuple(params),
            ).fetchone()
        return row is not None

    def exposure(self) -> Decimal:
        total = Decimal("0")
        for position in self.risk_managed_positions():
            remaining = decimal_value(position["remaining_shares_text"]) or Decimal("0")
            acquired = decimal_value(position["acquired_shares_text"]) or Decimal("0")
            cost = decimal_value(position["cost_all_in_text"]) or Decimal("0")
            if remaining > 0 and acquired > 0:
                total += cost * remaining / acquired
        return total

    def latch_stop_exit(
        self,
        position_id: str,
    ) -> dict[str, Any]:
        """Durably latch the one-way 0.66 exit before any remote action."""
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?",
                (position_id,),
            ).fetchone()
            if current is None:
                conn.rollback()
                raise KeyError(position_id)

            remaining = (
                decimal_value(current["remaining_shares_text"])
                or Decimal("0")
            )
            prior_stage = int(current["stop_stage"] or 0)
            newly_latched = prior_stage < 1 and remaining > 0

            if remaining > 0:
                conn.execute(
                    """
                    UPDATE live_strategy_positions
                    SET stop_stage=MAX(stop_stage,1),
                        state=CASE
                            WHEN state='EXIT_RECONCILIATION_REQUIRED'
                                THEN state
                            ELSE 'EXITING'
                        END,
                        updated_at=?
                    WHERE position_id=?
                    """,
                    (ts, position_id),
                )
                conn.execute(
                    """
                    UPDATE live_event_states
                    SET status='EXITING',updated_at=?
                    WHERE event_id=?
                    """,
                    (ts, current["event_id"]),
                )

            updated = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?",
                (position_id,),
            ).fetchone()
            conn.commit()

        return {
            **(row_to_dict(updated) or {}),
            "_newly_latched": newly_latched,
        }

    def require_exit_reconciliation(
        self,
        position_id: str,
    ) -> None:
        """Fail closed until remote order/fill truth is known."""
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT event_id,remaining_shares_text
                FROM live_strategy_positions
                WHERE position_id=?
                """,
                (position_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(position_id)
            remaining = (
                decimal_value(row["remaining_shares_text"])
                or Decimal("0")
            )
            if remaining > 0:
                conn.execute(
                    """
                    UPDATE live_strategy_positions
                    SET state='EXIT_RECONCILIATION_REQUIRED',
                        stop_stage=MAX(stop_stage,1),
                        updated_at=?
                    WHERE position_id=?
                    """,
                    (ts, position_id),
                )
                conn.execute(
                    """
                    UPDATE live_event_states
                    SET status='EXIT_RECONCILIATION_REQUIRED',updated_at=?
                    WHERE event_id=?
                    """,
                    (ts, row["event_id"]),
                )
            conn.commit()

    def clear_exit_reconciliation(
        self,
        position_id: str,
    ) -> None:
        """Return a reconciled, still-open latched position to EXITING."""
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE live_strategy_positions
                SET state='EXITING',updated_at=?
                WHERE position_id=?
                  AND state='EXIT_RECONCILIATION_REQUIRED'
                  AND stop_stage>=1
                  AND CAST(remaining_shares_text AS REAL)>0
                """,
                (ts, position_id),
            )
            conn.execute(
                """
                UPDATE live_event_states
                SET status='EXITING',updated_at=?
                WHERE event_id=(
                    SELECT event_id FROM live_strategy_positions
                    WHERE position_id=? AND state='EXITING'
                )
                """,
                (ts, position_id),
            )
            conn.commit()

    def note_exit_liquidity_wait(
        self,
        position_id: str,
        book_hash: str,
    ) -> bool:
        """Persist one no-liquidity observation per distinct book state."""
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT last_exit_book_hash,stop_stage,remaining_shares_text
                FROM live_strategy_positions
                WHERE position_id=?
                """,
                (position_id,),
            ).fetchone()
            if (
                row is None
                or int(row["stop_stage"] or 0) < 1
                or (decimal_value(row["remaining_shares_text"]) or Decimal("0")) <= 0
                or str(row["last_exit_book_hash"] or "") == book_hash
            ):
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE live_strategy_positions
                SET last_exit_book_hash=?,updated_at=?
                WHERE position_id=?
                """,
                (book_hash, ts, position_id),
            )
            conn.commit()
        return True

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
        base_identity = (
            f"{position_id}:{purpose}:{position.get('stop_stage', 0)}:{book_hash}"
        )
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

            prior_attempts = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM live_strategy_intents
                    WHERE position_id=? AND action=? AND purpose=?
                    """,
                    (position_id, action, purpose),
                ).fetchone()[0]
                or 0
            )

            identity = (
                base_identity
                if prior_attempts == 0
                else f"{base_identity}:retry:{prior_attempts}"
            )
            intent_id = stable_id("intent", identity)

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
        self._inflight_submission_intents.add(intent_id)
        return row_to_dict(row) or {}

    def mark_waiting_sellable(
        self,
        intent_id: str,
        *,
        reason: str,
        normalized_error: str | None = None,
    ) -> dict[str, Any]:
        """Keep a local-only SELL intent pending until token balance is sellable.

        This state is valid only when there is no known live remote order.
        sellable_shares is reset to zero so the runtime will not repeatedly
        submit from stale local sellability.
        """
        ts = now_iso()

        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT position_id,purpose,remote_order_id
                FROM live_strategy_intents
                WHERE intent_id=?
                """,
                (intent_id,),
            ).fetchone()

            if row is None:
                conn.rollback()
                raise KeyError(intent_id)

            if row["remote_order_id"]:
                conn.rollback()
                raise RuntimeError(
                    "WAITING_SELLABLE cannot be applied to a remote order"
                )

            conn.execute(
                """
                UPDATE live_strategy_intents
                SET state='WAITING_SELLABLE',
                    reason_code=?,
                    normalized_error=COALESCE(?,normalized_error),
                    updated_at=?
                WHERE intent_id=?
                """,
                (reason, normalized_error, ts, intent_id),
            )

            if row["position_id"]:
                position_state = (
                    "TP_OPEN"
                    if row["purpose"] == "TAKE_PROFIT"
                    else "EXITING"
                )
                conn.execute(
                    """
                    UPDATE live_strategy_positions
                    SET sellable_shares_text='0',
                        state=?,
                        updated_at=?
                    WHERE position_id=?
                    """,
                    (position_state, ts, row["position_id"]),
                )

            updated = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()

            conn.commit()

        return row_to_dict(updated) or {}

    def finalize_position_intent_failure(
        self,
        intent_id: str,
        *,
        state: str,
        reason: str | None,
        normalized_error: str | None = None,
    ) -> None:
        """Finalize a definitively non-live local position order attempt.

        This must never be used for UNKNOWN_AFTER_SUBMISSION because a remote
        order may exist in that case.
        """
        if state not in {"FAILED", "REJECTED", "ZERO_FILL"}:
            raise ValueError(
                f"invalid terminal position intent state: {state}"
            )

        ts = now_iso()

        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT position_id,purpose,remote_order_id,last_book_hash
                FROM live_strategy_intents
                WHERE intent_id=?
                """,
                (intent_id,),
            ).fetchone()

            if row is None:
                conn.rollback()
                raise KeyError(intent_id)

            if row["remote_order_id"]:
                conn.rollback()
                raise RuntimeError(
                    "cannot terminalize local-only failure with remote_order_id"
                )

            conn.execute(
                """
                UPDATE live_strategy_intents
                SET state=?,
                    reason_code=?,
                    normalized_error=COALESCE(?,normalized_error),
                    final_at=?,
                    updated_at=?
                WHERE intent_id=?
                """,
                (
                    state,
                    reason,
                    normalized_error,
                    ts,
                    ts,
                    intent_id,
                ),
            )

            if row["position_id"]:
                column = (
                    "tp_intent_id"
                    if row["purpose"] == "TAKE_PROFIT"
                    else "active_exit_intent_id"
                )

                position = conn.execute(
                    f"""
                    SELECT {column},remaining_shares_text,stop_stage
                    FROM live_strategy_positions
                    WHERE position_id=?
                    """,
                    (row["position_id"],),
                ).fetchone()

                if (
                    position is not None
                    and str(position[column] or "") == intent_id
                ):
                    remaining = (
                        decimal_value(
                            position["remaining_shares_text"]
                        )
                        or Decimal("0")
                    )
                    latched_exit = (
                        row["purpose"] == "STOP_066"
                        and remaining > 0
                    )
                    next_state = (
                        "EXITING"
                        if latched_exit
                        else "OPEN"
                    )
                    next_stage = max(
                        int(position["stop_stage"] or 0),
                        1 if latched_exit else 0,
                    )

                    conn.execute(
                        f"""
                        UPDATE live_strategy_positions
                        SET {column}=NULL,
                            state=CASE WHEN ? > 0 THEN ? ELSE state END,
                            stop_stage=?,
                            last_exit_book_hash=CASE
                                WHEN ? THEN ? ELSE last_exit_book_hash
                            END,
                            updated_at=?
                        WHERE position_id=?
                        """,
                        (
                            1 if remaining > 0 else 0,
                            next_state,
                            next_stage,
                            1 if latched_exit else 0,
                            row["last_book_hash"],
                            ts,
                            row["position_id"],
                        ),
                    )

            conn.commit()

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

    def cancel_active_exit(self, position_id: str, reason: str) -> dict[str, Any] | None:
        ts = now_iso()
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            position = conn.execute(
                "SELECT * FROM live_strategy_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if position is None or not position["active_exit_intent_id"]:
                conn.rollback()
                return None
            intent_id = position["active_exit_intent_id"]
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
                "SELECT position_id,purpose FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE live_strategy_intents SET state=?,reason_code=?,final_at=?,updated_at=?
                WHERE intent_id=?
                """,
                (state, reason, ts if success else None, ts, intent_id),
            )
            if row and row["position_id"]:
                column = (
                    "tp_intent_id" if row["purpose"] == "TAKE_PROFIT" else "active_exit_intent_id"
                )
                conn.execute(
                    f"""
                    UPDATE live_strategy_positions SET {column}=NULL,
                        state=CASE
                            WHEN state NOT IN (
                                'OPEN', 'TP_OPEN', 'EXITING',
                                'EXIT_RECONCILIATION_REQUIRED'
                            ) THEN state
                            WHEN ? THEN 'OPEN'
                            ELSE 'EXIT_RECONCILIATION_REQUIRED'
                        END,
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
        cumulative_filled_shares: Decimal | None = None,
        cumulative_notional: Decimal | None = None,
        cumulative_fees: Decimal | None = None,
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
            intent_row = conn.execute(
                "SELECT * FROM live_strategy_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if intent_row is None:
                conn.rollback()
                raise KeyError(intent_id)
            remaining_before = decimal_value(row["remaining_shares_text"]) or Decimal("0")
            prior_intent_filled = (
                decimal_value(intent_row["filled_shares_text"]) or Decimal("0")
            )
            prior_intent_average = (
                decimal_value(intent_row["average_price_text"]) or Decimal("0")
            )
            prior_intent_fees = (
                decimal_value(intent_row["fee_text"]) or Decimal("0")
            )
            prior_intent_notional = prior_intent_filled * prior_intent_average
            if cumulative_filled_shares is not None:
                target_filled = max(prior_intent_filled, cumulative_filled_shares)
                target_notional = max(
                    prior_intent_notional,
                    cumulative_notional
                    if cumulative_notional is not None
                    else target_filled * average_price,
                )
                target_fees = max(
                    prior_intent_fees,
                    cumulative_fees
                    if cumulative_fees is not None
                    else prior_intent_fees + fees,
                )
                delta_shares = target_filled - prior_intent_filled
                delta_notional = target_notional - prior_intent_notional
                delta_fees = target_fees - prior_intent_fees
            else:
                delta_shares = max(Decimal("0"), sold_shares)
                delta_notional = delta_shares * average_price
                delta_fees = max(Decimal("0"), fees)
                target_filled = prior_intent_filled + delta_shares
                target_notional = prior_intent_notional + delta_notional
                target_fees = prior_intent_fees + delta_fees
            exit_value_before = decimal_value(row["exit_value_text"]) or Decimal("0")
            exit_fees_before = decimal_value(row["exit_fees_text"]) or Decimal("0")
            acquired = decimal_value(row["acquired_shares_text"]) or Decimal("0")
            if (
                cumulative_filled_shares is not None
                and prior_intent_filled == 0
                and exit_value_before == 0
                and remaining_before < acquired
            ):
                # A lagging positions response may have reduced local shares
                # before maker fills were attributed. With no accounted exit
                # proceeds and no other filled exit intent, rebuild the
                # pre-exit baseline atomically before applying execution truth.
                other_rows = conn.execute(
                    "SELECT filled_shares_text FROM live_strategy_intents "
                    "WHERE position_id=? AND intent_id<>? "
                    "AND action IN ('EXIT','TP')",
                    (position_id, intent_id),
                ).fetchall()
                other_exit_filled = sum(
                    (
                        decimal_value(item["filled_shares_text"])
                        or Decimal("0")
                        for item in other_rows
                    ),
                    Decimal("0"),
                )
                if other_exit_filled == 0:
                    remaining_before = acquired
            actual_sold = min(delta_shares, remaining_before)
            remaining = remaining_before - actual_sold
            applied_notional = (
                delta_notional * actual_sold / delta_shares
                if delta_shares > 0 else Decimal("0")
            )
            applied_fees = (
                delta_fees * actual_sold / delta_shares
                if delta_shares > 0 else Decimal("0")
            )
            exit_value = exit_value_before + applied_notional
            exit_fees = exit_fees_before + applied_fees
            cost = decimal_value(row["cost_all_in_text"]) or Decimal("0")
            allocated_cost = cost * (acquired - remaining) / acquired if acquired > 0 else Decimal("0")
            pnl = exit_value - exit_fees - allocated_cost
            dust = remaining if Decimal("0") < remaining < min_sellable else Decimal("0")
            keep_active = bool(
                remaining > 0 and final_state == "PARTIAL"
                and intent_row and intent_row["order_type"] == "GTC"
            )
            if remaining == 0:
                position_state = "CLOSED"
            elif dust > 0:
                position_state = "DUST"
            elif final_state in {"UNKNOWN", "CANCEL_UNCERTAIN"}:
                position_state = "EXIT_RECONCILIATION_REQUIRED"
            elif purpose == "TAKE_PROFIT":
                position_state = "TP_OPEN"
            elif purpose == "STOP_066":
                position_state = "EXITING"
            elif keep_active:
                position_state = "EXITING"
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
                    stop_stage=?,active_exit_intent_id=CASE
                        WHEN ? IN ('CLOSED','DUST') THEN NULL
                        WHEN ? THEN active_exit_intent_id ELSE NULL END,
                    tp_intent_id=CASE
                        WHEN ? IN ('CLOSED','DUST') THEN NULL
                        WHEN ?='TAKE_PROFIT' AND ?=1 THEN tp_intent_id
                        WHEN ?='TAKE_PROFIT' THEN NULL ELSE tp_intent_id END,
                    last_exit_book_hash=?,
                    updated_at=?,closed_at=CASE
                        WHEN ? IN ('CLOSED','DUST') THEN ? ELSE closed_at END
                WHERE position_id=?
                """,
                (
                    canonical_decimal(remaining), canonical_decimal(max(Decimal("0"), remaining-dust)),
                    canonical_decimal(dust), canonical_decimal(exit_value),
                    canonical_decimal(exit_fees), canonical_decimal(pnl), position_state,
                    stop_stage, position_state, 1 if keep_active else 0,
                    position_state, purpose, 1 if remaining > 0 else 0, purpose, book_hash,
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
                    final_state, canonical_decimal(target_filled),
                    canonical_decimal(
                        target_notional / target_filled
                        if target_filled > 0 else Decimal("0")
                    ),
                    canonical_decimal(target_fees), canonical_decimal(remaining),
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
            if str(row["state"]) in {"RESOLVED_LOSER", "RESOLVED_WINNER", "REDEEM_PENDING", "REDEEMED"}:
                conn.rollback()
                return row_to_dict(row) or {}
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
            conn.execute(
                """
                UPDATE live_strategy_deals SET state=?,realized_pnl_text=?,
                    final_reason='MARKET_RESOLUTION',
                    closed_at=CASE WHEN ?='RESOLVED_LOSER' THEN ? ELSE closed_at END,
                    updated_at=? WHERE event_id=?
                """,
                (state, canonical_decimal(pnl), state, ts, ts, row["event_id"]),
            )
            if not winner:
                day_key = datetime.now(ZoneInfo("Asia/Jerusalem")).date().isoformat()
                conn.execute(
                    """
                    INSERT INTO live_daily_limits(day_key,timezone,created_at,updated_at)
                    VALUES(?,'Asia/Jerusalem',?,?)
                    ON CONFLICT(day_key) DO NOTHING
                    """,
                    (day_key, ts, ts),
                )
                conn.execute(
                    """
                    UPDATE live_daily_limits SET
                        realized_pnl_usd=realized_pnl_usd+?,
                        consecutive_losing_deals=consecutive_losing_deals+1,
                        updated_at=? WHERE day_key=?
                    """,
                    (canonical_decimal(pnl), ts, day_key),
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

    def matched_exit_attempt_evidence(
        self, intent_id: str
    ) -> dict[str, Any] | None:
        with self.base.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_order_attempts WHERE intent_id=? "
                "AND operation='CREATE_ORDER' AND phase='RESULT' "
                "AND success=1 ORDER BY occurred_at DESC,record_id DESC LIMIT 1",
                (str(intent_id),),
            ).fetchone()
        item = row_to_dict(row)
        if not item:
            return None
        for key in ("normalized_json", "response_json", "request_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(
                    item.get(key) or "{}"
                )
            except (TypeError, ValueError):
                item[key.removesuffix("_json")] = {}
        return item

    def record_authoritative_auto_repair(
        self,
        *,
        actor: str,
        position_id: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
    ) -> int:
        ts = now_iso()
        details = sanitize({
            "position_id": position_id,
            "before": before,
            "after": after,
            "authoritative_evidence": evidence,
        })
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO live_audit_log "
                "(occurred_at,actor,action,status,reason,details_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    ts, actor, "authoritative_exit_auto_repair", "ok", reason,
                    json.dumps(details, sort_keys=True),
                ),
            )
            count_24h = int(conn.execute(
                "SELECT COUNT(*) FROM live_audit_log "
                "WHERE action='authoritative_exit_auto_repair' "
                "AND occurred_at>=?",
                (
                    (datetime.now(timezone.utc).replace(microsecond=0)
                     - timedelta(days=1)).isoformat(),
                ),
            ).fetchone()[0])
            self.base.set_states_on_connection(conn, {
                "auto_repair_last_at": ts,
                "auto_repair_count_24h": str(count_24h),
            }, actor)
            audit_id = int(cursor.lastrowid)
            conn.commit()
        return audit_id

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
                local_sellable = (
                    decimal_value(existing["sellable_shares_text"])
                    or Decimal("0")
                )

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
                            dust_shares_text='0',average_entry_price_text=?,
                            state=CASE
                                WHEN state='QUARANTINED' THEN 'QUARANTINED'
                                WHEN stop_stage>=1 THEN 'EXITING'
                                ELSE 'OPEN'
                            END,
                            updated_at=?
                        WHERE position_id=?
                        """,
                        (
                            canonical_decimal(acquired),canonical_decimal(remote_shares),
                            canonical_decimal(remote_shares),canonical_decimal(average_price),
                            ts,position_id,
                        ),
                    )
                elif local_sellable != remote_shares:
                    # Same financial position, but remote account truth now
                    # confirms that the tokens are visible for SELL.
                    conn.execute(
                        """
                        UPDATE live_strategy_positions
                        SET sellable_shares_text=?,
                            updated_at=?
                        WHERE position_id=?
                        """,
                        (
                            canonical_decimal(remote_shares),
                            ts,
                            position_id,
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

    def set_reconciliation_state(
        self,
        *,
        ready: bool,
        reason: str,
        actor: str,
        auto_recoverable: bool = False,
        run_id: str | int | None = None,
        finished_at: str | None = None,
    ) -> None:
        completed_at = finished_at or now_iso()
        if not ready:
            self.acquire_pause(
                actor=actor,
                reason=reason,
                owner="RECONCILIATION",
                source_reconciliation_run_id=run_id,
            )

        values = {
            "reconciliation_readiness": "READY" if ready else "NOT_READY",
            "reconciliation_block_reason": "" if ready else reason,
            "live_blocked_by_reconciliation": "false" if ready else "true",
            "last_reconciliation_run_id": str(run_id or ""),
            "last_reconciliation_finished_at": completed_at,
        }
        if ready:
            values["last_successful_reconciliation_at"] = completed_at

        # Recovery sees all reconciliation flags and evidence as one snapshot.
        with self.base.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.base.set_states_on_connection(conn, values, actor)
            conn.commit()

        if ready and run_id is not None:
            self.promote_repairable_pause(
                actor=actor,
                reconciliation_run_id=run_id,
                clean_finished_at=completed_at,
            )

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

    def entry_blocking_intents(self) -> list[dict[str, Any]]:
        with self.base.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.* FROM live_strategy_intents AS i
                LEFT JOIN live_strategy_positions AS p
                  ON p.position_id=i.position_id
                WHERE i.state NOT IN (
                    'FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED',
                    'FAILED','SETTLED','REDEEMED'
                )
                  AND COALESCE(p.state,'') != 'QUARANTINED'
                  AND NOT EXISTS (
                    SELECT 1 FROM live_quarantines AS q
                    WHERE q.status='OPEN'
                      AND (
                        (q.event_id IS NOT NULL AND q.event_id=i.event_id)
                        OR (q.token_id IS NOT NULL AND q.token_id=i.token_id)
                      )
                  )
                ORDER BY i.created_at
                """
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def fast_reconciliation_intents(self) -> list[dict[str, Any]]:
        return self.entry_blocking_intents()

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
                "SELECT id,active FROM live_alerts WHERE fingerprint=? ORDER BY active DESC,id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE live_alerts SET last_seen_at=?,occurrence_count=occurrence_count+1,
                        severity=?,message=?,active=1,acknowledged_at=NULL,acknowledged_by=NULL
                    WHERE id=?
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
            "pause_owner": self.base.get_state("pause_owner", "NONE"),
            "pause_reason": self.base.get_state("pause_reason", ""),
            "pause_auto_recoverable": self.base.get_state(
                "pause_auto_recoverable", "false"
            ).lower() == "true",
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
