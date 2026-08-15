#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    truststore = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import websockets

from live.order_book import OrderBookSet
from live.repository import LiveRepository


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_message(message: dict) -> dict:
    event_type = str(message.get("event_type") or message.get("type") or "").lower()
    result = {
        "event_type": event_type,
        "timestamp": message.get("timestamp"),
        "asset_id": message.get("asset_id"),
        "market": message.get("market"),
        "best_bid": message.get("best_bid"),
        "best_ask": message.get("best_ask"),
    }
    if event_type == "book":
        result["bids"] = message.get("bids")
        result["asks"] = message.get("asks")
    elif event_type == "price_change":
        result["price_changes"] = message.get("price_changes")
    return result


async def capture(args: argparse.Namespace) -> dict:
    repo = LiveRepository(args.db)
    assets = repo.market_ws_asset_ids()
    books = OrderBookSet(assets)
    ring: deque[dict] = deque(maxlen=args.ring_size)
    mismatches: list[dict] = []
    messages_seen = 0
    out_of_order_count = 0
    post_mismatch_remaining: int | None = None

    async with websockets.connect(
        args.url,
        ping_interval=None,
        close_timeout=5,
        max_queue=(256, 64),
        compression=None,
    ) as ws:
        await ws.send(json.dumps({
            "type": "market",
            "assets_ids": assets,
            "custom_feature_enabled": True,
        }))

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(10)
                await ws.send("PING")

        heartbeat_task = asyncio.create_task(heartbeat())
        deadline = time.monotonic() + args.duration
        try:
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                receive_wall_ms = time.time_ns() // 1_000_000
                received_at = utc_now()
                if raw in ("PONG", b"PONG"):
                    continue
                payload = json.loads(raw)
                messages = payload if isinstance(payload, list) else [payload]
                for batch_index, message in enumerate(messages):
                    if not isinstance(message, dict):
                        continue
                    frame = books.apply(
                        message,
                        now_ms=receive_wall_ms,
                        max_age_ms=1_000,
                        future_tolerance_ms=1_000,
                        include_depth=False,
                    )
                    record = {
                        "receive_timestamp": received_at,
                        "receive_wall_ms": receive_wall_ms,
                        "batch_index": batch_index,
                        "batch_size": len(messages),
                        "message": compact_message(message),
                        "frame": {
                            "message_hash": frame.message_hash,
                            "duplicate": frame.duplicate,
                            "out_of_order": frame.out_of_order,
                            "rejected_reason": frame.rejected_reason,
                            "exchange_age_ms": frame.exchange_age_ms,
                            "updates": list(frame.updates),
                        },
                    }
                    ring.append(record)
                    messages_seen += 1
                    if frame.out_of_order:
                        out_of_order_count += 1
                    reasons = {
                        str(update.get("readiness_reason") or "")
                        for update in frame.updates
                    }
                    is_target_mismatch = (
                        (
                            "BEST_PRICE_MISMATCH" in reasons
                            and (
                                args.mismatch_event_type == "any"
                                or message.get("event_type") == args.mismatch_event_type
                            )
                        )
                        or (args.stop_on_out_of_order and frame.out_of_order)
                    )
                    if is_target_mismatch:
                        mismatches.append({
                            "message_hash": frame.message_hash,
                            "record_index": messages_seen,
                        })
                        if post_mismatch_remaining is None:
                            post_mismatch_remaining = args.following_messages
                    elif post_mismatch_remaining is not None:
                        post_mismatch_remaining -= 1
                        if post_mismatch_remaining <= 0:
                            return {
                                "captured_at": utc_now(),
                                "assets": assets,
                                "messages_seen": messages_seen,
                                "out_of_order_count": out_of_order_count,
                                "mismatches": mismatches,
                                "records": list(ring),
                            }
        finally:
            heartbeat_task.cancel()
    return {
        "captured_at": utc_now(),
        "assets": assets,
        "messages_seen": messages_seen,
        "out_of_order_count": out_of_order_count,
        "mismatches": mismatches,
        "records": list(ring),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/opt/polymarket-btc-live/poly_live.sqlite3")
    parser.add_argument(
        "--url",
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
    )
    parser.add_argument("--duration", type=int, default=90)
    parser.add_argument("--ring-size", type=int, default=256)
    parser.add_argument("--following-messages", type=int, default=16)
    parser.add_argument(
        "--mismatch-event-type",
        choices=("any", "price_change", "best_bid_ask"),
        default="any",
    )
    parser.add_argument("--stop-on-out-of-order", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(capture(args))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps({
        "output": str(args.output),
        "messages_seen": result["messages_seen"],
        "mismatches": len(result["mismatches"]),
        "records": len(result["records"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
