#!/usr/bin/env python3
"""One-time correction: Polymarket's public trade-history API (`/data/trades`)
returns ``fee_rate_bps=0`` for every fill on this account/market, so every row
ingested so far has ``fee_usd=0`` -- understating fees (and therefore the
"תזרים" cash-flow column) across the board.

The internal strategy pipeline records the REAL fee per fill
(``live_strategy_fills.fee_text``) and every external fill is that same
account's own trade, matchable 1:1 by ``external_fills.trade_id ==
live_strategy_fills.remote_trade_id``. This script overwrites ``fee_usd`` (and
clears the misleading ``fee_rate_bps``) with that authoritative value wherever
a match exists. Idempotent -- safe to re-run; only touches rows whose fee
actually changes.

Usage:
    python -m scripts.backfill_external_fee_usd [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.repository import LiveRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("LIVE_DB_PATH") or "/opt/polymarket-btc-live/poly_live.sqlite3")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    repo = LiveRepository(Path(args.db))  # RW
    with repo.connect() as conn:
        rows = conn.execute(
            """
            SELECT ef.trade_id AS trade_id, ef.fee_usd AS old_fee, f.fee_text AS new_fee_text
            FROM external_fills ef
            JOIN live_strategy_fills f ON f.remote_trade_id = ef.trade_id
            WHERE f.fee_text IS NOT NULL
            """
        ).fetchall()

        changed = []
        for row in rows:
            try:
                new_fee = round(float(row["new_fee_text"]), 6)
            except (TypeError, ValueError):
                continue
            old_fee = row["old_fee"]
            if old_fee is None or abs(float(old_fee) - new_fee) > 1e-9:
                changed.append((row["trade_id"], old_fee, new_fee))

        delta_total = sum((c[2] - (c[1] or 0.0)) for c in changed)
        print(json.dumps({
            "matched_rows": len(rows),
            "rows_to_update": len(changed),
            "fee_usd_delta_total": round(delta_total, 4),
            "dry_run": args.dry_run,
        }, sort_keys=True))

        if args.dry_run or not changed:
            return 0

        conn.executemany(
            "UPDATE external_fills SET fee_usd = ?, fee_rate_bps = NULL WHERE trade_id = ?",
            [(new_fee, trade_id) for trade_id, _old, new_fee in changed],
        )
        conn.commit()
    print(json.dumps({"status": "ok", "updated": len(changed)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
