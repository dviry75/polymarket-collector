"""Per-email-type recipient preferences, edited from the dashboard.

The dashboard (no SMTP creds, no trading-DB writes) only rewrites
``PREFS_FILE``. Every sender reads it at send time and drops the addresses the
operator switched off for that email type. The file stores *disabled*
addresses, so a missing/corrupt file, or a recipient added later to
``HOURLY_REPORT_RECIPIENTS``, falls back to the old behaviour: send to all.

[CRITICAL ACTION] can never be switched off for every address: saving such a
change is refused, and a file that says so anyway is ignored for that type.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

PREFS_FILE = Path("/opt/polymarket-btc-live/notification-prefs.json")

CRITICAL_ACTION = "critical_action"
HOURLY_REPORT = "hourly_report"
DAILY_REPORT = "daily_report"

EMAIL_TYPES: dict[str, str] = {
    CRITICAL_ACTION: "התראת CRITICAL ACTION",
    HOURLY_REPORT: "דוח שעתי",
    DAILY_REPORT: "דוח יומי (20:00)",
}
ALWAYS_ON_TYPES = frozenset({CRITICAL_ACTION})


class NotificationPrefsError(ValueError):
    pass


def prefs_file() -> Path:
    """Resolved per call so tests can point it at a temp file."""
    override = os.environ.get("NOTIFICATION_PREFS_FILE")
    return Path(override) if override else PREFS_FILE


def load_disabled(path: Path | None = None) -> dict[str, set[str]]:
    """Disabled addresses per email type; empty on any read/parse problem."""
    try:
        data = json.loads((path or prefs_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    disabled = data.get("disabled") if isinstance(data, dict) else None
    if not isinstance(disabled, dict):
        return {}
    result: dict[str, set[str]] = {}
    for kind, values in disabled.items():
        if kind in EMAIL_TYPES and isinstance(values, list):
            result[kind] = {
                str(v).strip().lower() for v in values
                if isinstance(v, str) and v.strip()
            }
    return result


def recipients_for(
    kind: str, recipients: Iterable[str], path: Path | None = None
) -> tuple[str, ...]:
    """The configured recipients that should get ``kind``, in config order."""
    configured = tuple(recipients)
    disabled = load_disabled(path).get(kind, set())
    selected = tuple(r for r in configured if r.strip().lower() not in disabled)
    if not selected and kind in ALWAYS_ON_TYPES:
        return configured
    return selected


def describe(recipients: Iterable[str], path: Path | None = None) -> dict:
    """Dashboard view: every configured address with its per-type switch."""
    configured = list(recipients)
    disabled = load_disabled(path)
    return {
        "types": [
            {"key": key, "label": label, "always_on": key in ALWAYS_ON_TYPES}
            for key, label in EMAIL_TYPES.items()
        ],
        "recipients": [
            {
                "address": address,
                "enabled": {
                    key: address.lower() not in disabled.get(key, set())
                    for key in EMAIL_TYPES
                },
            }
            for address in configured
        ],
    }


def save_enabled(
    enabled: Mapping[str, Mapping[str, bool]],
    recipients: Iterable[str],
    path: Path | None = None,
) -> dict:
    """Persist ``{address: {type: bool}}`` for the configured addresses only."""
    configured = list(recipients)
    if not configured:
        raise NotificationPrefsError("no report recipients are configured on this host")
    known = {r.lower(): r for r in configured}
    for address, switches in enabled.items():
        if address.lower() not in known:
            raise NotificationPrefsError(f"{address!r} is not a configured report address")
        unknown = set(switches) - set(EMAIL_TYPES)
        if unknown:
            raise NotificationPrefsError(f"unknown email type(s): {sorted(unknown)}")
    disabled: dict[str, list[str]] = {}
    for kind in EMAIL_TYPES:
        off = [
            r for r in configured
            if enabled.get(r, enabled.get(r.lower(), {})).get(kind, True) is False
        ]
        if kind in ALWAYS_ON_TYPES and len(off) == len(configured):
            raise NotificationPrefsError(
                f"{EMAIL_TYPES[kind]} must stay on for at least one address"
            )
        disabled[kind] = sorted(r.lower() for r in off)
    target = path or prefs_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "disabled": disabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)  # atomic within the dir
    return describe(configured, target)
