from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .repository import LiveRepository, now_iso

SCHEMA_VERSION = 4
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProvenanceContext:
    execution_mode: str
    environment: str
    run_id: str
    strategy_id: str
    strategy_version: str
    source: str
    cutover_at: str


# table -> (primary key, best available original timestamp, verification expression)
PROVENANCE_TABLES: dict[str, tuple[str, str, str]] = {
    "live_orders": ("local_order_id", "created_at", "'PENDING'"),
    "live_order_fills": (
        "id", "matched_at",
        "CASE WHEN polymarket_trade_id IS NOT NULL THEN 'VERIFIED' ELSE 'PENDING' END",
    ),
    "live_positions": ("id", "created_at", "'PENDING'"),
    "live_account_snapshots": (
        "id", "sampled_at",
        "CASE WHEN account_identity_status='VERIFIED' AND status='ok' THEN 'VERIFIED' ELSE 'PARTIAL' END",
    ),
    "live_websocket_events": (
        "id", "event_timestamp",
        "CASE WHEN polymarket_trade_id IS NOT NULL OR polymarket_order_id IS NOT NULL THEN 'OBSERVED' ELSE 'PENDING' END",
    ),
    "live_market_snapshots": ("id", "market_timestamp", "'VERIFIED'"),
    "live_markets": ("id", "market_timestamp", "'VERIFIED'"),
    "live_event_states": ("event_id", "locked_at", "'PENDING'"),
    "live_strategy_intents": ("intent_id", "created_at", "'PENDING'"),
    "live_order_attempts": ("record_id", "occurred_at", "'PENDING'"),
    "live_strategy_fills": (
        "fill_id", "matched_at",
        "CASE WHEN remote_trade_id IS NOT NULL THEN 'VERIFIED' ELSE 'PENDING' END",
    ),
    "live_strategy_positions": ("position_id", "created_at", "'DERIVED'"),
    "live_strategy_deals": ("deal_id", "created_at", "'DERIVED'"),
    "live_audit_timeline": ("id", "occurred_at", "'OBSERVED'"),
}

PROVENANCE_COLUMNS: dict[str, str] = {
    "execution_mode": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "environment": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "run_id": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "strategy_id": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "strategy_version": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "provenance_source": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "source_timestamp": "TEXT",
    "ingested_at": "TEXT",
    "reconciliation_status": "TEXT NOT NULL DEFAULT 'NOT_RECONCILED'",
    "verification_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
}


def _safe_name(value: str, *, fallback: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned if cleaned else fallback


def _context(config: Any, *, cutover_at: str | None, environment: str | None, run_id: str | None) -> ProvenanceContext:
    return ProvenanceContext(
        execution_mode=_safe_name(getattr(config, "execution_mode", ""), fallback=UNKNOWN).upper(),
        environment=_safe_name(environment or getattr(config, "environment", ""), fallback="LIVE").upper(),
        run_id=_safe_name(run_id, fallback=uuid.uuid4().hex),
        strategy_id=_safe_name(getattr(config, "strategy_id", ""), fallback="btc-updown-5m"),
        strategy_version=_safe_name(getattr(config, "strategy_version", ""), fallback="strategy-v1"),
        source=_safe_name(getattr(config, "provenance_source", ""), fallback="TRADER"),
        cutover_at=cutover_at or now_iso(),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _state_value_sql(key: str, fallback: str = UNKNOWN) -> str:
    safe_key = key.replace("'", "''")
    safe_fallback = fallback.replace("'", "''")
    return (
        "COALESCE(NULLIF((SELECT value FROM live_system_state "
        f"WHERE key='{safe_key}'),''),'{safe_fallback}')"
    )


def _create_trigger(
    conn: sqlite3.Connection,
    table: str,
    primary_key: str,
    source_timestamp: str,
    verification_expression: str,
) -> None:
    insert_trigger = f"trg_{table}_dashboard_provenance"
    update_trigger = f"trg_{table}_dashboard_provenance_update"
    conn.execute(f"DROP TRIGGER IF EXISTS {insert_trigger}")
    conn.execute(f"DROP TRIGGER IF EXISTS {update_trigger}")
    assignment = f"""
                execution_mode={_state_value_sql('provenance_execution_mode')},
                environment={_state_value_sql('provenance_environment')},
                run_id={_state_value_sql('provenance_run_id')},
                strategy_id={_state_value_sql('provenance_strategy_id')},
                strategy_version={_state_value_sql('provenance_strategy_version')},
                provenance_source={_state_value_sql('provenance_source')},
                source_timestamp=COALESCE(NEW.source_timestamp, NEW.{source_timestamp}),
                ingested_at=COALESCE(NEW.ingested_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                reconciliation_status=CASE
                    WHEN NEW.reconciliation_status='NOT_RECONCILED' THEN 'PENDING'
                    ELSE NEW.reconciliation_status END,
                verification_status=CASE
                    WHEN NEW.verification_status='UNKNOWN' THEN {verification_expression}
                    ELSE NEW.verification_status END
    """
    conn.executescript(
        f"""
        CREATE TRIGGER {insert_trigger}
        AFTER INSERT ON {table}
        WHEN NEW.execution_mode='UNKNOWN'
          OR NEW.environment='UNKNOWN'
          OR NEW.run_id='UNKNOWN'
        BEGIN
            UPDATE {table}
            SET execution_mode={_state_value_sql('provenance_execution_mode')},
                environment={_state_value_sql('provenance_environment')},
                run_id={_state_value_sql('provenance_run_id')},
                strategy_id={_state_value_sql('provenance_strategy_id')},
                strategy_version={_state_value_sql('provenance_strategy_version')},
                provenance_source={_state_value_sql('provenance_source')},
                source_timestamp=COALESCE(NEW.source_timestamp, NEW.{source_timestamp}),
                ingested_at=COALESCE(NEW.ingested_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                reconciliation_status=CASE
                    WHEN NEW.reconciliation_status='NOT_RECONCILED' THEN 'PENDING'
                    ELSE NEW.reconciliation_status END,
                verification_status=CASE
                    WHEN NEW.verification_status='UNKNOWN' THEN {verification_expression}
                    ELSE NEW.verification_status END
            WHERE {primary_key}=NEW.{primary_key};
        END;
        """
    )
    conn.executescript(
        f"""
        CREATE TRIGGER {update_trigger}
        AFTER UPDATE ON {table}
        WHEN NEW.execution_mode='UNKNOWN'
          OR NEW.environment='UNKNOWN'
          OR NEW.run_id='UNKNOWN'
        BEGIN
            UPDATE {table}
            SET {assignment}
            WHERE {primary_key}=NEW.{primary_key};
        END;
        """
    )


def migrate_dashboard_schema(
    repo: LiveRepository,
    config: Any,
    *,
    cutover_at: str | None = None,
    environment: str | None = None,
    run_id: str | None = None,
) -> ProvenanceContext:
    """Apply the additive dashboard/provenance schema without classifying legacy rows.

    Existing rows retain UNKNOWN provenance. Triggers populate context only for future
    inserts. The migration is intentionally additive and idempotent.
    """
    if repo.query_only:
        raise RuntimeError("query-only repository cannot run dashboard migrations")
    context = _context(config, cutover_at=cutover_at, environment=environment, run_id=run_id)
    with repo.connect() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_dashboard_cutovers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment TEXT NOT NULL UNIQUE,
                cutover_at TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                run_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_dashboard_history_verification (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                evidence_source TEXT NOT NULL,
                evidence_hash TEXT,
                reason TEXT,
                verified_at TEXT NOT NULL,
                PRIMARY KEY(entity_type, entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_history_verification_status
            ON live_dashboard_history_verification(entity_type, verification_status, verified_at);
            """
        )
        conn.execute(
            """
            INSERT INTO live_dashboard_cutovers(
                environment,cutover_at,execution_mode,run_id,strategy_id,
                strategy_version,source,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(environment) DO NOTHING
            """,
            (
                context.environment, context.cutover_at, context.execution_mode,
                context.run_id, context.strategy_id, context.strategy_version,
                context.source, now_iso(),
            ),
        )
        stored = conn.execute(
            "SELECT * FROM live_dashboard_cutovers WHERE environment=?",
            (context.environment,),
        ).fetchone()
        if stored is None:
            raise RuntimeError("dashboard cutover row was not created")
        context = ProvenanceContext(
            execution_mode=str(stored["execution_mode"]),
            environment=str(stored["environment"]),
            run_id=str(stored["run_id"]),
            strategy_id=str(stored["strategy_id"]),
            strategy_version=str(stored["strategy_version"]),
            source=str(stored["source"]),
            cutover_at=str(stored["cutover_at"]),
        )
        state = {
            "provenance_execution_mode": context.execution_mode,
            "provenance_environment": context.environment,
            "provenance_run_id": context.run_id,
            "provenance_strategy_id": context.strategy_id,
            "provenance_strategy_version": context.strategy_version,
            "provenance_source": context.source,
            "dashboard_cutover_at": context.cutover_at,
            "dashboard_schema_version": str(SCHEMA_VERSION),
        }
        for key, value in state.items():
            conn.execute(
                """
                INSERT INTO live_system_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (key, value, now_iso()),
            )
        existing_tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, (primary_key, source_timestamp, verification_expression) in PROVENANCE_TABLES.items():
            if table not in existing_tables:
                continue
            columns = _table_columns(conn, table)
            for column, definition in PROVENANCE_COLUMNS.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            _create_trigger(conn, table, primary_key, source_timestamp, verification_expression)
        for table in ("live_order_fills", "live_strategy_fills", "live_strategy_deals"):
            if table not in existing_tables:
                continue
            columns = _table_columns(conn, table)
            if "fee_verification_status" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN fee_verification_status TEXT NOT NULL DEFAULT 'UNKNOWN'")
            if "fee_source" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN fee_source TEXT")
        for table in (
            "live_orders", "live_order_fills", "live_account_snapshots",
            "live_strategy_intents", "live_strategy_fills",
            "live_strategy_positions", "live_strategy_deals", "live_audit_timeline",
        ):
            if table in existing_tables:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_dashboard_provenance "
                    f"ON {table}(environment,execution_mode,verification_status,ingested_at)"
                )
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(1,'dashboard_provenance_v1',?,'dashboard-provenance-v1')
            ON CONFLICT(version) DO NOTHING
            """,
            (now_iso(),),
        )
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(2,'dashboard_provenance_update_triggers_v2',?,'dashboard-provenance-update-triggers-v2')
            ON CONFLICT(version) DO NOTHING
            """,
            (now_iso(),),
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_markets_dashboard_updated ON live_markets(updated_at DESC, event_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_alerts_dashboard_active ON live_alerts(active, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_reconciliation_dashboard_status ON live_reconciliation_runs(status, id DESC)")
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(3,'dashboard_fee_verification_v3',?,'dashboard-fee-verification-v3')
            ON CONFLICT(version) DO NOTHING
            """,
            (now_iso(),),
        )
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(4,'dashboard_query_indexes_v4',?,'dashboard-query-indexes-v4')
            ON CONFLICT(version) DO NOTHING
            """,
            (now_iso(),),
        )
        conn.commit()
    return context


def mark_reconciled_provenance(repo: LiveRepository) -> dict[str, int]:
    """Promote only post-cutover rows backed by remote identifiers/reconciliation."""
    if repo.query_only:
        raise RuntimeError("query-only repository cannot mark reconciliation provenance")
    cutover = repo.get_state("dashboard_cutover_at", "")
    if not cutover:
        return {}
    counts: dict[str, int] = {}
    with repo.connect() as conn:
        updates = {
            "account_snapshots": (
                """UPDATE live_account_snapshots SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,sampled_at)>=? AND status='ok'
                     AND account_identity_status='VERIFIED'""",
                (cutover,),
            ),
            "intents": (
                """UPDATE live_strategy_intents SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,created_at)>=? AND remote_order_id IS NOT NULL""",
                (cutover,),
            ),
            "fills": (
                """UPDATE live_strategy_fills SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,created_at)>=? AND remote_trade_id IS NOT NULL""",
                (cutover,),
            ),
            "positions": (
                """UPDATE live_strategy_positions AS p
                   SET reconciliation_status='RECONCILED',verification_status='DERIVED_VERIFIED'
                   WHERE p.environment='LIVE' AND p.execution_mode='REAL_TRADING'
                     AND COALESCE(p.ingested_at,p.created_at)>=?
                     AND EXISTS (
                         SELECT 1 FROM live_strategy_intents i
                         JOIN live_strategy_fills f ON f.intent_id=i.intent_id
                         WHERE i.event_id=p.event_id AND f.verification_status='VERIFIED'
                     )""",
                (cutover,),
            ),
            "deals": (
                """UPDATE live_strategy_deals AS d
                   SET reconciliation_status='RECONCILED',verification_status='DERIVED_VERIFIED'
                   WHERE d.environment='LIVE' AND d.execution_mode='REAL_TRADING'
                     AND COALESCE(d.ingested_at,d.created_at)>=?
                     AND EXISTS (
                         SELECT 1 FROM live_strategy_intents i
                         JOIN live_strategy_fills f ON f.intent_id=i.intent_id
                         WHERE i.event_id=d.event_id AND f.verification_status='VERIFIED'
                     )""",
                (cutover,),
            ),
        }
        for name, (sql, params) in updates.items():
            cursor = conn.execute(sql, params)
            counts[name] = max(0, int(cursor.rowcount))
        conn.commit()
    return counts
