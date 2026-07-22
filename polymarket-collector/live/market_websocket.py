from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import asyncio
import json
import logging
import os
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
    STATES = {"DISABLED", "CONNECTING", "AUTHENTICATING", "CONNECTED", "STALE", "RECONNECTING", "AUTH_FAILED", "ERROR", "STOPPED"}
    AUTH_KEYS = ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE")

    def __init__(self, repo: LiveRepository, stale_after_seconds: int = 25,
                 reconciliation: Callable[[], Awaitable[Any]] | None = None):
        self.repo, self.stale_after_seconds = repo, stale_after_seconds
        self.status = WebSocketStatus(channel="user", status="DISABLED")
        self.connected_at = self.last_ping_at = self.last_pong_at = None
        self.messages_received = self.order_events_received = self.trade_events_received = 0
        self.subscribed_condition_ids: list[str] = []
        self._task = None
        self._stop = asyncio.Event()
        self._ws = None
        self._lock = asyncio.Lock()
        self._reconciliation = reconciliation
        self._authenticated_signal = False
        self._silent_failures = 0
        self._logger = logging.getLogger("live.user_ws")

    def subscription_message(self, condition_ids, auth_payload=None):
        payload = {"type": "user", "markets": list(dict.fromkeys(condition_ids))}
        if auth_payload:
            payload["auth"] = auth_payload
        return payload

    def dynamic_subscription_message(self, condition_ids, operation="subscribe"):
        if operation not in {"subscribe", "unsubscribe"}:
            raise ValueError("invalid subscription operation")
        return {"operation": operation, "markets": list(dict.fromkeys(condition_ids))}

    def credentials(self):
        values = [os.getenv(name, "").strip() for name in self.AUTH_KEYS]
        return {"apiKey": values[0], "secret": values[1], "passphrase": values[2]} if all(values) else None

    async def start(self, url):
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._stop.clear()
            self._task = asyncio.create_task(self.run(url), name="polymarket-user-ws")

    async def stop(self):
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._set_state("STOPPED")

    async def run(self, url, connect=None):
        creds = self.credentials()
        if not creds:
            self._set_state("AUTH_FAILED", "User WebSocket credentials are missing")
            return
        try:
            import websockets
        except Exception:
            self._set_state("ERROR", "websockets package unavailable")
            return
        connector, attempt = connect or websockets.connect, 0
        while not self._stop.is_set():
            condition_ids = self.repo.user_ws_condition_ids()
            if not condition_ids:
                self._set_state("DISABLED", "No managed BTC 5m condition IDs available")
                await asyncio.sleep(2)
                continue
            self._set_state("CONNECTING" if attempt == 0 else "RECONNECTING")
            try:
                async with connector(url, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    self._authenticated_signal = False
                    self._set_state("AUTHENTICATING")
                    await ws.send(json.dumps(self.subscription_message(condition_ids, creds)))
                    await ws.send("PING")
                    self.last_ping_at = now_iso()
                    self.subscribed_condition_ids, self.connected_at = condition_ids, now_iso()
                    self._set_state("CONNECTED")
                    attempt = 0
                    if self._reconciliation:
                        await self._reconciliation()
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    subscriptions = asyncio.create_task(self._subscription_loop(ws))
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_seconds)
                            except asyncio.TimeoutError:
                                self._set_state("STALE", "User WebSocket receive timeout")
                                raise ConnectionError("stale connection")
                            await self._receive(raw)
                    finally:
                        heartbeat.cancel()
                        subscriptions.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws = None
                error = self._safe_error(exc)
                if self._is_auth_error(error):
                    self._set_state("AUTH_FAILED", "User WebSocket authentication failed")
                    return
                attempt += 1
                if not self._authenticated_signal:
                    self._silent_failures += 1
                    if self._silent_failures >= 2:
                        self._set_state("AUTH_FAILED", "User WebSocket closed before authentication acknowledgement")
                        return
                else:
                    self._silent_failures = 0
                self.status.reconnect_attempts += 1
                self._set_state("RECONNECTING", error)
                try:
                    await asyncio.wait_for(self._stop.wait(), min(30.0, 2 ** min(attempt, 5)) + random.random())
                except asyncio.TimeoutError:
                    pass
        self._set_state("STOPPED")

    async def _heartbeat(self, ws):
        while not self._stop.is_set():
            await asyncio.sleep(10)
            await ws.send("PING")
            self.last_ping_at = now_iso()

    async def _subscription_loop(self, ws):
        while not self._stop.is_set():
            await asyncio.sleep(2)
            wanted = self.repo.user_ws_condition_ids()
            add = [x for x in wanted if x not in self.subscribed_condition_ids]
            remove = [x for x in self.subscribed_condition_ids if x not in wanted]
            if add:
                await ws.send(json.dumps(self.dynamic_subscription_message(add, "subscribe")))
            if remove:
                await ws.send(json.dumps(self.dynamic_subscription_message(remove, "unsubscribe")))
            self.subscribed_condition_ids = wanted

    async def _receive(self, raw):
        self.status.last_message_at, self.status.stale = now_iso(), False
        if raw == "PONG" or raw == b"PONG":
            self._authenticated_signal = True
            self._silent_failures = 0
            self.last_pong_at = now_iso()
            self._persist_state()
            return
        try:
            payload = json.loads(raw)
        except Exception:
            return
        for message in payload if isinstance(payload, list) else [payload]:
            if isinstance(message, dict):
                self._authenticated_signal = True
                self._silent_failures = 0
                if self._is_auth_error(json.dumps(message)):
                    raise PermissionError("authentication failed")
                self.process_message(message)

    def process_message(self, message):
        normalized = self.normalize(message)
        stored = self.repo.store_ws_event("user", normalized, "processed")
        self.messages_received += 1
        if stored and normalized.get("event_type") == "order":
            self.order_events_received += 1
        if stored and normalized.get("event_type") == "trade":
            self.trade_events_received += 1
        self.status.status, self.status.last_message_at, self.status.stale = "CONNECTED", now_iso(), False
        self._persist_state()
        return stored

    def normalize(self, message):
        clean = self.sanitize(message)
        event_type = str(clean.get("event_type") or clean.get("type") or "").lower()
        status = str(clean.get("status") or clean.get("event") or clean.get("message_type") or "").upper()
        condition_id, asset_id = clean.get("market") or clean.get("condition_id"), clean.get("asset_id") or clean.get("token_id")
        original = self._number(clean.get("original_size") or clean.get("size") or clean.get("maker_amount"))
        matched = self._number(clean.get("matched_size") or clean.get("size_matched") or clean.get("taker_amount"))
        remaining = self._number(clean.get("remaining_size"))
        if remaining is None and original is not None and matched is not None:
            remaining = max(0.0, original - matched)
        order_id = clean.get("order_id") or (clean.get("id") if event_type == "order" else None)
        trade_id = clean.get("trade_id") or (clean.get("id") if event_type == "trade" else None)
        return {"event_type": event_type, "message_type": status, "message_status": status,
                "order_id": order_id, "trade_id": trade_id, "condition_id": condition_id, "asset_id": asset_id,
                "outcome": clean.get("outcome") or self.repo.outcome_for_asset(condition_id, asset_id),
                "side": str(clean.get("side") or "").upper() or None, "price": self._number(clean.get("price")),
                "original_size": original, "matched_size": matched, "remaining_size": remaining,
                "liquidity_role": clean.get("trader_side") or clean.get("liquidity_role"),
                "transaction_hash": clean.get("transaction_hash") or clean.get("transactionHash"),
                "event_timestamp": str(clean.get("timestamp") or clean.get("created_at") or clean.get("updated_at") or "") or None,
                "correlation": {k: clean.get(k) for k in ("owner", "maker_order_id", "taker_order_id") if clean.get(k)},
                "raw": clean}

    @classmethod
    def sanitize(cls, value):
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if any(m in key.lower() for m in ("secret", "passphrase", "apikey", "api_key", "private_key", "auth")) else cls.sanitize(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [cls.sanitize(item) for item in value]
        return value

    @staticmethod
    def _number(value):
        try:
            return float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_auth_error(text):
        lowered = text.lower()
        return any(x in lowered for x in ("auth failed", "authentication failed", "invalid api", "unauthorized", "forbidden"))

    @staticmethod
    def _safe_error(exc):
        text = f"{type(exc).__name__}: {exc}"
        for name in UserWebSocketManager.AUTH_KEYS:
            value = os.getenv(name, "")
            if value:
                text = text.replace(value, "[REDACTED]")
        return text[:500]

    def _set_state(self, state, error=None):
        self.status.status = state if state in self.STATES else "ERROR"
        self.status.error = error
        self.status.stale = state in {"STALE", "ERROR", "AUTH_FAILED", "STOPPED"}
        self._persist_state()

    def _persist_state(self):
        self.repo.set_state("user_ws_status", self.status.status, "user_ws")
        self.repo.set_state("user_ws_health", json.dumps(self.health(), sort_keys=True), "user_ws")

    def health(self):
        return {"connected": self.status.status == "CONNECTED", "status": self.status.status,
                "connected_at": self.connected_at, "last_message_at": self.status.last_message_at,
                "last_ping_at": self.last_ping_at, "last_pong_at": self.last_pong_at,
                "last_error": self.status.error, "reconnect_count": self.status.reconnect_attempts,
                "reconnect_attempts": self.status.reconnect_attempts,
                "subscribed_condition_ids": list(self.subscribed_condition_ids),
                "messages_received": self.messages_received, "order_events_received": self.order_events_received,
                "trade_events_received": self.trade_events_received, "stale": self.status.stale}
