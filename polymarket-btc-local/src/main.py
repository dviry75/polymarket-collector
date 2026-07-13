import time
from datetime import timedelta
from pathlib import Path

from requests import HTTPError, RequestException

from src.config import load_config
from src.csv_storage import (
    EVENT_LOG_HEADERS,
    HEADERS,
    append_row,
    ensure_csv_file,
    export_csv_to_xlsx,
    upsert_event_row,
)
from src.polymarket import (
    calculate_trade_window,
    discover_current_btc_5m_market,
    fetch_active_events,
    fetch_orderbook,
    fetch_trades,
    find_btc_up_down_5m_markets,
    orderbook_metrics,
    parse_datetime,
    utc_now,
)


def main() -> None:
    print("Starting Polymarket BTC 5m collector.")
    config = load_config()

    try:
        csv_path = ensure_csv_file(config.csv_events_path, HEADERS)
        event_logs_path = ensure_csv_file(config.csv_event_logs_path, EVENT_LOG_HEADERS)
        print(f"Using events CSV storage: {csv_path}")
        print(f"Using event logs CSV storage: {event_logs_path}")

        print("Fetching active events from Polymarket Gamma API.")
        events = fetch_active_events(config.polymarket_gamma_events_url, config.events_fetch_limit)
        matches = find_btc_up_down_5m_markets(events)
        print(f"Fetched {len(events)} events. Found {len(matches)} matching markets.")

        if not matches:
            print("No matching BTC Up/Down 5m markets found.")

        for row in matches:
            action = upsert_event_row(csv_path, row, HEADERS)
            print(f"{action}: condition_id={row['condition_id']} status={row['status']}")

        run_polling(config, csv_path, event_logs_path)
        xlsx_path = export_csv_to_xlsx(event_logs_path, Path("output"))
        print(f"Excel created: {xlsx_path}")
        print("Collector completed.")
    except HTTPError as exc:
        raise RuntimeError(f"Polymarket HTTP error: {exc}") from exc
    except RequestException as exc:
        raise RuntimeError(f"Polymarket request failed: {exc}") from exc


def run_polling(config, csv_path: Path, event_logs_path: Path) -> None:
    started_monotonic = time.monotonic()
    previous_sample_at = None
    active_market = None
    seen_trade_keys_by_condition: dict[str, set[str]] = {}

    print(
        f"Polling every {config.poll_interval_seconds}s for {config.run_duration_seconds}s.",
        flush=True,
    )

    while time.monotonic() - started_monotonic < config.run_duration_seconds:
        tick_started = time.monotonic()
        now = utc_now()

        if not active_market or market_has_ended(active_market, now):
            active_market = discover_current_btc_5m_market()
            previous_sample_at = None
            if active_market:
                upsert_event_row(csv_path, active_market, HEADERS)
                print(f"Active market: {active_market['event_slug']}", flush=True)

        if not active_market:
            append_row(event_logs_path, empty_log_row(now.isoformat(), "no_market", "No active BTC 5m market"), EVENT_LOG_HEADERS)
            sleep_remaining(config.poll_interval_seconds, tick_started)
            continue

        window_start = previous_sample_at or (now - timedelta(seconds=config.poll_interval_seconds))
        window_end = now
        previous_sample_at = window_end

        log_row = sample_market(active_market, window_start, window_end, seen_trade_keys_by_condition)
        append_row(event_logs_path, log_row, EVENT_LOG_HEADERS)
        print(
            f"sampled {log_row['event_slug']} status={log_row['status']} "
            f"up_ask={log_row['up_best_ask']} down_ask={log_row['down_best_ask']} "
            f"trades={log_row['total_trades_count_window']}",
            flush=True,
        )

        sleep_remaining(config.poll_interval_seconds, tick_started)


def market_has_ended(market: dict, now) -> bool:
    end_time = parse_datetime(market.get("end_time"))
    return bool(end_time and now >= end_time)


def sample_market(
    market: dict,
    window_start,
    window_end,
    seen_trade_keys_by_condition: dict[str, set[str]],
) -> dict:
    errors = []
    condition_id = market.get("condition_id")
    up_token_id = market.get("yes_token_id")
    down_token_id = market.get("no_token_id")

    up_book, up_error = fetch_orderbook(up_token_id)
    down_book, down_error = fetch_orderbook(down_token_id)
    trades, trades_error = fetch_trades(condition_id)

    if up_error:
        errors.append(f"up_orderbook={up_error}")
    if down_error:
        errors.append(f"down_orderbook={down_error}")
    if trades_error:
        errors.append(f"trades={trades_error}")

    up_metrics = orderbook_metrics(up_book)
    down_metrics = orderbook_metrics(down_book)
    seen_trade_keys = seen_trade_keys_by_condition.setdefault(str(condition_id), set())
    trade_metrics = calculate_trade_window(trades, window_start, window_end, seen_trade_keys)

    orderbook_ok = up_book is not None and down_book is not None
    status = "ok" if orderbook_ok and not errors else ("partial_error" if orderbook_ok else "error")

    return {
        "sampled_at": window_end.isoformat(),
        "event_slug": market.get("event_slug"),
        "event_id": market.get("polymarket_event_id"),
        "market_id": market.get("polymarket_market_id"),
        "condition_id": condition_id,
        "start_time": market.get("start_time"),
        "end_time": market.get("end_time"),
        "up_token_id": up_token_id,
        "down_token_id": down_token_id,
        "up_best_ask": up_metrics["best_ask"],
        "up_best_bid": up_metrics["best_bid"],
        "up_midpoint": up_metrics["midpoint"],
        "up_spread": up_metrics["spread"],
        "up_last_trade_price": up_metrics["last_trade_price"],
        "up_orderbook_timestamp": up_metrics["timestamp"],
        "down_best_ask": down_metrics["best_ask"],
        "down_best_bid": down_metrics["best_bid"],
        "down_midpoint": down_metrics["midpoint"],
        "down_spread": down_metrics["spread"],
        "down_last_trade_price": down_metrics["last_trade_price"],
        "down_orderbook_timestamp": down_metrics["timestamp"],
        **trade_metrics,
        "status": status,
        "error": " | ".join(errors),
    }


def empty_log_row(sampled_at: str, status: str, error: str) -> dict:
    row = {header: "" for header in EVENT_LOG_HEADERS}
    row.update(
        {
            "sampled_at": sampled_at,
            "up_trades_count_window": 0,
            "down_trades_count_window": 0,
            "up_volume_shares_window": 0,
            "down_volume_shares_window": 0,
            "up_volume_usdc_window": 0,
            "down_volume_usdc_window": 0,
            "total_trades_count_window": 0,
            "total_volume_usdc_window": 0,
            "status": status,
            "error": error,
        }
    )
    return row


def sleep_remaining(interval_seconds: int, tick_started: float) -> None:
    elapsed = time.monotonic() - tick_started
    time.sleep(max(0, interval_seconds - elapsed))


if __name__ == "__main__":
    main()
