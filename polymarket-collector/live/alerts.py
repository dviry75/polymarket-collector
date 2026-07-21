from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .repository import LiveRepository


ALERT_TYPES = {
    "order_submitted",
    "order_matched",
    "order_confirmed",
    "order_failed",
    "stop_loss_partial",
    "stop_loss_failed",
    "websocket_disconnected",
    "websocket_stale",
    "reconciliation_gap",
    "account_identity_mismatch",
    "daily_loss_limit",
    "consecutive_failures",
    "consecutive_losses",
    "kill_switch_activated",
    "market_token_mismatch",
    "below_minimum_order_size",
}


@dataclass
class NoopAlertProvider:
    repo: LiveRepository

    def emit(self, alert_type: str, payload: dict[str, Any]) -> None:
        status = "ok" if alert_type in ALERT_TYPES else "unknown_type"
        self.repo.audit("system", f"alert:{alert_type}", status, details=payload)
