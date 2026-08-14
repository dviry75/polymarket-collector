#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time


async def yield_benchmark(iterations: int) -> float:
    started = time.perf_counter()
    for _ in range(iterations):
        await asyncio.sleep(0)
    return time.perf_counter() - started


async def callback_benchmark(iterations: int) -> float:
    loop = asyncio.get_running_loop()
    finished = loop.create_future()
    remaining = iterations

    def step() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining <= 0:
            finished.set_result(None)
        else:
            loop.call_soon(step)

    started = time.perf_counter()
    loop.call_soon(step)
    await finished
    return time.perf_counter() - started


async def benchmark(iterations: int, repetitions: int) -> dict[str, object]:
    yields = []
    callbacks = []
    for _ in range(repetitions):
        yields.append(await yield_benchmark(iterations))
        callbacks.append(await callback_benchmark(iterations))
    return {
        "iterations": iterations,
        "repetitions": repetitions,
        "yield_median_ms": round(statistics.median(yields) * 1000, 3),
        "yield_ops_per_second": round(iterations / statistics.median(yields), 1),
        "callback_median_ms": round(statistics.median(callbacks) * 1000, 3),
        "callback_ops_per_second": round(iterations / statistics.median(callbacks), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", choices=("asyncio", "uvloop"), default="asyncio")
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.loop == "uvloop":
        import uvloop
        uvloop.install()
    result = asyncio.run(benchmark(args.iterations, args.repetitions))
    result["loop"] = args.loop
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
