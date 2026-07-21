from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import asyncio
import json
import random

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

    def dynamic_subscription_message(self, asset_ids: list[str], operation: str = "subscribe") -> dict[str, Any]:
        return {"operation": operation, "assets_ids": asset_ids, "custom_feature_enabled": True}

    async def connect_for_messages(self, url: str, asset_ids: list[str], *, max_messages: int = 1, timeout_seconds: float = 20.0) -> dict[str, Any]:
        """Bounded public smoke connection. It never uses credentials or trading APIs."""
        try:
            import websockets
        except Exception as exc:
            self.mark_disconnect(f"websockets unavailable: {exc}")
            return {"connected": False, "messages": 0, "error": "websockets package is not available"}

        received = 0
        self.status.status = "CONNECTING"
        try:
            async with websockets.connect(url, ping_interval=None, close_timeout=2) as ws:
                self.status.status = "CONNECTED"
                await ws.send(json.dumps(self.subscription_message(asset_ids)))

                async def heartbeat() -> None:
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")

                heartbeat_task = asyncio.create_task(heartbeat())
                try:
                    deadline = asyncio.get_running_loop().time() + timeout_seconds
                    while received < max_messages and asyncio.get_running_loop().time() < deadline:
                        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        if raw == "PONG":
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            payload = {"event_type": "raw", "payload": str(raw)}
                        if isinstance(payload, list):
                            for item in payload:
                                if isinstance(item, dict):
                                    self.process_message(item)
                                    received += 1
                        elif isinstance(payload, dict):
                            self.process_message(payload)
                            received += 1
                finally:
                    heartbeat_task.cancel()
            return {"connected": True, "messages": received, "error": ""}
        except Exception as exc:
            self.mark_disconnect(f"{type(exc).__name__}: {exc}")
            return {"connected": False, "messages": received, "error": f"{type(exc).__name__}: {exc}"}

    def reconnect_delay_seconds(self) -> float:
        base = min(30.0, 2 ** max(0, self.status.reconnect_attempts))
        return base + random.uniform(0, 1)

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
        self.repo.set_state("market_ws_status", self.status.status, "market_ws")
        self.repo.set_state("market_ws_last_message_at", self.status.last_message_at or "", "market_ws")
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
    def __init__(self, repo: LiveRepository, stale_after_seconds: int = 15):
        self.repo = repo
        self.stale_after_seconds = stale_after_seconds
        self.status = WebSocketStatus(channel="user", status="NOT_CONFIGURED")

    def subscription_message(self, condition_ids: list[str], auth_payload: dict[str, str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "user", "markets": condition_ids}
        if auth_payload:
            payload["auth"] = auth_payload
        return payload

    def process_message(self, message: dict[str, Any]) -> bool:
        stored = self.repo.store_ws_event("user", message, "processed")
        self.status.status = "CONNECTED"
        self.status.last_message_at = now_iso()
        self.status.stale = False
        self.repo.set_state("user_ws_status", self.status.status, "user_ws")
        self.repo.set_state("user_ws_last_message_at", self.status.last_message_at or "", "user_ws")
        return stored

    def health(self) -> dict[str, Any]:
        if self.status.last_message_at:
            dt = datetime.fromisoformat(self.status.last_message_at.replace("Z", "+00:00"))
            self.status.stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > self.stale_after_seconds
        return self.status.__dict__
