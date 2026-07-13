import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    app_env: str
    polymarket_gamma_events_url: str
    events_fetch_limit: int
    discovery_interval_seconds: int
    csv_events_path: str
    csv_event_logs_path: str
    poll_interval_seconds: int
    run_duration_seconds: int


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw_value}") from exc


def load_config() -> Config:
    load_dotenv()

    return Config(
        app_env=os.getenv("APP_ENV", "local").strip() or "local",
        polymarket_gamma_events_url=(
            os.getenv("POLYMARKET_GAMMA_EVENTS_URL", "https://gamma-api.polymarket.com/events").strip()
            or "https://gamma-api.polymarket.com/events"
        ),
        events_fetch_limit=_get_int_env("EVENTS_FETCH_LIMIT", 100),
        discovery_interval_seconds=_get_int_env("DISCOVERY_INTERVAL_SECONDS", 60),
        csv_events_path=os.getenv("CSV_EVENTS_PATH", "data/events.csv").strip() or "data/events.csv",
        csv_event_logs_path=os.getenv("CSV_EVENT_LOGS_PATH", "data/event_logs.csv").strip() or "data/event_logs.csv",
        poll_interval_seconds=_get_int_env("POLL_INTERVAL_SECONDS", 2),
        run_duration_seconds=_get_int_env("RUN_DURATION_SECONDS", 120),
    )
