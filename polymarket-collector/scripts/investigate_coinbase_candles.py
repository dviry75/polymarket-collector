import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def candle_times(payload):
    times = []
    if not isinstance(payload, list):
        return times
    for item in payload:
        if isinstance(item, list) and item:
            try:
                times.append(int(float(item[0])))
            except (TypeError, ValueError):
                pass
    return times


def order_label(times):
    if len(times) < 2:
        return "single_or_empty"
    if times == sorted(times):
        return "ascending"
    if times == sorted(times, reverse=True):
        return "descending"
    return "mixed"


def build_variants(now_dt: datetime):
    bucket_epoch = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)
    bucket_start = datetime.fromtimestamp(bucket_epoch, timezone.utc)
    bucket_end = bucket_start + timedelta(seconds=app.COINBASE_CANDLE_GRANULARITY_SECONDS)
    previous_start = bucket_start - timedelta(seconds=app.COINBASE_CANDLE_GRANULARITY_SECONDS)

    return {
        "no_range": {
            "granularity": app.COINBASE_CANDLE_GRANULARITY_SECONDS,
        },
        "start_current_end_now": {
            "granularity": app.COINBASE_CANDLE_GRANULARITY_SECONDS,
            "start": iso_z(bucket_start),
            "end": iso_z(now_dt),
        },
        "start_current_end_candle_end": {
            "granularity": app.COINBASE_CANDLE_GRANULARITY_SECONDS,
            "start": iso_z(bucket_start),
            "end": iso_z(bucket_end),
        },
        "start_prev_end_now": {
            "granularity": app.COINBASE_CANDLE_GRANULARITY_SECONDS,
            "start": iso_z(previous_start),
            "end": iso_z(now_dt),
        },
        "start_prev_end_candle_end": {
            "granularity": app.COINBASE_CANDLE_GRANULARITY_SECONDS,
            "start": iso_z(previous_start),
            "end": iso_z(bucket_end),
        },
    }


def summarize_response(name, params, payload, error, now_dt):
    bucket_epoch = app.floor_to_epoch(now_dt, app.COINBASE_CANDLE_GRANULARITY_SECONDS)
    times = candle_times(payload)
    current = None
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, list) and len(item) >= 6:
                try:
                    if int(float(item[0])) == bucket_epoch:
                        current = item
                        break
                except (TypeError, ValueError):
                    pass

    return {
        "variant": name,
        "params": params,
        "error": error,
        "response_is_list": isinstance(payload, list),
        "count": len(payload) if isinstance(payload, list) else None,
        "current_present": current is not None,
        "current_volume": current[5] if current else None,
        "bucket_epoch": bucket_epoch,
        "bucket_age_seconds": int(now_dt.timestamp()) - bucket_epoch,
        "order": order_label(times),
        "times": [datetime.fromtimestamp(item, timezone.utc).isoformat() for item in times[:8]],
    }


def request_variant(client, name, params, now_dt):
    url = app.coinbase_candles_url()
    try:
        response = client.get(
            url,
            params=params,
            headers={"User-Agent": "polymarket-btc-collector-investigation/1.0"},
        )
        payload = response.json()
        error = None if response.status_code == 200 else f"HTTP {response.status_code}: {response.text[:160]}"
    except Exception as exc:
        payload = None
        error = f"{type(exc).__name__}: {exc}"

    result = summarize_response(name, params, payload, error, now_dt)
    result["url"] = f"{url}?{urlencode(params)}"
    return result


def aggregate(results):
    by_variant = {}
    for result in results:
        item = by_variant.setdefault(
            result["variant"],
            {
                "checks": 0,
                "current_present": 0,
                "errors": 0,
                "bucket_ages_when_missing": [],
                "orders": {},
            },
        )
        item["checks"] += 1
        if result["current_present"]:
            item["current_present"] += 1
        if result["error"]:
            item["errors"] += 1
        if not result["current_present"]:
            item["bucket_ages_when_missing"].append(result["bucket_age_seconds"])
        item["orders"][result["order"]] = item["orders"].get(result["order"], 0) + 1

    for item in by_variant.values():
        item["current_present_rate"] = round(item["current_present"] / item["checks"], 4) if item["checks"] else 0
    return by_variant


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    started = time.monotonic()
    results = []
    with httpx.Client(timeout=app.COINBASE_REQUEST_TIMEOUT_SECONDS) as client:
        while time.monotonic() - started < args.duration_seconds:
            now_dt = datetime.now(timezone.utc)
            variants = build_variants(now_dt)
            for name, params in variants.items():
                result = request_variant(client, name, params, now_dt)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(args.interval_seconds)

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds,
        "aggregate": aggregate(results),
        "results": results,
    }
    print(json.dumps({"summary": summary["aggregate"]}, indent=2, ensure_ascii=False))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
