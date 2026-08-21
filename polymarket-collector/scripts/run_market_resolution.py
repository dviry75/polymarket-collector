from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.market_resolution import MarketResolutionReconciler
from live.repository import LiveRepository


async def main() -> None:
    db_path = Path(os.getenv("LIVE_DB_PATH", "/opt/polymarket-btc-live/poly_live.sqlite3"))
    interval = max(5, int(os.getenv("LIVE_MARKET_RESOLUTION_INTERVAL_SECONDS", "30")))
    grace = max(0, int(os.getenv("LIVE_MARKET_RESOLUTION_GRACE_SECONDS", "60")))
    batch_size = max(1, int(os.getenv("LIVE_MARKET_RESOLUTION_BATCH_SIZE", "10")))
    repo = LiveRepository(db_path)
    repo.migrate()
    reconciler = MarketResolutionReconciler(repo, grace_seconds=grace, batch_size=batch_size)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    while not stopping.is_set():
        try:
            logging.info("market resolution cycle: %s", await reconciler.run_once())
        except Exception:
            logging.exception("market resolution cycle failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
