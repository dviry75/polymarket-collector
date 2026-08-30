from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .repository import LiveRepository, now_iso

SCHEMA_VERSION = 6
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
    "live_strategy_runs": ("run_id", "started_at", "'VERIFIED'"),
    "live_position_events": ("position_event_id", "occurred_at", "'DERIVED_VERIFIED'"),
    "live_redemptions": ("redemption_id", "requested_at", "'DERIVED_VERIFIED'"),
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
    rotate_runtime_run: bool = False,
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
            CREATE TABLE IF NOT EXISTS live_strategy_runs (
                run_id TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                execution_mode TEXT NOT NULL DEFAULT 'UNKNOWN', environment TEXT NOT NULL DEFAULT 'UNKNOWN',
                strategy_id TEXT NOT NULL DEFAULT 'UNKNOWN', strategy_version TEXT NOT NULL DEFAULT 'UNKNOWN',
                provenance_source TEXT NOT NULL DEFAULT 'UNKNOWN', source_timestamp TEXT, ingested_at TEXT,
                reconciliation_status TEXT NOT NULL DEFAULT 'NOT_RECONCILED',
                verification_status TEXT NOT NULL DEFAULT 'UNKNOWN'
            );
            CREATE TABLE IF NOT EXISTS live_position_events (
                position_event_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, event_id TEXT, event_type TEXT NOT NULL,
                previous_state TEXT, new_state TEXT, shares_text TEXT, amount_text TEXT, occurred_at TEXT NOT NULL,
                execution_mode TEXT NOT NULL DEFAULT 'UNKNOWN', environment TEXT NOT NULL DEFAULT 'UNKNOWN',
                run_id TEXT NOT NULL DEFAULT 'UNKNOWN', strategy_id TEXT NOT NULL DEFAULT 'UNKNOWN',
                strategy_version TEXT NOT NULL DEFAULT 'UNKNOWN', provenance_source TEXT NOT NULL DEFAULT 'UNKNOWN',
                source_timestamp TEXT, ingested_at TEXT, reconciliation_status TEXT NOT NULL DEFAULT 'NOT_RECONCILED',
                verification_status TEXT NOT NULL DEFAULT 'UNKNOWN'
            );
            CREATE TABLE IF NOT EXISTS live_redemptions (
                redemption_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, event_id TEXT, token_id TEXT, state TEXT NOT NULL,
                shares_text TEXT, amount_text TEXT, transaction_hash TEXT, requested_at TEXT NOT NULL, completed_at TEXT,
                execution_mode TEXT NOT NULL DEFAULT 'UNKNOWN', environment TEXT NOT NULL DEFAULT 'UNKNOWN',
                run_id TEXT NOT NULL DEFAULT 'UNKNOWN', strategy_id TEXT NOT NULL DEFAULT 'UNKNOWN',
                strategy_version TEXT NOT NULL DEFAULT 'UNKNOWN', provenance_source TEXT NOT NULL DEFAULT 'UNKNOWN',
                source_timestamp TEXT, ingested_at TEXT, reconciliation_status TEXT NOT NULL DEFAULT 'NOT_RECONCILED',
                verification_status TEXT NOT NULL DEFAULT 'UNKNOWN'
            );
            CREATE INDEX IF NOT EXISTS idx_live_strategy_runs_environment ON live_strategy_runs(environment,execution_mode,started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_live_position_events_position ON live_position_events(position_id,occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_live_redemptions_position ON live_redemptions(position_id,requested_at DESC);
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
            run_id=context.run_id if rotate_runtime_run else str(stored["run_id"]),
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
        if rotate_runtime_run:
            started_at = now_iso()
            conn.execute(
                "UPDATE live_strategy_runs SET state='INTERRUPTED',ended_at=? "
                "WHERE environment=? AND execution_mode=? AND state='RUNNING'",
                (started_at, context.environment, context.execution_mode),
            )
            conn.execute(
                """
                INSERT INTO live_strategy_runs(
                    run_id,state,started_at,execution_mode,environment,strategy_id,strategy_version,
                    provenance_source,source_timestamp,ingested_at,reconciliation_status,verification_status
                ) VALUES(?,'RUNNING',?,?,?,?,?,?,?,?,'PENDING','VERIFIED')
                """,
                (context.run_id, started_at, context.execution_mode, context.environment,
                 context.strategy_id, context.strategy_version, context.source, started_at, started_at),
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
        conn.executescript(
            f"""
            DROP TRIGGER IF EXISTS trg_live_strategy_positions_position_event_insert;
            CREATE TRIGGER trg_live_strategy_positions_position_event_insert
            AFTER INSERT ON live_strategy_positions
            BEGIN
                INSERT OR IGNORE INTO live_position_events(
                    position_event_id,position_id,event_id,event_type,new_state,shares_text,amount_text,occurred_at,
                    execution_mode,environment,run_id,strategy_id,strategy_version,provenance_source,
                    source_timestamp,ingested_at,reconciliation_status,verification_status
                ) VALUES(
                    NEW.position_id || ':create:' || NEW.created_at, NEW.position_id, NEW.event_id, 'CREATED', NEW.state,
                    NEW.remaining_shares_text,NEW.cost_all_in_text,NEW.created_at,
                    CASE WHEN NEW.execution_mode='UNKNOWN' THEN {_state_value_sql('provenance_execution_mode')} ELSE NEW.execution_mode END,
                    CASE WHEN NEW.environment='UNKNOWN' THEN {_state_value_sql('provenance_environment')} ELSE NEW.environment END,
                    CASE WHEN NEW.run_id='UNKNOWN' THEN {_state_value_sql('provenance_run_id')} ELSE NEW.run_id END,
                    CASE WHEN NEW.strategy_id='UNKNOWN' THEN {_state_value_sql('provenance_strategy_id')} ELSE NEW.strategy_id END,
                    CASE WHEN NEW.strategy_version='UNKNOWN' THEN {_state_value_sql('provenance_strategy_version')} ELSE NEW.strategy_version END,
                    CASE WHEN NEW.provenance_source='UNKNOWN' THEN {_state_value_sql('provenance_source')} ELSE NEW.provenance_source END,
                    NEW.created_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NEW.reconciliation_status,
                    CASE WHEN NEW.verification_status='UNKNOWN' THEN 'DERIVED' ELSE NEW.verification_status END
                );
            END;
            DROP TRIGGER IF EXISTS trg_live_strategy_positions_position_event_update;
            CREATE TRIGGER trg_live_strategy_positions_position_event_update
            AFTER UPDATE ON live_strategy_positions
            WHEN OLD.state IS NOT NEW.state OR OLD.remaining_shares_text IS NOT NEW.remaining_shares_text
                 OR OLD.realized_pnl_text IS NOT NEW.realized_pnl_text
            BEGIN
                INSERT OR IGNORE INTO live_position_events(
                    position_event_id,position_id,event_id,event_type,previous_state,new_state,shares_text,amount_text,occurred_at,
                    execution_mode,environment,run_id,strategy_id,strategy_version,provenance_source,source_timestamp,ingested_at,
                    reconciliation_status,verification_status
                ) VALUES(
                    NEW.position_id || ':update:' || NEW.updated_at || ':' || NEW.state,NEW.position_id,NEW.event_id,'STATE_OR_BALANCE_CHANGED',
                    OLD.state,NEW.state,NEW.remaining_shares_text,NEW.realized_pnl_text,NEW.updated_at,NEW.execution_mode,NEW.environment,
                    NEW.run_id,NEW.strategy_id,NEW.strategy_version,NEW.provenance_source,NEW.updated_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'),NEW.reconciliation_status,NEW.verification_status
                );
            END;
            DROP TRIGGER IF EXISTS trg_live_strategy_positions_redemption_insert;
            CREATE TRIGGER trg_live_strategy_positions_redemption_insert
            AFTER INSERT ON live_strategy_positions
            WHEN NEW.state IN ('REDEEM_PENDING','REDEEMED')
            BEGIN
                INSERT OR IGNORE INTO live_redemptions(
                    redemption_id,position_id,event_id,token_id,state,shares_text,amount_text,requested_at,completed_at,
                    execution_mode,environment,run_id,strategy_id,strategy_version,provenance_source,source_timestamp,ingested_at,
                    reconciliation_status,verification_status
                ) VALUES(
                    NEW.position_id || ':redemption',NEW.position_id,NEW.event_id,NEW.token_id,NEW.state,NEW.remaining_shares_text,
                    CASE WHEN NEW.resolved_winner=1 THEN NEW.remaining_shares_text ELSE NULL END,NEW.updated_at,
                    CASE WHEN NEW.state='REDEEMED' THEN NEW.updated_at ELSE NULL END,
                    CASE WHEN NEW.execution_mode='UNKNOWN' THEN {_state_value_sql('provenance_execution_mode')} ELSE NEW.execution_mode END,
                    CASE WHEN NEW.environment='UNKNOWN' THEN {_state_value_sql('provenance_environment')} ELSE NEW.environment END,
                    CASE WHEN NEW.run_id='UNKNOWN' THEN {_state_value_sql('provenance_run_id')} ELSE NEW.run_id END,
                    CASE WHEN NEW.strategy_id='UNKNOWN' THEN {_state_value_sql('provenance_strategy_id')} ELSE NEW.strategy_id END,
                    CASE WHEN NEW.strategy_version='UNKNOWN' THEN {_state_value_sql('provenance_strategy_version')} ELSE NEW.strategy_version END,
                    CASE WHEN NEW.provenance_source='UNKNOWN' THEN {_state_value_sql('provenance_source')} ELSE NEW.provenance_source END,
                    NEW.updated_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NEW.reconciliation_status,NEW.verification_status
                );
            END;
            DROP TRIGGER IF EXISTS trg_live_strategy_positions_redemption_update;
            CREATE TRIGGER trg_live_strategy_positions_redemption_update
            AFTER UPDATE OF state ON live_strategy_positions
            WHEN NEW.state IN ('REDEEM_PENDING','REDEEMED') AND OLD.state IS NOT NEW.state
            BEGIN
                INSERT INTO live_redemptions(
                    redemption_id,position_id,event_id,token_id,state,shares_text,amount_text,requested_at,completed_at,
                    execution_mode,environment,run_id,strategy_id,strategy_version,provenance_source,source_timestamp,ingested_at,
                    reconciliation_status,verification_status
                ) VALUES(
                    NEW.position_id || ':redemption',NEW.position_id,NEW.event_id,NEW.token_id,NEW.state,NEW.remaining_shares_text,
                    CASE WHEN NEW.resolved_winner=1 THEN NEW.remaining_shares_text ELSE NULL END,NEW.updated_at,
                    CASE WHEN NEW.state='REDEEMED' THEN NEW.updated_at ELSE NULL END,CASE WHEN NEW.execution_mode='UNKNOWN' THEN {_state_value_sql('provenance_execution_mode')} ELSE NEW.execution_mode END,
                    CASE WHEN NEW.environment='UNKNOWN' THEN {_state_value_sql('provenance_environment')} ELSE NEW.environment END,
                    CASE WHEN NEW.run_id='UNKNOWN' THEN {_state_value_sql('provenance_run_id')} ELSE NEW.run_id END,
                    CASE WHEN NEW.strategy_id='UNKNOWN' THEN {_state_value_sql('provenance_strategy_id')} ELSE NEW.strategy_id END,
                    CASE WHEN NEW.strategy_version='UNKNOWN' THEN {_state_value_sql('provenance_strategy_version')} ELSE NEW.strategy_version END,
                    CASE WHEN NEW.provenance_source='UNKNOWN' THEN {_state_value_sql('provenance_source')} ELSE NEW.provenance_source END,NEW.updated_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'),NEW.reconciliation_status,NEW.verification_status
                ) ON CONFLICT(redemption_id) DO UPDATE SET
                    state=excluded.state,shares_text=excluded.shares_text,amount_text=excluded.amount_text,
                    completed_at=excluded.completed_at,source_timestamp=excluded.source_timestamp,
                    ingested_at=excluded.ingested_at,reconciliation_status=excluded.reconciliation_status,
                    verification_status=excluded.verification_status;
            END;
            """
        )
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
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_cutover_validate_insert
            BEFORE INSERT ON live_dashboard_cutovers
            WHEN NEW.environment NOT IN ('LIVE','STAGING','TEST','DEMO')
              OR NEW.execution_mode NOT IN ('READ_ONLY','PAPER_TRADING','REAL_TRADING')
              OR NEW.run_id='' OR NEW.strategy_id='' OR NEW.strategy_version='' OR NEW.source=''
            BEGIN SELECT RAISE(ABORT, 'invalid dashboard cutover provenance'); END;
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_cutover_validate_update
            BEFORE UPDATE ON live_dashboard_cutovers
            WHEN NEW.environment NOT IN ('LIVE','STAGING','TEST','DEMO')
              OR NEW.execution_mode NOT IN ('READ_ONLY','PAPER_TRADING','REAL_TRADING')
              OR NEW.run_id='' OR NEW.strategy_id='' OR NEW.strategy_version='' OR NEW.source=''
            BEGIN SELECT RAISE(ABORT, 'invalid dashboard cutover provenance'); END;
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_history_validate_insert
            BEFORE INSERT ON live_dashboard_history_verification
            WHEN NEW.verification_status NOT IN ('UNKNOWN','UNVERIFIED','PENDING','OBSERVED','DERIVED','VERIFIED','RECONCILED','DERIVED_VERIFIED')
              OR NEW.evidence_source=''
            BEGIN SELECT RAISE(ABORT, 'invalid dashboard history verification'); END;
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_history_validate_update
            BEFORE UPDATE ON live_dashboard_history_verification
            WHEN NEW.verification_status NOT IN ('UNKNOWN','UNVERIFIED','PENDING','OBSERVED','DERIVED','VERIFIED','RECONCILED','DERIVED_VERIFIED')
              OR NEW.evidence_source=''
            BEGIN SELECT RAISE(ABORT, 'invalid dashboard history verification'); END;
            """
        )
        for table in PROVENANCE_TABLES:
            if table not in existing_tables:
                continue
            conn.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_dashboard_validate_insert
                BEFORE INSERT ON {table}
                WHEN NEW.execution_mode NOT IN ('UNKNOWN','READ_ONLY','PAPER_TRADING','REAL_TRADING')
                  OR NEW.environment NOT IN ('UNKNOWN','LIVE','STAGING','TEST','DEMO')
                  OR NEW.verification_status NOT IN ('UNKNOWN','UNVERIFIED','PENDING','PARTIAL','OBSERVED','DERIVED','VERIFIED','RECONCILED','DERIVED_VERIFIED')
                  OR NEW.reconciliation_status NOT IN ('NOT_RECONCILED','PENDING','RECONCILED','GAPS','FAILED')
                BEGIN SELECT RAISE(ABORT, 'invalid dashboard provenance'); END;
                CREATE TRIGGER IF NOT EXISTS trg_{table}_dashboard_validate_update
                BEFORE UPDATE ON {table}
                WHEN NEW.execution_mode NOT IN ('UNKNOWN','READ_ONLY','PAPER_TRADING','REAL_TRADING')
                  OR NEW.environment NOT IN ('UNKNOWN','LIVE','STAGING','TEST','DEMO')
                  OR NEW.verification_status NOT IN ('UNKNOWN','UNVERIFIED','PENDING','PARTIAL','OBSERVED','DERIVED','VERIFIED','RECONCILED','DERIVED_VERIFIED')
                  OR NEW.reconciliation_status NOT IN ('NOT_RECONCILED','PENDING','RECONCILED','GAPS','FAILED')
                BEGIN SELECT RAISE(ABORT, 'invalid dashboard provenance'); END;
                """
            )
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(5,'dashboard_provenance_constraints_v5',?,'dashboard-provenance-constraints-v5')
            ON CONFLICT(version) DO NOTHING
            """,
            (now_iso(),),
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_live_account_snapshots_dashboard_current
            ON live_account_snapshots(environment,execution_mode,id DESC)
            WHERE verification_status IN ('VERIFIED','RECONCILED')
              AND ingested_at IS NOT NULL
            """
        )
        conn.execute(
            """
            INSERT INTO live_schema_migrations(version,name,applied_at,checksum)
            VALUES(6,'dashboard_account_current_index_v6',?,'dashboard-account-current-index-v6')
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
    counts: dict[str, int] = {}
    with repo.connect() as conn:
        cutover_row = conn.execute(
            "SELECT value FROM live_system_state WHERE key='dashboard_cutover_at'"
        ).fetchone()
        cutover = str(cutover_row[0]) if cutover_row else ""
        if not cutover:
            return {}
        updates = {
            "account_snapshots": (
                """UPDATE live_account_snapshots SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,sampled_at)>=? AND status='ok'
                     AND account_identity_status='VERIFIED'
                     AND (reconciliation_status!='RECONCILED' OR verification_status!='VERIFIED')""",
                (cutover,),
            ),
            "intents": (
                """UPDATE live_strategy_intents SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,created_at)>=? AND remote_order_id IS NOT NULL
                     AND (reconciliation_status!='RECONCILED' OR verification_status!='VERIFIED')""",
                (cutover,),
            ),
            "fills": (
                """UPDATE live_strategy_fills SET reconciliation_status='RECONCILED',verification_status='VERIFIED'
                   WHERE environment='LIVE' AND execution_mode='REAL_TRADING'
                     AND COALESCE(ingested_at,created_at)>=? AND remote_trade_id IS NOT NULL
                     AND (reconciliation_status!='RECONCILED' OR verification_status!='VERIFIED')""",
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
                     )
                     AND (reconciliation_status!='RECONCILED' OR verification_status!='DERIVED_VERIFIED')""",
                (cutover,),
            ),
            "deals": (
                """UPDATE live_strategy_deals AS d
                   SET reconciliation_status='RECONCILED',verification_status='DERIVED_VERIFIED',
                       fee_verification_status=CASE
                           WHEN EXISTS (
                               SELECT 1 FROM live_strategy_intents fi
                               JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                               WHERE fi.event_id=d.event_id
                           ) AND NOT EXISTS (
                               SELECT 1 FROM live_strategy_intents fi
                               JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                               WHERE fi.event_id=d.event_id
                                 AND ff.fee_verification_status!='VERIFIED'
                           ) THEN 'VERIFIED' ELSE 'UNKNOWN' END,
                       fee_source=CASE
                           WHEN EXISTS (
                               SELECT 1 FROM live_strategy_intents fi
                               JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                               WHERE fi.event_id=d.event_id AND ff.fee_verification_status='VERIFIED'
                           ) THEN 'polymarket_fee_rate_bps_formula' ELSE NULL END
                   WHERE d.environment='LIVE' AND d.execution_mode='REAL_TRADING'
                     AND COALESCE(d.ingested_at,d.created_at)>=?
                     AND EXISTS (
                         SELECT 1 FROM live_strategy_intents i
                         JOIN live_strategy_fills f ON f.intent_id=i.intent_id
                         WHERE i.event_id=d.event_id AND f.verification_status='VERIFIED'
                     )
                     AND (
                         reconciliation_status!='RECONCILED'
                         OR verification_status!='DERIVED_VERIFIED'
                         OR fee_verification_status IS NOT CASE
                             WHEN EXISTS (
                                 SELECT 1 FROM live_strategy_intents fi
                                 JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                                 WHERE fi.event_id=d.event_id
                             ) AND NOT EXISTS (
                                 SELECT 1 FROM live_strategy_intents fi
                                 JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                                 WHERE fi.event_id=d.event_id
                                   AND ff.fee_verification_status!='VERIFIED'
                             ) THEN 'VERIFIED' ELSE 'UNKNOWN' END
                         OR fee_source IS NOT CASE
                             WHEN EXISTS (
                                 SELECT 1 FROM live_strategy_intents fi
                                 JOIN live_strategy_fills ff ON ff.intent_id=fi.intent_id
                                 WHERE fi.event_id=d.event_id AND ff.fee_verification_status='VERIFIED'
                             ) THEN 'polymarket_fee_rate_bps_formula' ELSE NULL END
                     )""",
                (cutover,),
            ),
        }
        for name, (sql, params) in updates.items():
            cursor = conn.execute(sql, params)
            counts[name] = max(0, int(cursor.rowcount))
        conn.commit()
    return counts
