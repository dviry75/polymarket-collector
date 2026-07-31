from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    truststore = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.config import LiveConfig
from live.market_discovery import refresh_btc_5m_markets
from live.market_websocket import MarketWebSocketManager
from live.repository import LiveRepository


async def main() -> int:
    config = LiveConfig.from_env()
    with tempfile.TemporaryDirectory(prefix="polymarket-market-ws-smoke-") as directory:
        repo = LiveRepository(Path(directory) / "live.sqlite3")
        repo.migrate()
        conditions = await refresh_btc_5m_markets(repo)
        asset_ids = repo.market_ws_asset_ids()
        if not asset_ids:
            print(json.dumps({
                "ok": False,
                "reason": "NO_ACTIVE_BTC_5M_ASSETS",
                "conditions": conditions,
            }))
            return 2
        manager = MarketWebSocketManager(repo, stale_after_seconds=30)
        result = await manager.connect_for_messages(
            config.market_ws_url,
            asset_ids,
            max_messages=4,
            timeout_seconds=20,
        )
        snapshots = repo.list_table("live_market_snapshots", 20)
        output = {
            "ok": bool(result.get("connected") and result.get("messages") and snapshots),
            "conditions": conditions,
            "asset_ids": asset_ids,
            "connection": result,
            "health": manager.health(),
            "snapshots": len(snapshots),
            "snapshot_event_types": sorted({row["event_type"] for row in snapshots}),
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
