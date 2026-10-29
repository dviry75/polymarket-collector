"""Versioned DEMO schema, applied transactionally without executescript commits."""

import hashlib
from datetime import datetime, timezone

from ..domain.models import market_definitions


SCHEMA = (
    """CREATE TABLE demo_v2_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """INSERT INTO demo_v2_meta VALUES ('generation','0'),('resetting','0'),('session_version','1')""",
    """CREATE TABLE demo_v2_definitions (
        id TEXT PRIMARY KEY, asset TEXT NOT NULL, timeframe TEXT NOT NULL,
        family_id TEXT NOT NULL, enabled INTEGER NOT NULL, duration_seconds INTEGER NOT NULL,
        provenance_json TEXT NOT NULL)""",
    """CREATE TABLE demo_v2_instances (
        id TEXT PRIMARY KEY, definition_id TEXT NOT NULL REFERENCES demo_v2_definitions(id),
        external_id TEXT NOT NULL UNIQUE, condition_id TEXT NOT NULL UNIQUE,
        up_token_id TEXT NOT NULL UNIQUE, down_token_id TEXT NOT NULL UNIQUE,
        slug TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
        discovered_at TEXT NOT NULL, metadata_source TEXT NOT NULL, verified INTEGER NOT NULL,
        status TEXT NOT NULL, resolution_status TEXT NOT NULL, winner_token_id TEXT,
        resolution_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
        CHECK(up_token_id != down_token_id), CHECK(ends_at > starts_at))""",
    """CREATE INDEX demo_v2_instances_family_time ON demo_v2_instances(definition_id,starts_at,ends_at)""",
    """CREATE TABLE demo_v2_rules (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL, archived INTEGER NOT NULL,
        current_version_id TEXT REFERENCES demo_v2_rule_versions(id), created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, CHECK(NOT (enabled=1 AND archived=1)))""",
    """CREATE TABLE demo_v2_rule_versions (
        id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES demo_v2_rules(id),
        version INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(rule_id,version))""",
    """CREATE TABLE demo_v2_entry_counts (
        rule_id TEXT NOT NULL REFERENCES demo_v2_rules(id),
        instance_id TEXT NOT NULL REFERENCES demo_v2_instances(id), count INTEGER NOT NULL CHECK(count>=0),
        PRIMARY KEY(rule_id,instance_id))""",
    """CREATE TABLE demo_v2_entry_runtime (
        rule_id TEXT NOT NULL REFERENCES demo_v2_rules(id),
        instance_id TEXT NOT NULL REFERENCES demo_v2_instances(id), token_id TEXT NOT NULL,
        version_id TEXT NOT NULL REFERENCES demo_v2_rule_versions(id),
        last_ask_text TEXT, generation INTEGER NOT NULL, sequence INTEGER NOT NULL,
        observed_at TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(rule_id,instance_id,token_id))""",
    """CREATE TABLE demo_v2_positions (
        id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        rule_id TEXT NOT NULL REFERENCES demo_v2_rules(id),
        rule_version_id TEXT NOT NULL REFERENCES demo_v2_rule_versions(id),
        instance_id TEXT NOT NULL REFERENCES demo_v2_instances(id), token_id TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('UP','DOWN')), status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
        entry_trigger_text TEXT NOT NULL, entry_price_text TEXT NOT NULL, entry_at TEXT NOT NULL,
        generation INTEGER NOT NULL, sequence INTEGER NOT NULL, entry_source TEXT NOT NULL,
        gross_shares_text TEXT NOT NULL, buy_fee_text TEXT NOT NULL, buy_fee_shares_text TEXT NOT NULL,
        net_shares_text TEXT NOT NULL, entry_notional_text TEXT NOT NULL, cash_debit_text TEXT NOT NULL,
        tp_text TEXT, sl_text TEXT, entry_provenance_json TEXT NOT NULL, fee_evidence_json TEXT NOT NULL,
        exit_at TEXT, exit_price_text TEXT, exit_reason TEXT, exit_provenance_json TEXT,
        sell_fee_text TEXT, gross_proceeds_text TEXT, net_proceeds_text TEXT, net_pnl_text TEXT,
        result TEXT NOT NULL DEFAULT 'OPEN', CHECK(result IN ('OPEN','POSITIVE','NEGATIVE','BREAK_EVEN')))""",
    """CREATE INDEX demo_v2_positions_open_token ON demo_v2_positions(status,token_id)""",
    """CREATE INDEX demo_v2_positions_rule_time ON demo_v2_positions(rule_id,entry_at)""",
    """CREATE TABLE demo_v2_ledger (
        id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
        rule_id TEXT REFERENCES demo_v2_rules(id), position_id TEXT REFERENCES demo_v2_positions(id),
        movement_type TEXT NOT NULL, amount_text TEXT NOT NULL, reference TEXT NOT NULL UNIQUE,
        provenance_json TEXT NOT NULL)""",
    """CREATE INDEX demo_v2_ledger_rule ON demo_v2_ledger(rule_id,occurred_at)""",
    """CREATE TABLE demo_v2_underlying (
        id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES demo_v2_instances(id),
        position_id TEXT REFERENCES demo_v2_positions(id), context TEXT NOT NULL,
        asset TEXT NOT NULL, price_text TEXT NOT NULL, observed_at TEXT NOT NULL,
        source TEXT NOT NULL, reference_id TEXT NOT NULL, provenance_json TEXT NOT NULL)""",
    """CREATE TABLE demo_v2_fee_evidence (
        instance_id TEXT PRIMARY KEY REFERENCES demo_v2_instances(id), state TEXT NOT NULL,
        observed_at TEXT NOT NULL, source TEXT NOT NULL, evidence_json TEXT NOT NULL)""",
    """CREATE TABLE demo_v2_incidents (
        id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, component TEXT NOT NULL,
        code TEXT NOT NULL, details_json TEXT NOT NULL)""",
    """CREATE TRIGGER demo_v2_versions_immutable BEFORE UPDATE ON demo_v2_rule_versions
        BEGIN SELECT RAISE(ABORT,'rule versions are immutable'); END""",
    """CREATE TRIGGER demo_v2_versions_delete_guard BEFORE DELETE ON demo_v2_rule_versions
        WHEN (SELECT value FROM demo_v2_meta WHERE key='resetting')!='1'
        BEGIN SELECT RAISE(ABORT,'rule versions cannot be deleted'); END""",
    """CREATE TRIGGER demo_v2_ledger_immutable BEFORE UPDATE ON demo_v2_ledger
        BEGIN SELECT RAISE(ABORT,'ledger is immutable'); END""",
    """CREATE TRIGGER demo_v2_ledger_delete_guard BEFORE DELETE ON demo_v2_ledger
        WHEN (SELECT value FROM demo_v2_meta WHERE key='resetting')!='1'
        BEGIN SELECT RAISE(ABORT,'ledger cannot be deleted'); END""",
)


def migrate(database) -> None:
    checksum = hashlib.sha256("\n".join(SCHEMA).encode()).hexdigest()
    with database.connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS demo_v2_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
            rows = conn.execute("SELECT * FROM demo_v2_schema ORDER BY version").fetchall()
            if rows and (len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != checksum):
                raise ValueError("unsupported or modified DEMO migration history")
            if not rows:
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO demo_v2_schema VALUES (1,?,?)", (checksum, datetime.now(timezone.utc).isoformat()))
                for definition in market_definitions():
                    conn.execute("INSERT INTO demo_v2_definitions VALUES (?,?,?,?,?,?,?)", (
                        definition.key, definition.asset.value, definition.timeframe.value,
                        definition.family_id, int(definition.enabled), definition.expected_duration,
                        '{"source":"SOURCE_OF_TRUTH","provider_verified":false}',
                    ))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
