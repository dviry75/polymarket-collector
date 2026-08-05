from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime


async def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="polymarket-live-soak-") as directory:
        db_path = Path(directory) / "soak.sqlite3"
        config = LiveConfig(
            trading_mode="LIVE", execution_mode="PAPER_TRADING",
            paper_trading_enabled=True, live_module_enabled=True,
            live_adapter="mock", live_db_path=str(db_path),
        )
        base = LiveRepository(db_path)
        base.migrate()
        strategy = StrategyRepository(base)
        strategy.migrate()
        adapter = MockTradingAdapter()
        reconciliation = ReconciliationWorker(base, adapter, strategy)
        runtime = LiveStrategyRuntime(
            config, base, strategy, adapter,
            reconciliation=lambda reason: reconciliation.run_once(f"soak:{reason}"),
        )
        base.set_state("kill_switch", "false", "soak")
        strategy.set_pause_entries(False, "soak", "DETERMINISTIC_FIXTURE")
        processed = 0
        for index in range(12):
            start = 2_000_000_000 + index * 300
            event_id = f"btc-updown-5m-{start}"
            condition_id = f"condition-{index}"
            yes, no = f"yes-{index}", f"no-{index}"
            base.upsert_market({
                "event_id": event_id, "condition_id": condition_id,
                "yes_token_id": yes, "no_token_id": no,
                "token_mapping_status": "verified", "min_order_size": "5",
                "min_tick_size": "0.01", "taker_base_fee": "0.07",
                "accepting_orders": True,
                "raw_market_info": {"slug": event_id, "scope_verified": True},
            })
            observed = datetime.fromtimestamp(start + 200, timezone.utc).isoformat()
            simultaneous = index % 4 == 0
            updates = [
                {"condition_id": condition_id, "event_id": event_id, "asset_id": yes,
                 "outcome": "YES", "best_bid": "0.73", "best_ask": "0.74",
                 "bids": [{"price": "0.73", "size": "20"}],
                 "asks": [{"price": "0.74", "size": "20"}], "book_ready": True},
                {"condition_id": condition_id, "event_id": event_id, "asset_id": no,
                 "outcome": "NO", "best_bid": "0.25",
                 "best_ask": "0.74" if simultaneous else "0.26",
                 "bids": [{"price": "0.25", "size": "20"}],
                 "asks": [{"price": "0.74" if simultaneous else "0.26", "size": "20"}],
                 "book_ready": True},
            ]
            context = {
                "received_at": observed, "message_hash": f"entry-{index}",
                "updates": updates,
                "event_readiness": {condition_id: {"ready": True, "reason": "READY"}},
            }
            await runtime.process_atomic_frame(context)
            processed += 1
            # Replay the exact same trigger after a simulated restart.
            if index == 5:
                runtime = LiveStrategyRuntime(config, base, StrategyRepository(base), adapter)
                await runtime.process_atomic_frame(context)
                processed += 1
            position = strategy.position_for_token(yes)
            if position and position.get("state") == "TP_OPEN":
                await runtime.process_atomic_frame({
                    "received_at": observed, "message_hash": f"tp-{index}",
                    "updates": [{
                        "condition_id": condition_id, "event_id": event_id,
                        "asset_id": yes, "outcome": "YES", "best_bid": "0.96",
                        "best_ask": "0.97", "bids": [{"price": "0.96", "size": "20"}],
                        "asks": [{"price": "0.97", "size": "20"}], "book_ready": True,
                    }],
                    "event_readiness": {condition_id: {"ready": True, "reason": "READY"}},
                })
                processed += 1
        with base.connect() as conn:
            duplicate_entries = conn.execute(
                "SELECT COUNT(*) FROM (SELECT event_id,COUNT(*) c FROM live_strategy_intents WHERE action='ENTRY' GROUP BY event_id HAVING c>1)"
            ).fetchone()[0]
            parallel_exits = conn.execute(
                "SELECT COUNT(*) FROM (SELECT position_id,COUNT(*) c FROM live_strategy_intents WHERE action='EXIT' AND state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED') GROUP BY position_id HAVING c>1)"
            ).fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM live_event_states").fetchone()[0]
            simultaneous = conn.execute("SELECT COUNT(*) FROM live_event_states WHERE status='SKIPPED_SIMULTANEOUS_TRIGGER'").fetchone()[0]
            entries = conn.execute("SELECT COUNT(*) FROM live_strategy_intents WHERE action='ENTRY'").fetchone()[0]
        integrity = sqlite3.connect(db_path).execute("PRAGMA integrity_check").fetchone()[0]
        result = {
            "events": events, "entry_intents": entries,
            "simultaneous_skips": simultaneous, "frames_processed": processed,
            "duplicate_entry_groups": duplicate_entries,
            "parallel_exit_groups": parallel_exits,
            "active_positions": len(strategy.active_positions()),
            "legacy_orders": len(base.list_table("live_orders", 100)),
            "db_size_bytes": db_path.stat().st_size,
            "integrity_check": integrity,
        }
        assert events == 12
        assert entries == 9
        assert simultaneous == 3
        assert duplicate_entries == 0
        assert parallel_exits == 0
        assert result["active_positions"] == 0
        assert result["legacy_orders"] == 0
        assert integrity == "ok"
        return result


if __name__ == "__main__":
    print(asyncio.run(run()))
