#!/usr/bin/env python3
"""Best-effort reconstruction of ``live_strategy_exit_audit`` rows for the
stop / emergency exits that closed before the forensic instrumentation existed.

What is recoverable from history: latch time (from the timeline, biased a few
tens of ms late), submit time, realized fill VWAP, execution latency, the latch
bid, purpose, floor, outcome axis.

What is NOT recoverable: order-book depth at any decision point (only a hash was
ever stored), precise cross time / detection latency for healthy exits, and
therefore the MARKET / LIQUIDITY / BOOK_FILL_MISMATCH distinction. Every row this
writes is marked ``evidence_quality = 'RECONSTRUCTED_PARTIAL'`` so the classifier
and the dashboard can tell them apart from live-captured rows.

Read-only against every table except ``live_strategy_exit_audit`` (insert-only
here). Safe to run against the live DB; take ``--backup`` if you want a snapshot.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.config import LiveConfig
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository, stable_id

LATCH_REASONS = (
    "STOP_066_TRIGGER_LATCHED",
    "STOP_LATCHED_ON_RECOVERY",
    "ENTRY_FILL_OUTSIDE_POLICY",
)
_ACCEPT = Decimal("0.60")


def _dec(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except Exception:
        return None


def _delta_ms(start, end):
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(round((b - a).total_seconds() * 1000))


def _reconstruct(conn, repo: StrategyRepository, position: dict) -> dict | None:
    pid = position["position_id"]
    event_id = position["event_id"]

    deal = conn.execute(
        "SELECT final_reason, entry_intent_id, closed_at FROM live_strategy_deals "
        "WHERE event_id=?", (event_id,),
    ).fetchone()
    deal_reason = deal["final_reason"] if deal else None

    # Prefer a real STOP / EMERGENCY exit intent; that is the episode we can
    # actually reconstruct a submit time and latency for.
    intent = conn.execute(
        "SELECT * FROM live_strategy_intents WHERE position_id=? AND action='EXIT' "
        "AND (purpose LIKE 'STOP%' OR purpose LIKE 'EMERGENCY%') "
        "ORDER BY (state IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','SETTLED')) DESC, "
        "COALESCE(final_at, submitted_at, created_at) DESC LIMIT 1",
        (pid,),
    ).fetchone()
    if intent is None:
        # latched but no stop-purpose SELL was ever reserved (price recovered
        # and the deal closed via TP / resolution). Nothing meaningful to
        # reconstruct beyond the latch; skip.
        if deal_reason not in (
            "STOP_066", "EMERGENCY_060", "EMERGENCY_OPERATOR",
            "EMERGENCY_INVALID_ENTRY",
        ):
            return None
    intent = dict(intent) if intent else {}
    intent_id = intent.get("intent_id")
    purpose = intent.get("purpose") or deal_reason or "STOP_066"

    latch = conn.execute(
        f"SELECT occurred_at, parameters_json FROM live_audit_timeline "
        f"WHERE event_id=? AND reason_code IN ({','.join('?' * len(LATCH_REASONS))}) "
        f"ORDER BY occurred_at LIMIT 1",
        (event_id, *LATCH_REASONS),
    ).fetchone()
    params = {}
    if latch and latch["parameters_json"]:
        try:
            params = json.loads(latch["parameters_json"])
        except ValueError:
            params = {}

    submitted_at = intent.get("submitted_at")
    if not submitted_at and intent_id:
        r = conn.execute(
            "SELECT MIN(occurred_at) AS a FROM live_order_attempts "
            "WHERE intent_id=? AND operation='CREATE_ORDER'", (intent_id,),
        ).fetchone()
        submitted_at = r["a"] if r else None

    clob_status = intent.get("reason_code")
    if not clob_status and intent_id:
        r = conn.execute(
            "SELECT result_status FROM live_order_attempts WHERE intent_id=? "
            "AND phase='RESULT' ORDER BY occurred_at DESC LIMIT 1", (intent_id,),
        ).fetchone()
        clob_status = r["result_status"] if r else None

    shares = vwap = fees = None
    if intent_id:
        summ = repo.fill_summary(intent_id)
        shares, vwap, fees = summ.get("shares"), summ.get("average_price"), summ.get("fees")
    if (not shares or shares <= 0) and _dec(intent.get("filled_shares_text")):
        shares = _dec(intent.get("filled_shares_text"))
        vwap = _dec(intent.get("average_price_text"))
        fees = _dec(intent.get("fee_text"))

    first_fill = last_fill = None
    if intent_id:
        r = conn.execute(
            "SELECT MIN(matched_at) AS f, MAX(matched_at) AS l "
            "FROM live_strategy_fills WHERE intent_id=?", (intent_id,),
        ).fetchone()
        first_fill, last_fill = (r["f"], r["l"]) if r else (None, None)

    settlement_wait = conn.execute(
        "SELECT 1 FROM live_audit_timeline WHERE event_id=? AND "
        "(reason_code LIKE '%WAITING_SELLABLE%' "
        "OR reason_code LIKE '%WAITING_FOR_SELLABLE%') LIMIT 1",
        (event_id,),
    ).fetchone()

    latched_at = latch["occurred_at"] if latch else None
    has_fill = bool(vwap and _dec(vwap) and _dec(vwap) > 0)
    if not has_fill:
        outcome = "NO_EXECUTION"
    elif _dec(vwap) >= _ACCEPT:
        outcome = "ACCEPTABLE"
    else:
        outcome = "BAD_EXIT"

    # optional cross anchor from a historical SLA-breach row
    breach = conn.execute(
        "SELECT parameters_json FROM live_audit_timeline WHERE event_id=? "
        "AND reason_code='ACTIVE_POSITION_SLA_BREACH' ORDER BY occurred_at LIMIT 1",
        (event_id,),
    ).fetchone()
    cross_at = None
    if breach and breach["parameters_json"]:
        try:
            cross_at = json.loads(breach["parameters_json"]).get("stop_eligible_frame_at")
        except ValueError:
            cross_at = None

    fields = {
        "exit_purpose": purpose,
        "entry_intent_id": deal["entry_intent_id"] if deal else None,
        "exit_intent_id": intent_id,
        "min_price_floor_text": params.get("min_price") or intent.get("price_limit_text"),
        "latched_at": latched_at,
        "latch_best_bid_text": params.get("trigger_bid"),
        "latch_liquidity_hash": params.get("liquidity_hash"),
        "latch_source": "backfill",
        "cross_detected_at": cross_at,
        "submitted_at": submitted_at,
        "requested_shares_text": intent.get("requested_shares_text"),
        "clob_status": clob_status,
        "remote_order_id": intent.get("remote_order_id"),
        "actual_fill_shares_text": _fmt(shares),
        "actual_fill_vwap_text": _fmt(vwap),
        "actual_fill_fees_text": _fmt(fees),
        "first_fill_at": first_fill,
        "last_fill_at": last_fill,
        "execution_latency_ms": _delta_ms(latched_at, submitted_at),
        "detection_latency_ms": _delta_ms(cross_at, latched_at),
        "settlement_wait": 1 if settlement_wait else 0,
        "exit_outcome": outcome,
        "evidence_quality": "RECONSTRUCTED_PARTIAL",
        "deep_capture": 0,
    }
    return {k: v for k, v in fields.items() if v is not None}


def _fmt(v):
    d = _dec(v)
    return format(d, "f") if d is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Rewrite rows that already exist.")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--db-path", type=Path,
                        default=Path(LiveConfig.from_env().live_db_path))
    args = parser.parse_args()

    if args.backup and args.apply:
        dst = args.db_path.with_suffix(
            f".pre_exit_audit_backfill_{now_iso().replace(':', '')}.sqlite3.gz"
        )
        with open(args.db_path, "rb") as fh, gzip.open(dst, "wb") as gz:
            shutil.copyfileobj(fh, gz)
        print(f"backup: {dst}")

    repo = StrategyRepository(LiveRepository(args.db_path))
    repo.migrate()  # ensure the tables exist

    written = skipped = 0
    with repo.base.connect() as conn:
        positions = [
            dict(r) for r in conn.execute(
                "SELECT position_id, event_id, condition_id, token_id, stop_stage "
                "FROM live_strategy_positions WHERE stop_stage >= 1 "
                "ORDER BY created_at"
            ).fetchall()
        ]
        existing = {
            r[0] for r in conn.execute(
                "SELECT position_id FROM live_strategy_exit_audit"
            )
        }
        for pos in positions:
            if pos["position_id"] in existing and not args.force:
                skipped += 1
                continue
            fields = _reconstruct(conn, repo, pos)
            if fields is None:
                continue
            print(
                f"{pos['event_id']:<26} {fields.get('exit_purpose',''):<22} "
                f"outcome={fields.get('exit_outcome'):<12} "
                f"vwap={fields.get('actual_fill_vwap_text') or '-':<7} "
                f"exec_ms={fields.get('execution_latency_ms')}"
            )
            if args.apply:
                repo.record_exit_audit(
                    stable_id("exit-audit", pos["position_id"]),
                    position_id=pos["position_id"],
                    event_id=pos["event_id"],
                    episode_seq=1,
                    condition_id=pos["condition_id"],
                    token_id=pos["token_id"],
                    deal_id=stable_id("deal", pos["event_id"]),
                    **fields,
                )
                written += 1

    print(
        f"\n{len(positions)} latched positions; "
        f"{skipped} already present; "
        + (f"{written} rows written" if args.apply else "dry-run, nothing written")
    )
    print("next: scripts/classify_stop_exits.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
