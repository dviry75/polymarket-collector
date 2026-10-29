import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import httpx
import pytest

from demo_v2.domain.models import market_definitions
from demo_v2.providers.discovery import GammaDiscovery, GammaResolution, candidate_family, labeled_tokens, parse_instance
from demo_v2.providers.market_ws import PublicMarketWS
from demo_v2.providers.public_http import PublicHTTP
from demo_v2.providers.subscriptions import current_instances, subscription_demand
from demo_v2.streams import DecisionStream, ObservationRejected, TelemetryBuffer
from conftest import open_position


NOW = datetime(2026,10,29,12,tzinfo=timezone.utc)


def event_for(definition, index=1):
    market = {"id":str(index),"conditionId":"0x"+f"{index:064x}",
        "outcomes":'["Down","Up"]',"clobTokenIds":json.dumps([str(2*index),str(2*index+1)]),
        "eventStartTime":(NOW-timedelta(seconds=1)).isoformat(),
        "endDate":(NOW+timedelta(seconds=definition.expected_duration-1)).isoformat(),
        "question":f"{definition.asset.value} Up or Down", "slug":f"verified-{index}",
        "active":True,"closed":False,"acceptingOrders":True,"enableOrderBook":True}
    return {"slug":market["slug"],"series":[{"id":definition.key,"slug":candidate_family(definition)}],"markets":[market]}


def test_explicit_labels_are_used_instead_of_first_token_guessing():
    definition = market_definitions()[0]
    event = event_for(definition)
    instance, _ = parse_instance(event,definition,definition.key,NOW)
    assert instance.up_token_id == "3" and instance.down_token_id == "2"
    assert instance.starts_at == NOW-timedelta(seconds=1)
    for invalid in ({"outcomes":'["Yes","No"]'}, {"clobTokenIds":'["2","2"]'},
                    {"eventStartTime":"2026-10-29T00:00:00Z"}, {"conditionId":"invalid"}):
        changed = {**event,"markets":[{**event["markets"][0],**invalid}]}
        with pytest.raises(ValueError):
            parse_instance(changed,definition,definition.key,NOW)


def test_all_21_families_and_ambiguous_family_fail_closed():
    definitions = market_definitions()
    class FakeHTTP:
        duplicate = False
        async def get(self,path,params=None):
            if path == "/series":
                return [{"id":d.key,"slug":candidate_family(d),"recurrence":"hourly" if d.timeframe.value=="1h" else d.timeframe.value} for d in definitions]
            definition = next(d for d in definitions if d.key==params["series_id"])
            event = event_for(definition,definitions.index(definition)+1)
            return [event,event] if self.duplicate and definition.key=="BTC-5m" else [event]
    async def run():
        http=FakeHTTP()
        discovery=GammaDiscovery(http,clock=lambda:NOW)
        assert len(await discovery.discover(definitions)) == 21
        http.duplicate=True
        assert len(await discovery.discover(definitions)) == 20
        assert discovery.issues["BTC-5m"] == "AMBIGUOUS_CURRENT_INSTANCE"
    asyncio.run(run())


def test_unverified_missing_family_is_unavailable():
    class FakeHTTP:
        async def get(self,path,params=None): return []
    discovery=GammaDiscovery(FakeHTTP(),clock=lambda:NOW)
    assert asyncio.run(discovery.discover(market_definitions())) == []
    assert len(discovery.issues) == 21


def manager_for(market, capacity=20):
    clock=lambda:NOW
    stream=DecisionStream(capacity=capacity,clock=clock)
    generation=iter(range(1,100))
    manager=PublicMarketWS(stream,TelemetryBuffer(1),generation_factory=lambda:next(generation),clock=clock)
    asyncio.run(manager.set_subscriptions([market]))
    manager.begin_generation()
    return manager,stream


def snapshot(market,token,ask="0.75",bid="0.73",stamp=None):
    return {"event_type":"book","asset_id":token,"market":market.condition_id,
        "timestamp":str(int((stamp or NOW).timestamp()*1000)),
        "bids":[{"price":bid,"size":"10"}],"asks":[{"price":ask,"size":"10"}]}


def test_snapshot_delta_zero_delete_and_all_exact_prices_reach_decisions(market):
    manager,stream=manager_for(market)
    manager.process_message(snapshot(market,market.up_token_id))
    for price in ("0.74","0.73"):
        manager.process_message({"event_type":"price_change","market":market.condition_id,
            "timestamp":str(int(NOW.timestamp()*1000)),"price_changes":[{
                "asset_id":market.up_token_id,"side":"SELL","price":price,"size":"5",
                "best_bid":"0.73","best_ask":price}]})
    async def read():
        return [str((await stream.next_observation()).best_ask) for _ in range(3)]
    assert asyncio.run(read()) == ["0.75","0.74","0.73"]
    assert len(manager.telemetry.drain()) == 1
    manager.process_message({"event_type":"price_change","market":market.condition_id,
        "timestamp":str(int(NOW.timestamp()*1000)),"price_changes":[{
            "asset_id":market.up_token_id,"side":"SELL","price":"0.73","size":"0",
            "best_bid":"0.73","best_ask":"0.74"}]})
    assert manager.books.books[market.up_token_id].best_ask == Decimal("0.74")


def test_intermediate_top_transition_survives_one_frame(market):
    manager,stream=manager_for(market)
    book=snapshot(market,market.up_token_id,ask="0.73")
    book["asks"]=[{"price":p,"size":"10"} for p in ("0.73","0.74","0.75")]
    manager.process_message(book)
    manager.process_message({"event_type":"price_change","market":market.condition_id,
        "timestamp":book["timestamp"],"price_changes":[{
            "asset_id":market.up_token_id,"side":"SELL","price":p,"size":"0",
            "best_bid":"0.73","best_ask":ask} for p,ask in [("0.73","0.74"),("0.74","0.75")]]})
    async def read(): return [(await stream.next_observation()).best_ask for _ in range(3)]
    assert asyncio.run(read()) == list(map(Decimal,["0.73","0.74","0.75"]))


def test_dedup_generation_and_stale_data_never_replay(market):
    manager,stream=manager_for(market)
    book=snapshot(market,market.up_token_id)
    assert manager.process_message(book)
    assert manager.process_message(book) == []
    manager.begin_generation()
    assert manager.process_message(book,source_generation=1) == []
    assert manager.process_message(snapshot(market,market.up_token_id,stamp=NOW-timedelta(seconds=20))) == []
    assert not manager.token_ready(market.up_token_id)
    assert manager.process_message(book)
    assert asyncio.run(stream.next_observation()).generation == 2


def test_overflow_invalidates_stream(market):
    manager,stream=manager_for(market,capacity=1)
    manager.process_message(snapshot(market,market.up_token_id))
    with pytest.raises(ObservationRejected,match="OVERFLOW"):
        manager.process_message(snapshot(market,market.up_token_id,ask="0.74"))
    assert not manager.token_ready(market.up_token_id)


def test_demand_holds_old_position_through_rollover_and_unsubscribes_no_demand(repository,market,rule):
    position=open_position(repository,rule)
    old=replace(market,starts_at=NOW-timedelta(hours=1),ends_at=NOW)
    new=replace(market,instance_id="new",up_token_id="new-up",down_token_id="new-down",starts_at=NOW,ends_at=NOW+timedelta(hours=1))
    wanted=subscription_demand([old,new],[rule],[position],NOW,lambda *args:True)
    assert {i.instance_id for i in wanted} == {old.instance_id,new.instance_id}
    assert subscription_demand([old,new],[],[],NOW,lambda *args:True) == ()
    assert subscription_demand([old,new],[rule],[],NOW,lambda *args:False) == ()
    assert current_instances([new,replace(new,instance_id="overlap")],NOW) == {}


def test_dynamic_subscribe_unsubscribe_and_clean_shutdown(market):
    manager,stream=manager_for(market)
    class FakeWS:
        def __init__(self): self.sent=[];self.closed=False
        async def send(self,raw): self.sent.append(json.loads(raw))
        async def close(self): self.closed=True
    async def run():
        ws=FakeWS();manager._ws=ws
        newer=replace(market,instance_id="next",up_token_id="next-up",down_token_id="next-down")
        await manager.set_subscriptions([market,newer])
        assert ws.sent[0]["operation"] == "subscribe"
        assert market.up_token_id in manager.token_map
        assert ws.closed
        await manager.set_subscriptions([newer])
        assert ws.sent[-1]["operation"] == "unsubscribe"
        assert "custom_feature_enabled" not in ws.sent[-1]
        await manager.close()
        assert manager._stop.is_set()
    asyncio.run(run())


def test_fake_socket_run_subscribes_dispatches_heartbeats_and_stops(market):
    class FakeWS:
        def __init__(self): self.sent=[];self.queue=asyncio.Queue()
        async def send(self,raw): self.sent.append(raw)
        async def recv(self):
            message=await self.queue.get()
            if message is None: raise ConnectionError("fake close")
            return message
        async def close(self): self.queue.put_nowait(None)
    async def run():
        ws=FakeWS()
        class Connection:
            async def __aenter__(self): return ws
            async def __aexit__(self,*args): await ws.close()
        stream=DecisionStream(clock=lambda:NOW)
        manager=PublicMarketWS(stream,TelemetryBuffer(),generation_factory=lambda:1,clock=lambda:NOW,
            connector=lambda *args,**kwargs:Connection(),heartbeat_seconds=0.001)
        await manager.set_subscriptions([market])
        ws.queue.put_nowait(json.dumps(snapshot(market,market.up_token_id,ask="0.74")))
        task=asyncio.create_task(manager.run())
        for _ in range(100):
            if manager.status=="CONNECTED": break
            await asyncio.sleep(0)
        observed=await asyncio.wait_for(stream.next_observation(),1)
        assert observed.best_ask==Decimal("0.74")
        await asyncio.sleep(0.005)
        assert "PING" in ws.sent
        assert json.loads(ws.sent[0])["type"]=="market"
        await manager.close()
        await asyncio.wait_for(task,1)
        assert manager.status=="STOPPED"
    asyncio.run(run())


def test_official_resolution_requires_verified_status_and_matching_ids():
    definition=market_definitions()[0];event=event_for(definition)
    instance,_=parse_instance(event,definition,definition.key,NOW)
    class FakeHTTP:
        async def get(self,path,params=None):
            assert path == "/markets/"+instance.external_id
            return event["markets"][0]
    provider=GammaResolution(FakeHTTP(),clock=lambda:NOW)
    assert asyncio.run(provider.resolve(instance)) is None
    event["markets"][0].update(closed=True,umaResolutionStatus="resolved",outcomePrices='["0","1"]')
    assert asyncio.run(provider.resolve(instance)).winner_token_id == instance.up_token_id
    event["markets"][0]["outcomePrices"]='["0.5","0.5"]'
    assert asyncio.run(provider.resolve(instance)) is None


def test_public_transport_has_no_write_surface_and_rejects_account_endpoints():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"ok":True}))) as client:
            public=PublicHTTP("https://clob.polymarket.com",client=client)
            assert not hasattr(public,"post")
            assert await public.get("/book",params={"token_id":"123"}) == {"ok":True}
            for path in ("//attacker/", "/auth/api-key", "/orders", "https://attacker/"):
                with pytest.raises(ValueError): await public.get(path)
    asyncio.run(run())
