from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .repository import LiveRepository, now_iso


@dataclass
class WebSocketStatus:
    channel: str
    status: str = "NOT_CONNECTED"
    last_message_at: str | None = None
    reconnect_attempts: int = 0
    stale: bool = True
    error: str | None = None


class MarketWebSocketManager:
    def __init__(self, repo: LiveRepository, stale_after_seconds: int = 30):
        self.repo = repo
        self.stale_after_seconds = stale_after_seconds
        self.status = WebSocketStatus(channel="market")

    def subscription_message(self, asset_ids: list[str]) -> dict[str, Any]:
        return {"type": "market", "assets_ids": asset_ids, "custom_feature_enabled": True}

    def process_message(self, message: dict[str, Any]) -> bool:
        stored = self.repo.store_ws_event("market", message, "processed")
        self.status.status = "CONNECTED"
        self.status.last_message_at = now_iso()
        self.status.stale = False
        if (message.get("event_type") or message.get("type")) == "market_resolved":
            condition_id = message.get("condition_id") or message.get("market")
            if condition_id:
                current = self.repo.latest_market(str(condition_id)) or {"condition_id": condition_id}
                current.update({
                    "market_resolved": True,
                    "winning_asset_id": message.get("winning_asset_id"),
                    "winning_outcome": message.get("winning_outcome"),
                    "source": "market_websocket",
                    "last_update_at": now_iso(),
                })
                self.repo.upsert_market(current)
        return stored

    def mark_disconnect(self, error: str = "") -> None:
        self.status.status = "DISCONNECTED"
        self.status.reconnect_attempts += 1
        self.status.stale = True
        self.status.error = error or None

    def health(self) -> dict[str, Any]:
        stale = True
        if self.status.last_message_at:
            dt = datetime.fromisoformat(self.status.last_message_at.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > self.stale_after_seconds
        self.status.stale = stale
        return self.status.__dict__


class UserWebSocketManager:
    def __init__(self, repo: LiveRepository):
        self.repo = repo
        self.status = WebSocketStatus(channel="user", status="NOT_CONFIGURED")

    def subscription_message(self, condition_ids: list[str]) -> dict[str, Any]:
        return {"type": "user", "markets": condition_ids}

    def process_message(self, message: dict[str, Any]) -> bool:
        stored = self.repo.store_ws_event("user", message, "processed")
        self.status.status = "CONNECTED"
        self.status.last_message_at = now_iso()
        self.status.stale = False
        return stored

    def health(self) -> dict[str, Any]:
        return self.status.__dict__

