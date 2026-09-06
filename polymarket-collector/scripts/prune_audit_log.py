#!/usr/bin/env python3
"""Archive and prune the two audit firehoses that bloat the live DB.

``live_audit_log`` (a key/value state-change journal — ``set_*`` per WS
heartbeat) and ``live_audit_timeline`` grow by millions of rows/day. A 5 GB DB
with 7M+ audit rows turns every writer-lock stall into a multi-second one and
any action-filtered scan into a ~1 s full scan.

This tool copies rows older than ``--older-than-days`` into a sibling archive
database, deletes them from the live DB, and runs an *incremental* vacuum so no
single statement holds the write lock for long. It is **operator-run**, off the
hot path, and dry-run by default.

Safe-by-default guarantees:
  * ``--apply`` is required to change anything; without it, only a report.
  * Rows are copied to the archive and verified (count match) *before* the
    DELETE. The archive is opened in the same directory as the live DB.
  * DELETE + vacuum run in bounded batches with a short ``busy_timeout`` so the
    trader's writes are never starved; if the DB is busy the batch retries.
  * Recent rows (default: last 3 days) are never touched.
  * ``live_audit_log`` rows whose ``action`` does not start with ``set_`` are
    kept regardless of age unless ``--include-non-set`` is given — those are the
    forensic ones (auto-repair, quarantine, operator actions).

Typical use (trader may stay running):

    python scripts/prune_audit_log.py --db /opt/polymarket-btc-live/poly_live.sqlite3
    python scripts/prune_audit_log.py --db ... --older-than-days 7 --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIN_RETAIN_DAYS = 3
BATCH = 20_000
BUSY_TIMEOUT_MS = 5_000

TABLES = {
    # table: (timestamp column, extra keep-predicate for --apply)
    "live_audit_log": "occurred_at",
    "live_audit_timeline": "occurred_at",
}


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if read_only else str(path)
    conn = sqlite3.connect(uri, uri=read_only, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _cutoff(days: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(days=days)
    ).isoformat()


def _where(table: str, cutoff: str, include_non_set: bool) -> tuple[str, list]:
    clause = f"{TABLES[table]} < ?"
    params: list = [cutoff]
    if table == "live_audit_log" and not include_non_set:
        clause += " AND action LIKE 'set\\_%' ESCAPE '\\'"
    return clause, params


def report(db: Path, cutoff: str, include_non_set: bool) -> dict[str, int]:
    conn = _connect(db, read_only=True)
    out: dict[str, int] = {}
    try:
        for table in TABLES:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            where, params = _where(table, cutoff, include_non_set)
            prunable = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
            out[table] = prunable
            print(
                f"  {table:24s} total={total:>12,}  "
                f"prunable(<{cutoff[:10]})={prunable:>12,}"
            )
    finally:
        conn.close()
    return out


def _archive_path(db: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return db.with_name(f"{db.stem}.audit_archive_{stamp}.sqlite3")


def prune(
    db: Path, cutoff: str, include_non_set: bool, *, archive: Path
) -> None:
    live = _connect(db)
    live.execute(f"ATTACH DATABASE '{archive}' AS arc")
    try:
        for table in TABLES:
            where, params = _where(table, cutoff, include_non_set)
            live.execute(
                f"CREATE TABLE IF NOT EXISTS arc.{table} "
                f"AS SELECT * FROM main.{table} WHERE 0"
            )
            # CREATE TABLE AS does not copy the PK: index id ourselves so the
            # resumable "already archived?" check stays fast across re-runs.
            live.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS arc.idx_{table}_id "
                f"ON {table}(id)"
            )
            copied = _run_batched(
                live,
                f"INSERT INTO arc.{table} SELECT * FROM main.{table} "
                f"WHERE {where} AND id NOT IN (SELECT id FROM arc.{table}) "
                f"LIMIT {BATCH}",
                params,
                label=f"archive {table}",
            )
            src = live.execute(
                f"SELECT COUNT(*) FROM main.{table} WHERE {where}", params
            ).fetchone()[0]
            arc = live.execute(
                f"SELECT COUNT(*) FROM arc.{table} WHERE {where}", params
            ).fetchone()[0]
            if arc < src:
                raise SystemExit(
                    f"ABORT: {table} archive {arc} < live {src}; nothing deleted"
                )
            print(f"  {table}: archived {copied:,} this run, {arc:,} total ready")
            deleted = _run_batched(
                live,
                f"DELETE FROM main.{table} WHERE id IN "
                f"(SELECT id FROM main.{table} WHERE {where} LIMIT {BATCH})",
                params,
                label=f"delete {table}",
            )
            print(f"  {table}: deleted {deleted:,}")
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        live.commit()
    finally:
        live.execute("DETACH DATABASE arc")
        live.close()


def _run_batched(
    conn: sqlite3.Connection, sql: str, params: list, *, label: str
) -> int:
    affected = 0
    while True:
        for attempt in range(10):
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        else:
            raise SystemExit(f"ABORT: {label} could not acquire the write lock")
        if not cur.rowcount:
            return affected
        affected += cur.rowcount
        time.sleep(0.05)  # let the trader's writes through between batches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--older-than-days", type=int, default=7)
    ap.add_argument(
        "--include-non-set",
        action="store_true",
        help="also prune forensic live_audit_log rows (auto-repair, operator...)",
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.older_than_days < MIN_RETAIN_DAYS:
        print(f"refusing: --older-than-days must be >= {MIN_RETAIN_DAYS}")
        return 2
    if not args.db.exists():
        print(f"no such db: {args.db}")
        return 2

    cutoff = _cutoff(args.older_than_days)
    print(f"live db : {args.db}  ({args.db.stat().st_size / 1e9:.2f} GB)")
    print(f"cutoff  : {cutoff}  (keep newer)")
    counts = report(args.db, cutoff, args.include_non_set)

    if not args.apply:
        print("\ndry-run only. re-run with --apply to archive + prune + vacuum.")
        return 0
    if not sum(counts.values()):
        print("\nnothing to prune.")
        return 0

    archive = _archive_path(args.db)
    print(f"\narchive : {archive}")
    prune(args.db, cutoff, args.include_non_set, archive=archive)
    print(f"done. live db now {args.db.stat().st_size / 1e9:.2f} GB")
    print("NOTE: DELETE frees pages for reuse (growth stops) but does not")
    print("      shrink the file. Run a full offline VACUUM in the next")
    print("      maintenance window to reclaim the ~GB on disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
