"""Weekday afternoon window in which entries need a stricter BTC move.

Until this module existed the window (Mon-Fri 14:00-23:00 Asia/Jerusalem)
closed entries outright. Entries now stay open there, but only with the
stricter BTC gate minimum (``btc_gate_window_min_delta_usd``). The strategy
runtime, the BTC gate and the reporting coverage all read the window from
here so they cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCHEDULE_TIMEZONE = "Asia/Jerusalem"
_TZ = ZoneInfo(SCHEDULE_TIMEZONE)

# Dates on which the window does not apply at all (plain $10 gate all day).
UNRESTRICTED_DATES = frozenset({
    "2026-08-21",
    "2026-09-04",
    "2026-09-07",
})

WINDOW_START_HOUR = 14
WINDOW_END_HOUR = 23  # exclusive


def local_time(at: datetime | None = None) -> datetime:
    return (at or datetime.now(timezone.utc)).astimezone(_TZ)


def in_strict_btc_window(at: datetime | None = None) -> bool:
    """True Mon-Fri (Python weekday 0-4) 14:00:00-22:59:59 Asia/Jerusalem."""
    local = local_time(at)
    return (
        local.weekday() < 5
        and WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR
        and local.date().isoformat() not in UNRESTRICTED_DATES
    )


def in_strict_btc_window_ms(epoch_ms: int) -> bool:
    return in_strict_btc_window(
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    )
