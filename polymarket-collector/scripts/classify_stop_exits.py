#!/usr/bin/env python3
"""Apply the offline root-cause classifier to captured stop-exit audit rows.

Read-mostly: it reads ``live_strategy_exit_audit`` + its book snapshots, runs
``live.exit_classifier.classify_exit``, and (only with ``--apply``) writes the
classification columns back onto the same rows. It never touches any other
table and never runs while the trader needs the write lock for anything
time-critical — the writes are tiny idempotent upserts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.config import LiveConfig
from live.exit_classifier import classify_exit
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


def _rows(repo: StrategyRepository, *, only_unclassified: bool):
    sql = "SELECT * FROM live_strategy_exit_audit"
    if only_unclassified:
        sql += " WHERE classified_at IS NULL"
    sql += " ORDER BY created_at"
    with repo.base.connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Print classifications without writing.")
    mode.add_argument("--apply", action="store_true",
                      help="Write classification columns back onto the rows.")
    parser.add_argument("--all", action="store_true",
                        help="Re-classify every row, not just unclassified ones.")
    parser.add_argument("--db-path", type=Path,
                        default=Path(LiveConfig.from_env().live_db_path))
    args = parser.parse_args()

    config = LiveConfig.from_env()
    repo = StrategyRepository(LiveRepository(args.db_path))

    rows = _rows(repo, only_unclassified=not args.all)
    if not rows:
        print("no exit-audit rows to classify")
        return 0

    written = 0
    for row in rows:
        snaps = repo.exit_book_snapshots(row["exit_audit_id"])
        result = classify_exit(
            row, snaps,
            acceptable_min_vwap=config.exit_forensic_acceptable_min_vwap,
            sla_ms=int(config.exit_supervisor_stop_to_submit_sla_seconds * 1000),
        )
        print(
            f"{row['event_id']:<26} {str(row.get('exit_purpose') or '-'):<22} "
            f"outcome={result['exit_outcome']:<12} "
            f"root={result['root_cause']:<18} "
            f"tech={result['technical_behavior']:<10} "
            f"[{row.get('evidence_quality')}] {result['rationale']}"
        )
        if args.apply:
            repo.record_exit_audit(
                row["exit_audit_id"],
                position_id=row["position_id"],
                event_id=row["event_id"],
                episode_seq=row.get("episode_seq") or 1,
                root_cause=result["root_cause"],
                primary_cause=result["primary_cause"],
                technical_behavior=result["technical_behavior"],
                exit_outcome=result["exit_outcome"],
                classified_at=now_iso(),
                classifier_version=result["classifier_version"],
            )
            written += 1

    print(f"\n{len(rows)} classified" + (f", {written} written" if args.apply else " (dry-run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
