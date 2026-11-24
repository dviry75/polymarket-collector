#!/usr/bin/env python3
"""Event-loop cost of the BTC reference feed + gate. Offline, temp DB only.

Runs the same workload twice on a fresh temporary database:

* baseline: market-frame replay through ``schedule_frame`` with the BTC gate
  ``off`` and no feed;
* btc: the same replay with the gate in ``observe`` (shadow evaluation on every
  0.74 trigger), RTDS frames parsed at 1 tick/s through the real parser, and
  the BTC writer task flushing to SQLite every second.

Reports event-loop lag (10 ms probe) and per-frame ``schedule_frame`` time.
Never opens a network connection and never reads the production env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live.strategy_runtime as strategy_runtime_module  # noqa: E402
from live.adapters.mock import MockTradingAdapter  # noqa: E402
from live.btc_reference_feed import RtdsChainlinkProvider  # noqa: E402
from live.config import LiveConfig  # noqa: E402
from live.repository import LiveRepository  # noqa: E402
from live.strategy_repository import StrategyRepository  # noqa: E402
from live.strategy_runtime import LiveStrategyRuntime  # noqa: E402


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def summary(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 4),
        "p99": round(pct(values, 99), 4),
        "max": round(max(values), 4),
    }


def rtds_update(ts_ms: int, price: Decimal) -> str:
    scaled = int(price * Decimal(10) ** 18)
    return json.dumps({
        "topic": "crypto_prices_chainlink", "type": "update", "timestamp": ts_ms + 1100,
        "payload": {"symbol": "btc/usd", "timestamp": ts_ms, "value": float(price),
                    "full_accuracy_value": str(scaled)},
    })


async def run_variant(btc: bool, seconds: float, frame_rate: int) -> dict[str, object]:
    temp = tempfile.TemporaryDirectory()
    try:
        base = LiveRepository(Path(temp.name) / "bench.sqlite3")
        base.migrate()
        strategy = StrategyRepository(base)
        strategy.migrate()
        window = int(time.time()) // 300 * 300
        event_id = f"btc-updown-5m-{window}"
        base.upsert_market({
            "event_id": event_id, "condition_id": "condition",
            "yes_token_id": "yes", "no_token_id": "no",
            "token_mapping_status": "verified", "accepting_orders": True,
            "min_order_size": 1, "min_tick_size": 0.01, "taker_base_fee": 0,
            "raw_market_info": {"yes_outcome_label": "Up", "no_outcome_label": "Down"},
        })
        config = LiveConfig(execution_mode="REAL_TRADING",
                            btc_gate_mode="observe" if btc else "off")
        runtime = LiveStrategyRuntime(config, base, strategy, MockTradingAdapter())
        market = base.latest_market("condition")
        runtime._market = lambda _condition: market  # RAM cache, as in production
        provider = RtdsChainlinkProvider()
        stop = asyncio.Event()
        writer = None
        if btc:
            runtime.set_btc_reference_provider(provider)
            writer = asyncio.create_task(runtime._btc.run(stop))

        lags: list[float] = []
        frame_ms: list[float] = []
        deadline = time.monotonic() + seconds

        async def lag_probe() -> None:
            while time.monotonic() < deadline:
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                lags.append((time.perf_counter() - started) * 1000 - 10)

        async def ticks() -> None:
            price = Decimal("80000.123456789012345678")
            while time.monotonic() < deadline:
                now_ms = int(time.time() * 1000) // 1000 * 1000
                if btc:
                    provider.handle_raw(rtds_update(now_ms, price))
                price += Decimal("1.5")
                await asyncio.sleep(1.0)

        async def frames() -> None:
            sequence = 0
            interval = 1 / frame_rate
            while time.monotonic() < deadline:
                sequence += 1
                ask = "0.74" if sequence % 2 else "0.73"
                token = "yes" if sequence % 4 < 2 else "no"
                context = {
                    "event_type": "price_change", "message_hash": f"f{sequence}",
                    "updates": [{"condition_id": "condition", "asset_id": token,
                                 "outcome": "YES" if token == "yes" else "NO",
                                 "best_ask": ask, "best_bid": "0.70"}],
                    "event_readiness": {"condition": {"ready": True, "reason": "READY"}},
                }
                started = time.perf_counter()
                runtime.schedule_frame(context)
                frame_ms.append((time.perf_counter() - started) * 1000)
                await asyncio.sleep(interval)

        await asyncio.gather(lag_probe(), ticks(), frames())
        stop.set()
        if writer is not None:
            await asyncio.wait_for(writer, 5)
        result: dict[str, object] = {
            "variant": "btc" if btc else "baseline",
            "event_loop_lag_ms": summary(lags),
            "schedule_frame_ms": summary(frame_ms),
        }
        if btc:
            health = runtime._btc.health()
            result["btc"] = {key: health[key] for key in (
                "evaluations_noted", "rows_written", "writer_failures")}
            result["btc"]["ticks_total"] = provider.state.ticks_total
        return result
    finally:
        temp.cleanup()


async def main_async(seconds: float, frame_rate: int) -> list[dict[str, object]]:
    # Keep every trigger inside the entry window so the shadow path always runs.
    strategy_runtime_module.remaining_seconds = lambda _event, _at: Decimal("60")
    return [
        await run_variant(False, seconds, frame_rate),
        await run_variant(True, seconds, frame_rate),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--frame-rate", type=int, default=50)
    parser.add_argument("--loop", choices=("asyncio", "uvloop"), default="asyncio")
    args = parser.parse_args()
    if args.loop == "uvloop":
        import uvloop
        uvloop.install()
    for row in asyncio.run(main_async(args.seconds, args.frame_rate)):
        row["loop"] = args.loop
        print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
