#!/usr/bin/env python3
"""Apply additive dashboard provenance migrations to an explicitly selected DB."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.config import LiveConfig
from live.dashboard_schema import migrate_dashboard_schema
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=("STAGING", "LIVE"))
    parser.add_argument("--cutover-at")
    parser.add_argument("--run-id")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    target = args.db.resolve()
    if not target.is_file():
        parser.error("database path must name an existing file")
    if args.environment == "LIVE" and not args.allow_live:
        parser.error("LIVE migration requires explicit --allow-live after a verified backup and drain")
    config = LiveConfig.from_env()
    context = migrate_dashboard_schema(
        LiveRepository(target), config,
        cutover_at=args.cutover_at,
        environment=args.environment,
        run_id=args.run_id,
    )
    print(json.dumps({
        "status": "ok", "database": str(target), "environment": context.environment,
        "execution_mode": context.execution_mode, "cutover_at": context.cutover_at,
        "run_id": context.run_id,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
