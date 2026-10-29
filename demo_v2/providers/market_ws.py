"""Public market-only transport. No LIVE repository, account WS or credentials."""

import asyncio
from datetime import datetime, timezone
import json
import random

from ..domain.models import MarketObservation
from ..streams import ObservationRejected
from .book_primitives import OrderBookSet, decimal_value


MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PublicMarketWS:
    def __init__(self, decisions, telemetry, *, generation_factory, clock=lambda:datetime.now(timezone.utc),
                 stale_seconds=5.0, connector=None, on_issue=lambda code:None, heartbeat_seconds=10.0):
        self.decisions, self.telemetry = decisions, telemetry
        self.generation_factory, self.clock = generation_factory, clock
        self.stale_seconds, self.connector, self.on_issue = stale_seconds, connector, on_issue
        self.heartbeat_seconds = heartbeat_seconds
        self._connected_at = None
        self.books = OrderBookSet()
        self.instances = {}
        self.token_map = {}
        self.latest = {}
        self.generation = self.sequence = self.wire_sequence = 0
        self.status = "STOPPED"
        self.reason = "NOT_CONNECTED"
        self._ws = None
        self._stop = asyncio.Event()
        self._changed = asyncio.Event()

    async def set_subscriptions(self, instances):
        instances = tuple(instances)
        mapping = {}
        for instance in instances:
            if not instance.verified:
                raise ValueError("cannot subscribe unverified token mappings")
            for token in (instance.up_token_id,instance.down_token_id):
                if token in mapping and mapping[token].instance_id != instance.instance_id:
                    raise ValueError("ambiguous token subscription")
                mapping[token] = instance
        old, wanted = set(self.token_map), set(mapping)
        self.instances = {i.instance_id:i for i in instances}
        self.token_map = mapping
        if old == wanted:
            return
        self.books.ensure_assets(wanted)
        self.latest = {token:value for token,value in self.latest.items() if token in wanted}
        self._changed.set()
        if self._ws is not None:
            add, remove = sorted(wanted-old), sorted(old-wanted)
            if add:
                await self._ws.send(json.dumps({"operation":"subscribe","assets_ids":add,"custom_feature_enabled":True}))
            if remove:
                await self._ws.send(json.dumps({"operation":"unsubscribe","assets_ids":remove}))
            # Conservative resync: request complete snapshots for the final
            # wanted set in a new generation; held old-instance tokens stay in it.
            self.decisions.invalidate("SUBSCRIPTION_RESYNC")
            await self._ws.close()

    def begin_generation(self):
        self.generation = self.generation_factory()
        self.sequence = self.wire_sequence = 0
        self.latest.clear()
        self.books.ensure_assets(self.token_map)
        self.books.mark_not_ready("AWAITING_SNAPSHOT",source_generation=self.generation)
        self.decisions.begin_generation(self.generation)
        self.status, self.reason = "CONNECTED", "AWAITING_FRESH_BOOKS"
        self._connected_at = self.clock()

    def process_message(self, message: dict, *, source_generation=None):
        if self.status != "CONNECTED" or (source_generation is not None and source_generation != self.generation):
            return []
        kind = str(message.get("event_type") or "")
        if kind not in {"book","price_change","best_bid_ask"}:
            return []
        self.wire_sequence += 1
        now = self.clock()
        frame = self.books.apply(message,now_ms=int(now.timestamp()*1000),max_age_ms=int(self.stale_seconds*1000),
            source_generation=self.generation,wire_sequence=self.wire_sequence,include_depth=False)
        if frame.rejected_reason or frame.duplicate or frame.out_of_order:
            if frame.rejected_reason:
                self.reason = frame.rejected_reason
                self.on_issue(self.reason)
            return []
        transitions = sorted(frame.top_transitions,key=lambda view:view.get("_raw_change_index",0))
        seen = {view["asset_id"] for view in transitions}
        views = [*transitions,*(view for view in frame.updates if view["asset_id"] not in seen)]
        emitted = []
        for view in views:
            token = str(view["asset_id"])
            instance = self.token_map.get(token)
            book = self.books.books.get(token)
            if not instance or not book or not book.ready or not view.get("book_ready"):
                continue
            condition = message.get("market")
            if condition and condition != instance.condition_id:
                self.decisions.invalidate("TOKEN_CONDITION_MISMATCH")
                raise ObservationRejected("TOKEN_CONDITION_MISMATCH")
            if frame.exchange_timestamp_ms is None:
                continue
            self.sequence += 1
            observation = MarketObservation(instance.instance_id,token,self.generation,self.sequence,
                datetime.fromtimestamp(frame.exchange_timestamp_ms/1000,timezone.utc),now,"POLYMARKET_PUBLIC_WS",
                decimal_value(view.get("best_bid")),decimal_value(view.get("best_ask")))
            self.decisions.publish(observation)
            self.latest[token] = observation
            self.telemetry.offer(observation)
            emitted.append(observation)
        if emitted:
            self.reason = ""
        return emitted

    def token_ready(self, token):
        observation = self.latest.get(token)
        return bool(self.status == "CONNECTED" and not self.decisions.reason and observation
            and observation.generation == self.generation and observation.book_ready
            and 0 <= (self.clock()-observation.observed_at).total_seconds() <= self.stale_seconds
            and 0 <= (self.clock()-observation.received_at).total_seconds() <= self.stale_seconds)

    def health(self):
        return {"status":self.status,"generation":self.generation,"reason":self.reason,
                "tokens":{token:self.token_ready(token) for token in self.token_map}}

    async def _heartbeat(self, ws):
        while not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            await ws.send("PING")

    async def _receive(self, ws):
        while not self._stop.is_set():
            raw = await asyncio.wait_for(ws.recv(),timeout=self.stale_seconds)
            if raw in ("PONG","PING"):
                if raw == "PING":
                    await ws.send("PONG")
                if self._connected_at and (self.clock()-self._connected_at).total_seconds() >= self.stale_seconds and any(not self.token_ready(token) for token in self.token_map):
                    raise ObservationRejected("MARKET_DATA_STALE")
                continue
            messages = json.loads(raw)
            for message in messages if isinstance(messages,list) else [messages]:
                if not isinstance(message,dict):
                    raise ValueError("invalid public WS frame")
                self.process_message(message,source_generation=self.generation)
            if self._connected_at and (self.clock()-self._connected_at).total_seconds() >= self.stale_seconds and any(not self.token_ready(token) for token in self.token_map):
                raise ObservationRejected("MARKET_DATA_STALE_OR_MISSING")

    async def run(self):
        if self.connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        else:
            connector = self.connector
        attempt = 0
        self._stop.clear()
        while not self._stop.is_set():
            if not self.token_map:
                self.status = "WAITING_FOR_DEMAND"
                self._changed.clear()
                await self._changed.wait()
                continue
            try:
                self.status = "CONNECTING"
                async with connector(MARKET_WS_URL,ping_interval=None,max_queue=32,close_timeout=2) as ws:
                    self._ws = ws
                    self.begin_generation()
                    await ws.send(json.dumps({"type":"market","assets_ids":sorted(self.token_map),"custom_feature_enabled":True}))
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    attempt = 0
                    try:
                        await self._receive(ws)
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat,return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.status, self.reason = "DISCONNECTED",type(exc).__name__
                self.decisions.invalidate("MARKET_WS_DISCONNECTED")
                self.on_issue("MARKET_WS_DISCONNECTED")
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(),timeout=min(30,2**min(attempt,4))+random.random())
                except asyncio.TimeoutError:
                    pass
            finally:
                self._ws = None
        self.status = "STOPPED"
        self.decisions.invalidate("MARKET_WS_STOPPED")

    async def close(self):
        self._stop.set()
        self._changed.set()
        self.decisions.invalidate("MARKET_WS_STOPPED")
        if self._ws is not None:
            await self._ws.close()

    async def resync(self,reason):
        self.decisions.invalidate(reason)
        self.latest.clear()
        if self._ws is not None:
            await self._ws.close()
