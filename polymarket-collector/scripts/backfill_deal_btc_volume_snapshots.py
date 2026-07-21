import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Coinbase BTC volume snapshots onto historical deals."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview updates without writing.")
    mode.add_argument("--apply", action="store_true", help="Write missing deal BTC volume snapshots.")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a timestamped SQLite backup before --apply.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute all deal snapshots, including deals that already have one.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=app.DB_PATH,
        help=f"SQLite database path. Default: {app.DB_PATH}",
    )
    return parser.parse_args()


def backup_db(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}.btc_volume_backfill_{timestamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def candidate_deals(conn: sqlite3.Connection, force: bool) -> list[sqlite3.Row]:
    where = "entry_at IS NOT NULL"
    if not force:
        where += " AND entry_btc_volume_log_id IS NULL"
    return conn.execute(f"""
        SELECT id, event_id, entry_at, entry_btc_volume_log_id
        FROM deals
        WHERE {where}
        ORDER BY entry_at ASC, id ASC
    """).fetchall()


def main() -> int:
    args = parse_args()
    db_path = args.db_path.resolve()
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2

    app.DB_PATH = db_path
    app.init_db()

    backup_path = None
    if args.apply and args.backup:
        backup_path = backup_db(db_path)

    scanned = 0
    matched = 0
    updated = 0
    unmatched = 0
    over_6 = 0
    already_had_snapshot = 0

    with app.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        deals = candidate_deals(conn, args.force)
        scanned = len(deals)

        for deal in deals:
            if deal["entry_btc_volume_log_id"] is not None:
                already_had_snapshot += 1

            snapshot = app.find_entry_btc_volume_snapshot(conn, deal["entry_at"], deal["event_id"])
            if not snapshot:
                unmatched += 1
                continue

            matched += 1
            if snapshot["volume_btc_delta"] is not None and float(snapshot["volume_btc_delta"]) > 6:
                over_6 += 1

            if args.apply:
                conn.execute("""
                    UPDATE deals SET
                        entry_btc_volume_log_id = ?,
                        entry_btc_volume_sampled_at = ?,
                        entry_btc_volume_btc_cumulative = ?,
                        entry_btc_volume_btc_delta = ?,
                        entry_btc_volume_status = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (
                    snapshot["id"],
                    snapshot["sampled_at"],
                    snapshot["volume_btc_cumulative"],
                    snapshot["volume_btc_delta"],
                    snapshot["status"],
                    app.now_iso(),
                    deal["id"],
                ))
                updated += 1

        if args.apply:
            conn.commit()

        total_over_6 = conn.execute("""
            SELECT COUNT(*)
            FROM deals
            WHERE entry_btc_volume_btc_delta > 6
        """).fetchone()[0]
        total_missing = conn.execute("""
            SELECT COUNT(*)
            FROM deals
            WHERE entry_btc_volume_log_id IS NULL
        """).fetchone()[0]

    print(f"mode={'apply' if args.apply else 'dry-run'}")
    print(f"db_path={db_path}")
    if backup_path:
        print(f"backup_path={backup_path}")
    print(f"scanned={scanned}")
    print(f"already_had_snapshot={already_had_snapshot}")
    print(f"matched={matched}")
    print(f"unmatched={unmatched}")
    print(f"updated={updated}")
    print(f"matched_over_6_delta={over_6}")
    print(f"total_deals_over_6_delta={total_over_6}")
    print(f"total_deals_missing_snapshot={total_missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
