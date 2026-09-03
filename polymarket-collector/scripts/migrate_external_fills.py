#!/usr/bin/env python3
"""Create the analytics-only ``external_fills`` tables on an explicitly selected DB.

This is a standalone, additive, idempotent migration. It is deliberately NOT
folded into ``live.dashboard_schema.migrate_dashboard_schema`` because that runs
on every trader start and drives the ``live_schema_migrations`` version chain --
and the trader never reads ``external_fills``.

Safe to run while the trader is live: only ``CREATE TABLE / INDEX IF NOT EXISTS``,
no ALTERs, no triggers, no ``live_schema_migrations`` write, no ``journal_mode``
change. Under WAL the brief exclusive lock for ``CREATE TABLE`` is retried for the
connection's ``busy_timeout`` (30 s).

Read-only against everything except the two new tables.

Usage:
    python -m scripts.migrate_external_fills --db /opt/polymarket-btc-live/poly_live.sqlite3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.repository import LiveRepository  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS external_fills (
    trade_id          TEXT PRIMARY KEY,
    condition_id      TEXT NOT NULL,
    token_id          TEXT,
    event_slug        TEXT,
    market_title      TEXT,
    side              TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    outcome           TEXT,
    price             REAL NOT NULL,
    size              REAL NOT NULL,
    notional_usd      REAL NOT NULL,
    fee_usd           REAL,
    fee_rate_bps      REAL,
    trader_side       TEXT CHECK (trader_side IN ('TAKER','MAKER')),
    transaction_hash  TEXT,
    matched_at        TEXT NOT NULL,
    status            TEXT,
    owner_hash        TEXT,
    raw_json          TEXT NOT NULL,
    ingested_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_fills_matched_at ON external_fills(matched_at);
CREATE INDEX IF NOT EXISTS idx_external_fills_condition  ON external_fills(condition_id, matched_at);
CREATE INDEX IF NOT EXISTS idx_external_fills_event_slug ON external_fills(event_slug);

CREATE TABLE IF NOT EXISTS external_fills_sync (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("LIVE_DB_PATH") or "/opt/polymarket-btc-live/poly_live.sqlite3"),
    )
    args = parser.parse_args()
    target = args.db.resolve()
    if not target.is_file():
        parser.error(f"database path must name an existing file: {target}")

    repo = LiveRepository(target)  # read-write; busy_timeout=30000 is set by connect()
    with repo.connect() as conn:
        conn.executescript(DDL)
        conn.commit()
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'external_fills%' ORDER BY name"
            ).fetchall()
        ]

    print(json.dumps({"status": "ok", "database": str(target), "tables": tables}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
