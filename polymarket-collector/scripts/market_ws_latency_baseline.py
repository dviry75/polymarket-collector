#!/usr/bin/env python3
from __future__ import annotations

import argparse
import array
import asyncio
import fcntl
import hashlib
import json
import statistics
import time
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import websockets

from live.order_book import OrderBookSet
from live.repository import LiveRepository


def internal_queue_depth(ws):
    try:
        return len(ws.recv_messages.frames)
    except (AttributeError, TypeError):
        return None


def tcp_recv_q_bytes(ws):
    try:
        raw_socket = ws.transport.get_extra_info("socket")
        pending = array.array("i", [0])
        fcntl.ioctl(raw_socket.fileno(), 0x541B, pending, True)
        return int(pending[0])
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def message_assets(message):
    changes = message.get("price_changes")
    if isinstance(changes, list):
        return sorted({
            str(change.get("asset_id") or "")
            for change in changes
            if isinstance(change, dict) and change.get("asset_id")
        })
    asset = str(message.get("asset_id") or "")
    return [asset] if asset else []


def summary(values):
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)
    def at(fraction):
        return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))]
    return {
        "p50": round(statistics.median(ordered), 4),
        "p95": round(at(.95), 4),
        "p99": round(at(.99), 4),
        "max": round(ordered[-1], 4),
    }


async def collect(args):
    repo = LiveRepository(args.db)
    records = []
    connections = []
    lag_samples = []
    current_lag_ms = 0.0
    stop = asyncio.Event()

    async def watchdog():
        nonlocal current_lag_ms
        interval = .01
        expected = time.perf_counter() + interval
        while not stop.is_set():
            await asyncio.sleep(interval)
            now = time.perf_counter()
            current_lag_ms = max(0.0, (now - expected) * 1000)
            lag_samples.append(current_lag_ms)
            expected = now + interval

    async def heartbeat(ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    watchdog_task = asyncio.create_task(watchdog())
    deadline = time.monotonic() + args.duration
    generation = 0
    try:
        while time.monotonic() < deadline:
            generation += 1
            tokens = await asyncio.to_thread(repo.market_ws_asset_ids)
            connection_deadline = min(
                deadline, time.monotonic() + args.reconnect_interval
            )
            async with websockets.connect(
                args.url, ping_interval=None, close_timeout=5, max_queue=16
            ) as ws:
                connections.append({
                    "generation": generation,
                    "connected_wall_ms": int(time.time() * 1000),
                    "tokens": tokens,
                    "max_queue": list(ws.max_queue),
                    "max_size": getattr(ws.protocol, "max_size", None),
                    "write_limit": list(ws.write_limit),
                    "extensions": [
                        type(extension).__name__
                        for extension in (getattr(ws.protocol, "extensions", None) or [])
                    ],
                    "local_address": str(ws.local_address),
                    "remote_address": str(ws.remote_address),
                })
                await ws.send(json.dumps({
                    "type": "market",
                    "assets_ids": tokens,
                    "custom_feature_enabled": True,
                }))
                ping_task = asyncio.create_task(heartbeat(ws))
                last_handler_end = None
                try:
                    while time.monotonic() < connection_deadline:
                        before_recv = time.perf_counter()
                        between_recv_gap_ms = (
                            (before_recv - last_handler_end) * 1000
                            if last_handler_end is not None else None
                        )
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        recv_return = time.perf_counter()
                        receive_wall_ms = int(time.time() * 1000)
                        queue_depth = internal_queue_depth(ws)
                        recv_q = tcp_recv_q_bytes(ws)
                        if raw in ("PONG", b"PONG"):
                            continue
                        parse_start = time.perf_counter()
                        payload = json.loads(raw)
                        parse_end = time.perf_counter()
                        messages = payload if isinstance(payload, list) else [payload]
                        for index, message in enumerate(messages):
                            if not isinstance(message, dict):
                                continue
                            handler_start = time.perf_counter()
                            timestamp = message.get("timestamp")
                            exchange_ms, timestamp_error = (
                                OrderBookSet._exchange_timestamp_ms(
                                    str(timestamp)
                                    if timestamp not in (None, "") else None
                                )
                            )
                            message_hash = hashlib.sha256(json.dumps(
                                message,
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ).encode()).hexdigest()
                            handler_end = time.perf_counter()
                            records.append({
                                "source": "minimal",
                                "connection_generation": generation,
                                "message_hash": message_hash,
                                "event_type": str(
                                    message.get("event_type")
                                    or message.get("type") or ""
                                ).lower(),
                                "asset_ids": message_assets(message),
                                "exchange_timestamp_ms": exchange_ms,
                                "timestamp_error": timestamp_error,
                                "socket_receive_wall_ms": receive_wall_ms,
                                "exchange_to_socket_receive_ms": (
                                    receive_wall_ms - exchange_ms
                                    if exchange_ms is not None else None
                                ),
                                "recv_wait_ms": (
                                    recv_return - before_recv
                                ) * 1000,
                                "between_recv_gap_ms": between_recv_gap_ms,
                                "socket_receive_to_handler_ms": (
                                    handler_start - recv_return
                                ) * 1000,
                                "parse_ms": (parse_end - parse_start) * 1000,
                                "total_processing_ms": (
                                    handler_end - handler_start
                                ) * 1000,
                                "event_loop_lag_ms": current_lag_ms,
                                "ws_internal_queue_depth": queue_depth,
                                "tcp_recv_q_bytes": recv_q,
                                "batch_index": index,
                                "batch_size": len(messages),
                            })
                            last_handler_end = handler_end
                        if records and len(records) % 1000 == 0:
                            print(json.dumps({
                                "records": len(records),
                                "generation": generation,
                                "queue": queue_depth,
                                "tcp_recv_q": recv_q,
                                "age_ms": records[-1].get(
                                    "exchange_to_socket_receive_ms"
                                ),
                            }), flush=True)
                        await asyncio.sleep(0)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    pass
                finally:
                    ping_task.cancel()
            if time.monotonic() < deadline:
                await asyncio.sleep(.5)
    finally:
        stop.set()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass

    metric_names = (
        "exchange_to_socket_receive_ms", "recv_wait_ms",
        "between_recv_gap_ms", "socket_receive_to_handler_ms", "parse_ms",
        "total_processing_ms", "event_loop_lag_ms",
        "ws_internal_queue_depth", "tcp_recv_q_bytes",
    )
    result = {
        "generated_at_ms": int(time.time() * 1000),
        "duration_seconds": args.duration,
        "record_count": len(records),
        "connections": connections,
        "metrics": {
            name: summary([
                float(record[name]) for record in records
                if record.get(name) is not None
            ])
            for name in metric_names
        },
        "event_loop_lag_watchdog": summary(lag_samples),
        "records": records,
    }
    args.output.write_text(json.dumps(result, sort_keys=True))
    print(json.dumps({
        "output": str(args.output),
        "records": len(records),
        "connections": len(connections),
        "metrics": result["metrics"],
    }), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", default="/opt/polymarket-btc-live/poly_live.sqlite3"
    )
    parser.add_argument(
        "--url",
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
    )
    parser.add_argument("--duration", type=int, default=420)
    parser.add_argument("--reconnect-interval", type=int, default=210)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "/opt/polymarket-btc-live/output/minimal_ws_latency_baseline.json"
        ),
    )
    asyncio.run(collect(parser.parse_args()))


if __name__ == "__main__":
    main()
