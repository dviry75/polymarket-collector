from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import asyncio
import json
import logging
import os
import random
from time import monotonic

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
    def __init__(
        self,
        repo: LiveRepository,
        stale_after_seconds: int = 30,
        on_snapshot: Callable[[dict[str, Any]], Any] | None = None,
        raw_events_enabled: bool = True,
    ):
        self.repo = repo
        self.stale_after_seconds = stale_after_seconds
        self.status = WebSocketStatus(channel="market")
        self.on_snapshot = on_snapshot
        self.raw_events_enabled = bool(raw_events_enabled)
        self.subscribed_asset_ids: list[str] = []
        self.messages_received = 0
        self.snapshots_received = 0
        self.snapshots_persisted = 0
        self.last_ping_at = self.last_pong_at = None
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._ws = None
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger("live.market_ws")
        self._last_message_state_persisted_at = 0.0

    def subscription_message(self, asset_ids: list[str]) -> dict[str, Any]:
        return {"type": "market", "assets_ids": asset_ids, "custom_feature_enabled": True}

    def dynamic_subscription_message(self, asset_ids: list[str], operation: str = "subscribe") -> dict[str, Any]:
        if operation not in {"subscribe", "unsubscribe"}:
            raise ValueError("invalid subscription operation")
        return {"operation": operation, "assets_ids": asset_ids, "custom_feature_enabled": True}

    async def start(self, url: str) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._stop.clear()
            self._task = asyncio.create_task(self.run(url), name="polymarket-market-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self.status.status = "STOPPED"
        self.status.stale = True

    async def run(self, url: str, connect=None) -> None:
        if connect is None:
            try:
                import websockets
            except Exception:
                self.mark_disconnect("websockets package unavailable")
                return
            connector = websockets.connect
        else:
            connector = connect
        attempt = 0
        while not self._stop.is_set():
            asset_ids = self.repo.market_ws_asset_ids()
            if not asset_ids:
                self.status.status = "WAITING_FOR_MARKETS"
                try:
                    await asyncio.wait_for(self._stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            self.status.status = "CONNECTING" if attempt == 0 else "RECONNECTING"
            try:
                async with connector(url, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(self.subscription_message(asset_ids)))
                    self.subscribed_asset_ids = asset_ids
                    self.status.status = "CONNECTED"
                    self.status.error = None
                    attempt = 0
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    subscriptions = asyncio.create_task(self._subscription_loop(ws))
                    try:
                        while not self._stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=max(15, self.stale_after_seconds))
                            if raw == "PONG" or raw == b"PONG":
                                self.last_pong_at = now_iso()
                                continue
                            payload = json.loads(raw)
                            for message in payload if isinstance(payload, list) else [payload]:
                                if isinstance(message, dict):
                                    self.process_message(message)
                            # A continuously readable socket may otherwise monopolize the
                            # event loop while snapshots are persisted synchronously.
                            await asyncio.sleep(0)
                    finally:
                        heartbeat.cancel()
                        subscriptions.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws = None
                attempt += 1
                self.mark_disconnect(f"{type(exc).__name__}: {exc}"[:500])
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), min(30.0, 2 ** min(attempt, 5)) + random.random()
                    )
                except asyncio.TimeoutError:
                    pass
        self.status.status = "STOPPED"

    async def _heartbeat(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            await ws.send("PING")
            self.last_ping_at = now_iso()

    async def _subscription_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1)
            wanted = self.repo.market_ws_asset_ids()
            add = [asset for asset in wanted if asset not in self.subscribed_asset_ids]
            remove = [asset for asset in self.subscribed_asset_ids if asset not in wanted]
            if add or remove:
                # Reconnect so every rotating BTC 5m market receives a fresh initial
                # book. In production the server accepted dynamic updates but did not
                # reliably emit the newly subscribed assets.
                await ws.close()
                return

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

    @staticmethod
    def _is_technical_message(message: dict[str, Any]) -> bool:
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        return event_type in {"", "heartbeat", "ping", "pong", "subscribed", "subscription"}

    def process_message(self, message: dict[str, Any]) -> bool:
        stored_event = False
        if self.raw_events_enabled and not self._is_technical_message(message):
            stored_event = self.repo.store_ws_event("market", message, "processed")
        snapshots = self._normalize_snapshots(message)
        stored_snapshots = 0
        for candidate in snapshots:
            stored = self.repo.store_market_snapshot(candidate)
            realtime_snapshot = dict(candidate)
            realtime_snapshot["id"] = stored.get("id") if stored else None
            realtime_snapshot["_persisted"] = bool(stored)
            self.snapshots_received += 1
            if stored is not None:
                self.snapshots_persisted += 1
                stored_snapshots += 1
            if self.on_snapshot is not None:
                self.on_snapshot(realtime_snapshot)
        self.messages_received += 1
        self.status.status = "CONNECTED"
        self.status.last_message_at = now_iso()
        self.status.stale = False
        if (message.get("event_type") or message.get("type")) == "market_resolved":
            condition_id = message.get("condition_id") or message.get("market")
            if condition_id:
                self.repo.mark_market_resolved(
                    str(condition_id),
                    message.get("winning_asset_id"),
                    message.get("winning_outcome"),
                )
        self._persist_message_state()
        return stored_event or stored_snapshots > 0

    def _persist_message_state(self) -> None:
        try:
            self.repo.set_state("market_ws_status", self.status.status, "market_ws")
            current = monotonic()
            if current - self._last_message_state_persisted_at >= 1.0:
                self.repo.set_state(
                    "market_ws_last_message_at", self.status.last_message_at or "", "market_ws",
                    audit_change=False,
                )
                self._last_message_state_persisted_at = current
        except Exception as exc:
            self._logger.warning(
                "market WebSocket health persistence failed: %s", type(exc).__name__
            )

    def _normalize_snapshots(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        event_type = str(message.get("event_type") or message.get("type") or "").lower()
        if event_type == "market_resolved":
            condition_id = str(message.get("condition_id") or message.get("market") or "")
            market = self.repo.latest_market(condition_id) if condition_id else None
            if not market:
                return []
            winning_asset = str(message.get("winning_asset_id") or "")
            timestamp = str(message.get("timestamp") or "") or None
            received_at = now_iso()
            results = []
            for index, (asset_id, outcome) in enumerate((
                (market.get("yes_token_id"), "YES"),
                (market.get("no_token_id"), "NO"),
            )):
                if not asset_id:
                    continue
                payout = 1.0 if str(asset_id) == winning_asset else 0.0
                identity = {"message": message, "asset_id": str(asset_id), "index": index}
                results.append({
                    "condition_id": condition_id,
                    "event_id": market.get("event_id"),
                    "asset_id": str(asset_id),
                    "outcome": outcome,
                    "event_type": event_type,
                    "best_bid": payout,
                    "best_ask": payout,
                    "market_timestamp": timestamp,
                    "received_at": received_at,
                    "latency_ms": self._latency_ms(timestamp),
                    "source": "POLYMARKET_MARKET_WS",
                    "message_hash": __import__("hashlib").sha256(
                        json.dumps(identity, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "raw_message": identity,
                })
            return results
        if event_type not in {"book", "best_bid_ask", "price_change"}:
            return []
        items = message.get("price_changes") if event_type == "price_change" else [message]
        if not isinstance(items, list):
            return []
        snapshots: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or message.get("asset_id") or "")
            market = self.repo.market_for_asset(asset_id) if asset_id else None
            if not market:
                continue
            bids = message.get("bids") if event_type == "book" else []
            asks = message.get("asks") if event_type == "book" else []
            bids = bids if isinstance(bids, list) else []
            asks = asks if isinstance(asks, list) else []
            best_bid, best_bid_size = self._best_level(bids, highest=True)
            best_ask, best_ask_size = self._best_level(asks, highest=False)
            if event_type != "book":
                best_bid = self._number(item.get("best_bid"))
                best_ask = self._number(item.get("best_ask"))
            timestamp = str(message.get("timestamp") or item.get("timestamp") or "") or None
            received_at = now_iso()
            latency_ms = self._latency_ms(timestamp)
            outcome = (
                "YES" if str(market.get("yes_token_id")) == asset_id
                else "NO" if str(market.get("no_token_id")) == asset_id
                else None
            )
            raw_with_identity = {"message": message, "asset_id": asset_id, "index": index}
            snapshots.append({
                "condition_id": market["condition_id"],
                "event_id": market.get("event_id"),
                "asset_id": asset_id,
                "outcome": outcome,
                "event_type": event_type,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "best_bid_size": best_bid_size,
                "best_ask_size": best_ask_size,
                "bids": bids,
                "asks": asks,
                "market_timestamp": timestamp,
                "received_at": received_at,
                "latency_ms": latency_ms,
                "source": "POLYMARKET_MARKET_WS",
                "message_hash": __import__("hashlib").sha256(
                    json.dumps(raw_with_identity, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "raw_message": raw_with_identity,
            })
        return snapshots

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _best_level(cls, levels: list[Any], *, highest: bool) -> tuple[float | None, float | None]:
        parsed = [
            (cls._number(level.get("price")), cls._number(level.get("size")))
            for level in levels if isinstance(level, dict)
        ]
        valid = [(price, size) for price, size in parsed if price is not None and (size or 0) > 0]
        if not valid:
            return None, None
        chooser = max if highest else min
        return chooser(valid, key=lambda level: level[0])

    @staticmethod
    def _latency_ms(timestamp: str | None) -> int | None:
        if not timestamp:
            return None
        try:
            source_ms = int(float(timestamp))
            if source_ms < 10_000_000_000:
                source_ms *= 1000
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            return max(0, now_ms - source_ms)
        except (TypeError, ValueError):
            return None

    def mark_disconnect(self, error: str = "") -> None:
        self.status.status = "DISCONNECTED"
        self.status.reconnect_attempts += 1
        self.status.stale = True
        self.status.error = error or None
        try:
            self.repo.set_state("market_ws_status", self.status.status, "market_ws")
        except Exception as exc:
            # Persistence is diagnostic here. A temporary SQLite lock must not
            # terminate the reconnect loop that restores the realtime feed.
            self._logger.warning(
                "market WebSocket state persistence failed during reconnect: %s",
                type(exc).__name__,
            )

    def health(self) -> dict[str, Any]:
        stale = True
        if self.status.last_message_at:
            dt = datetime.fromisoformat(self.status.last_message_at.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > self.stale_after_seconds
        self.status.stale = stale
        return {
            **self.status.__dict__,
            "subscribed_asset_ids": list(self.subscribed_asset_ids),
            "messages_received": self.messages_received,
            "snapshots_received": self.snapshots_received,
            "snapshots_persisted": self.snapshots_persisted,
            "last_ping_at": self.last_ping_at,
            "last_pong_at": self.last_pong_at,
        }


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
        try:
            self.repo.set_state("user_ws_status", self.status.status, "user_ws")
            self.repo.set_state(
                "user_ws_health", json.dumps(self.health(), sort_keys=True), "user_ws",
                audit_change=False,
            )
        except Exception as exc:
            self._logger.warning(
                "user WebSocket health persistence failed: %s", type(exc).__name__
            )

    def health(self):
        return {"connected": self.status.status == "CONNECTED", "status": self.status.status,
                "connected_at": self.connected_at, "last_message_at": self.status.last_message_at,
                "last_ping_at": self.last_ping_at, "last_pong_at": self.last_pong_at,
                "last_error": self.status.error, "reconnect_count": self.status.reconnect_attempts,
                "reconnect_attempts": self.status.reconnect_attempts,
                "subscribed_condition_ids": list(self.subscribed_condition_ids),
                "messages_received": self.messages_received, "order_events_received": self.order_events_received,
                "trade_events_received": self.trade_events_received, "stale": self.status.stale}
