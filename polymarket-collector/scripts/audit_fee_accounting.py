#!/usr/bin/env python3
"""Read-only audit of historical fee accounting (P1).

Finds persisted fills recorded as ``fee=0`` / ``fee_verification_status=VERIFIED``
that occurred in a fee-enabled market as a TAKER trade -- i.e. the case the
incident proved wrong. Reports the count and the deterministic correction that
``live/fee_accounting.py`` would now compute. It DOES NOT modify the database.

    python scripts/audit_fee_accounting.py [--db PATH] [--json]

An ``--apply`` mode exists but is deliberately gated behind an explicit
operator confirmation token and is out of scope for the P0 change: run the
audit, review, and apply as a separate approved migration.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.fee_accounting import resolve_trade_fee  # noqa: E402
from live.order_book import decimal_value  # noqa: E402


def _market_for_condition(conn: sqlite3.Connection, condition_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM live_markets WHERE condition_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (condition_id,),
    ).fetchone()
    return dict(row) if row is not None else {}


def audit(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT f.fill_id, f.intent_id, f.shares_text, f.price_text, f.fee_text,
               f.fee_verification_status, f.fee_source, f.raw_json,
               i.condition_id, i.action, i.purpose
        FROM live_strategy_fills f
        JOIN live_strategy_intents i ON i.intent_id = f.intent_id
        ORDER BY f.created_at
        """
    ).fetchall()

    affected: list[dict] = []
    total_estimated_correction = Decimal("0")
    for row in rows:
        fee = decimal_value(row["fee_text"]) or Decimal("0")
        status = str(row["fee_verification_status"] or "").upper()
        if fee != 0 or status != "VERIFIED":
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        market = _market_for_condition(conn, str(row["condition_id"] or ""))
        trade = {
            "price": row["price_text"],
            "size": row["shares_text"],
            "fee_rate_bps": raw.get("fee_rate_bps"),
            "liquidity_role": (
                raw.get("trader_side") or raw.get("liquidity_role")
            ),
            "raw_message": raw,
        }
        new_fee, new_status, new_source = resolve_trade_fee(trade, market)
        if new_status == "VERIFIED" and new_fee == 0:
            continue  # genuinely a zero-fee market
        if new_fee <= 0:
            # UNKNOWN with zero amount: flag but no numeric correction.
            affected.append({
                "fill_id": row["fill_id"], "intent_id": row["intent_id"],
                "condition_id": row["condition_id"],
                "old_fee": "0", "old_status": status,
                "new_status": new_status, "new_fee": "unknown",
            })
            continue
        total_estimated_correction += new_fee
        affected.append({
            "fill_id": row["fill_id"], "intent_id": row["intent_id"],
            "condition_id": row["condition_id"],
            "old_fee": "0", "old_status": status,
            "new_status": new_status, "new_source": new_source,
            "new_fee": str(new_fee),
        })
    conn.close()
    return {
        "db_path": db_path,
        "fills_scanned": len(rows),
        "affected_rows": len(affected),
        "estimated_total_fee_correction_usd": str(total_estimated_correction),
        "rows": affected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "LIVE_DB_PATH", "/opt/polymarket-btc-live/poly_live.sqlite3"
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--apply", action="store_true",
        help="disabled without POLY_FEE_AUDIT_APPLY_CONFIRM=<YYYY-MM-DD>",
    )
    args = parser.parse_args()

    if args.apply:
        print(
            "refusing --apply: historical fee correction is a separate approved "
            "migration, not part of this audit tool",
            file=sys.stderr,
        )
        return 2

    result = audit(args.db)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"db:                {result['db_path']}")
        print(f"fills scanned:     {result['fills_scanned']}")
        print(f"affected rows:     {result['affected_rows']}")
        print(
            "est. total fee correction (USD): "
            f"{result['estimated_total_fee_correction_usd']}"
        )
        for row in result["rows"][:50]:
            print(
                f"  {row['fill_id']}  cond={row['condition_id']}  "
                f"{row['old_fee']}/{row['old_status']} -> "
                f"{row['new_fee']}/{row['new_status']}"
            )
        if result["affected_rows"] > 50:
            print(f"  ... and {result['affected_rows'] - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
