"""Temp-DB correctness, process/lock failure and scoped-feed regressions."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import multiprocessing
import os
import time

import pytest

from demo_v2.config import DemoConfig
from demo_v2.decision_worker import CachedEngine, DecisionOwner, DecisionWorker, DecisionWorkerUnavailable, HotRepository
from demo_v2.domain.models import market_definitions
from demo_v2.domain.trading import RuleSpec
from demo_v2.health import ComponentHealth, HealthState
from demo_v2.providers.contracts import Providers
from demo_v2.providers.market_pool import MarketDataPool
from demo_v2.providers.market_ws import PublicMarketWS
from demo_v2.streams import DecisionStream, ObservationRejected, TelemetryBuffer
from conftest import quote
from test_public_pipeline import NOW,snapshot,manager_for
from test_runtime import runtime


def cached(repository):
    repo=HotRepository(repository.database)
    engine=CachedEngine(repo)
    engine.begin_generation(repository.next_generation())
    return engine


def payload(owner, market, rule, observations, stream="default", epoch=None):
    return (stream,observations,{market.instance_id:market},{},
        {(rule["id"],market.instance_id):True},
        {market.instance_id:datetime.now(timezone.utc)+timedelta(seconds=5)},
        owner.gate_epoch if epoch is None else epoch,{}, {},None)


def test_idle_observations_do_not_connect_or_write_sql(repository,market,rule,monkeypatch):
    engine=cached(repository)
    def no_database():
        raise AssertionError("no-op observations must not access SQL")
    monkeypatch.setattr(repository.database,"connect",no_database)
    for sequence in range(1,10001):
        engine.process(quote(sequence,ask="0.75"),market)
    assert engine.last_sequence==10000 and engine.commits==0 and not engine.reason


def test_cached_every_transition_shared_quota_and_no_repeat(repository,market,rule):
    engine=cached(repository)
    for seq,price in enumerate((".75",".74",".74",".73",".74",".73",".74",".73",".74"),1):
        engine.process(quote(seq,ask=price),market)
    assert repository.entry_count(rule["id"],market.instance_id)==3
    assert len(repository.positions())==3
    assert repository.wallet()==Decimal("-11.304")
    assert engine.processed==9 and engine.commits==3


def test_quiet_market_fresh_return_rearms_without_replaying_gap(repository,market,rule):
    engine=cached(repository)
    now=datetime.now(timezone.utc)
    engine.clock=lambda:now
    engine.process(quote(at=now),market)
    now+=timedelta(seconds=6)
    engine.process(quote(2,at=now),market)
    assert len(repository.positions())==2


def test_rule_edit_serialized_and_old_context_fails_closed(repository,market,rule):
    owner=DecisionOwner(repository.database.config)
    generation=owner.dispatch("next_generation",((),{}))["result"]
    owner.dispatch("observe",payload(owner,market,rule,[quote(generation=generation,ask=".75")]))
    old_epoch=owner.gate_epoch
    updated=owner.dispatch("update_rule",((rule["id"],rule["spec"].model_copy(update={"entry_trigger":Decimal(".73")})),{}))["result"]
    rejected=owner.dispatch("observe",payload(owner,market,rule,[quote(2,generation=generation)],epoch=old_epoch))
    assert rejected["reason"]=="CONTROL_CONTEXT_CHANGED" and not repository.positions()
    result=owner.dispatch("observe",payload(owner,market,updated,[quote(2,generation=generation,ask=".73")]))
    assert not result["reason"] and repository.positions()[0]["rule_version_id"]==updated["current_version_id"]


def test_two_stream_generations_do_not_fence_each_other(repository,market,rule):
    second=replace(market,instance_id="second",up_token_id="u2",down_token_id="d2",external_id="e2",condition_id="c2",
        definition=next(d for d in market_definitions() if d.key=="ETH-5m"))
    repository.upsert_instance(second)
    second_rule=repository.create_rule(RuleSpec(name="second",markets=("ETH-5m",),entry_trigger=".74"))
    owner=DecisionOwner(repository.database.config)
    g1=owner.dispatch("next_generation",((),{}))["result"]
    g2=owner.dispatch("next_generation",((),{}))["result"]
    assert not owner.dispatch("observe",payload(owner,market,rule,[quote(generation=g1)],stream="1h"))["reason"]
    q=quote(instance_id=second.instance_id,token=second.up_token_id,generation=g2)
    assert not owner.dispatch("observe",payload(owner,second,second_rule,[q],stream="5m"))["reason"]
    assert len(repository.positions())==2


def test_transport_and_market_fences_recheck_after_database_lock(repository,market,rule,monkeypatch):
    context=multiprocessing.get_context("spawn")
    streams=context.Array("q",4)
    markets=context.Array("q",2)
    owner=DecisionOwner(repository.database.config,streams,markets)
    generation=owner.dispatch("next_generation",((),{}))["result"]
    streams[0]=generation;markets[0]=1
    original=owner.repository.database.transaction
    @contextmanager
    def disconnected_while_waiting():
        with original() as conn:
            streams[0]=0
            yield conn
    monkeypatch.setattr(owner.repository.database,"transaction",disconnected_while_waiting)
    result=owner.dispatch("observe",payload(owner,market,rule,[quote(generation=generation)]))
    assert result["reason"]=="STREAM_FENCED" and not repository.positions()
    monkeypatch.setattr(owner.repository.database,"transaction",original)
    generation=owner.dispatch("next_generation",((),{}))["result"]
    streams[0]=generation
    @contextmanager
    def market_lost_while_waiting():
        with original() as conn:
            markets[0]=2
            yield conn
    monkeypatch.setattr(owner.repository.database,"transaction",market_lost_while_waiting)
    data=list(payload(owner,market,rule,[quote(generation=generation)]));data[-3]={market.instance_id:(0,1)}
    result=owner.dispatch("observe",tuple(data))
    assert not result["reason"] and not repository.positions()  # Local fault does not retire healthy peers.


def test_expired_pair_gate_after_lock_cannot_enter(repository,market,rule,monkeypatch):
    owner=DecisionOwner(repository.database.config)
    generation=owner.dispatch("next_generation",((),{}))["result"]
    original=owner.repository.database.transaction
    @contextmanager
    def expired():
        with original() as conn:
            owner.gate_until[market.instance_id]=datetime.now(timezone.utc)-timedelta(seconds=1)
            yield conn
    monkeypatch.setattr(owner.repository.database,"transaction",expired)
    result=owner.dispatch("observe",payload(owner,market,rule,[quote(generation=generation)]))
    assert not result["reason"] and not repository.positions()


def _crash_financial(config, market, rule, when):
    owner=DecisionOwner(config)
    generation=owner.dispatch("next_generation",((),{}))["result"]
    if when=="before_commit":
        owner.repository._ledger=lambda *args,**kwargs:os._exit(41)
    else:
        original=owner.repository.database.transaction
        @contextmanager
        def after_commit():
            with original() as conn:
                yield conn
            os._exit(42)
        owner.repository.database.transaction=after_commit
    owner.dispatch("observe",payload(owner,market,rule,[quote(generation=generation)]))


@pytest.mark.parametrize("when,count",[("before_commit",0),("after_commit",1)])
def test_process_crash_preserves_atomic_ledger_and_quota(repository,market,rule,when,count):
    context=multiprocessing.get_context("spawn")
    child=context.Process(target=_crash_financial,args=(repository.database.config,market,rule,when))
    child.start();child.join(10)
    assert child.exitcode in (41,42)
    assert len(repository.positions())==repository.entry_count(rule["id"],market.instance_id)==count
    with repository.database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM demo_v2_ledger").fetchone()[0]==count
        assert conn.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    assert repository.wallet()==Decimal("-3.768")*count
    # Recovery loads durable quota but never replays the uncertain observation.
    owner=DecisionOwner(repository.database.config)
    assert owner.projection()["counts"].get((rule["id"],market.instance_id),0)==count


def test_slow_database_does_not_block_parent_loop_and_timeout_is_not_replayed(repository,market,rule):
    async def run():
        worker=DecisionWorker(repository.database.config,timeout=2)
        await worker.start()
        try:
            generation=await worker.command("next_generation")
            worker.timeout=.2
            worker.fence_stream("default",generation,True)
            # A separate temp-DB writer models a stalled filesystem/writer.
            req=payload(DecisionOwner(repository.database.config),market,rule,[quote(generation=generation)])
            with repository.database.connect() as lock:
                lock.execute("BEGIN IMMEDIATE")
                pending=asyncio.create_task(worker.call("observe",req))
                ticks=0
                while not pending.done():
                    await asyncio.sleep(.01);ticks+=1
                with pytest.raises(DecisionWorkerUnavailable):
                    await pending
                assert ticks>=10 and not worker.alive
                lock.rollback()
            assert not repository.positions()
            worker.timeout=2
            await worker.start()
            assert not repository.positions()
        finally:
            await worker.close()
    asyncio.run(run())


def test_market_lease_reuse_fences_old_inflight_identity(repository):
    worker=DecisionWorker(repository.database.config)
    slot,old=worker.market_lease("old")
    worker.retain_markets(set())
    new_slot,new=worker.market_lease("new")
    assert slot==new_slot and new!=old and worker.market_fences[slot]!=old


def test_queue_delay_budget_is_independent_from_quote_freshness():
    stamp=datetime.now(timezone.utc);clock=[0]
    stream=DecisionStream(clock=lambda:stamp,max_queue_delay_seconds=1,monotonic=lambda:clock[0])
    stream.begin_generation(1);stream.publish(quote(at=stamp));clock[0]=1.1
    with pytest.raises(ObservationRejected,match="QUEUE_DELAY"):
        asyncio.run(stream.next_observation())
    assert stream.snapshot()["invalidations"]["DECISION_QUEUE_DELAY_EXCEEDED"]==1


def test_quiet_hype_does_not_disconnect_or_block_healthy_eth(repository,market,rule):
    market=replace(market,starts_at=NOW-timedelta(minutes=1),ends_at=NOW+timedelta(minutes=59))
    other=replace(market,instance_id="hype",definition=next(d for d in market_definitions() if d.key=="HYPE-1h"),
        up_token_id="hype-u",down_token_id="hype-d",condition_id="hype-c",external_id="hype-e")
    now=[NOW];mono=[0]
    stream=DecisionStream(clock=lambda:now[0]);generation=iter(range(1,10))
    data=PublicMarketWS(stream,TelemetryBuffer(),clock=lambda:now[0],monotonic=lambda:mono[0],generation_factory=lambda:next(generation))
    class WS:
        sent=[]
        async def send(self,raw): self.sent.append(raw)
    async def run():
        await data.set_subscriptions([market,other]);data.begin_generation()
        for instance in (market,other):
            for token in (instance.up_token_id,instance.down_token_id):
                data.process_message(snapshot(instance,token))
        now[0]+=timedelta(seconds=11);mono[0]=11
        for token in (market.up_token_id,market.down_token_id):
            data.process_message(snapshot(market,token,stamp=now[0]))
        ws=WS();await data._refresh_stale_markets(ws)
        assert data.token_ready(market.up_token_id) and not data.token_ready(other.up_token_id)
        assert data.generation==1 and not stream.reason and not ws.sent
    asyncio.run(run())
    worker=runtime(repository)
    worker.clock=lambda:now[0];worker.providers=replace(worker.providers,market_data=data)
    worker.database_ok=worker.engine_ok=True;worker.discovery_at=now[0];worker.current={market.definition.key:market.instance_id}
    worker.health.record(ComponentHealth("market:hype",HealthState.NOT_READY,"STALE",critical=False))
    assert worker.entry_gate(rule,market)
    worker.health.record(ComponentHealth("database",HealthState.NOT_READY,"FAILED"))
    assert not worker.entry_gate(rule,market)


def test_empty_verified_book_is_distinct_from_missing_snapshot(market):
    data,stream=manager_for(market)
    book=snapshot(market,market.up_token_id);book.update(bids=[],asks=[])
    data.process_message(book)
    assert data.health()["books"][market.up_token_id]["state"]=="READY_EMPTY"
    assert data.health()["books"][market.down_token_id]["state"]=="AWAITING_SNAPSHOT"
    assert data.latest[market.up_token_id].best_ask is None


def test_snapshot_timeout_only_retries_affected_pair(market):
    now=[0]
    stream=DecisionStream(clock=lambda:NOW)
    data=PublicMarketWS(stream,TelemetryBuffer(),clock=lambda:NOW,monotonic=lambda:now[0],generation_factory=lambda:1)
    class WS:
        sent=[]
        async def send(self,raw): self.sent.append(json.loads(raw))
    async def run():
        await data.set_subscriptions([market]);data.begin_generation();now[0]=11;ws=WS()
        await data._refresh_stale_markets(ws)
        assert [x["operation"] for x in ws.sent]==["unsubscribe","subscribe"]
        assert set(ws.sent[-1]["assets_ids"])=={market.up_token_id,market.down_token_id}
        assert data.generation==1 and not stream.reason
    asyncio.run(run())


def test_timeframe_shards_have_independent_streams_and_held_tokens(repository,market):
    pool=MarketDataPool(TelemetryBuffer(),generation_factory=lambda:1,config=repository.database.config,on_issue=lambda *args:None)
    other=replace(market,instance_id="five",definition=next(d for d in market_definitions() if d.key=="ETH-5m"),
        up_token_id="five-u",down_token_id="five-d")
    asyncio.run(pool.set_subscriptions([market,other]))
    pool.shards["1h"].begin_generation();pool.shards["5m"].begin_generation()
    pool.streams["5m"].invalidate("FAKE_LOSS")
    assert not pool.streams["1h"].reason
    assert set(pool.shards["1h"].token_map)=={market.up_token_id,market.down_token_id}
    assert set(pool.shards["5m"].token_map)=={other.up_token_id,other.down_token_id}


def test_child_receives_only_explicit_noncredential_settings(repository):
    worker=DecisionWorker(replace(repository.database.config,session_secret="fake-test-secret",login_password_hash="fake-hash"))
    assert set(worker.settings)=={"data_dir","observation_max_age_seconds","flat_fee_per_action"}
    assert "fake-test" not in repr(worker.settings)


def test_reference_diagnostics_do_not_relabel_old_source_price_fresh(market):
    from demo_v2.providers.underlying import BinanceResearchProvider,ReferencePoint
    now=datetime.now(timezone.utc)
    provider=BinanceResearchProvider(None,clock=lambda:now)
    provider.latest_points['ETHUSDT']=ReferencePoint(Decimal(100),now-timedelta(seconds=30),'BINANCE_PUBLIC','BINANCE_SPOT:ETHUSDT','SPOT_USDT_RESEARCH')
    provider._latest_fetched['ETHUSDT']=now
    report=provider.diagnostics(market,{'description':'Binance 1 hour'})
    assert report['fetch_age_seconds']==0 and report['source_age_seconds']==30
    assert report['source_observed_at']==now-timedelta(seconds=30)


def test_expired_ipc_deadline_never_trades_even_with_fresh_exchange_quote(repository,market,rule):
    owner=DecisionOwner(repository.database.config)
    generation=owner.dispatch('next_generation',((),{}))['result']
    req=list(payload(owner,market,rule,[quote(generation=generation)]));req[-1]=time.monotonic()-1
    result=owner.dispatch('observe',tuple(req))
    assert result['reason']=='DECISION_QUEUE_DELAY_EXCEEDED' and not repository.positions()


def test_queue_budget_rechecked_after_database_lock(repository,market,rule,monkeypatch):
    owner=DecisionOwner(repository.database.config)
    generation=owner.dispatch('next_generation',((),{}))['result']
    original=owner.repository.database.transaction
    @contextmanager
    def delayed():
        with original() as conn:
            owner.deadline=time.monotonic()-1
            yield conn
    monkeypatch.setattr(owner.repository.database,'transaction',delayed)
    req=list(payload(owner,market,rule,[quote(generation=generation)]));req[-1]=time.monotonic()+1
    result=owner.dispatch('observe',tuple(req))
    assert result['reason']=='DECISION_QUEUE_DELAY_EXCEEDED' and not repository.positions()


def test_eligibility_gap_without_quote_rearms_and_rule_edit_alone_does_not(repository,market,rule):
    owner=DecisionOwner(repository.database.config)
    generation=owner.dispatch('next_generation',((),{}))['result']
    req=list(payload(owner,market,rule,[quote(generation=generation)]));req[4]={(rule['id'],market.instance_id):(True,0)}
    assert len(owner.dispatch('observe',tuple(req))['effects'])==1
    edited=owner.dispatch('update_rule',((rule['id'],rule['spec'].model_copy(update={'name':'renamed'})),{}))['result']
    req=list(payload(owner,market,edited,[quote(2,generation=generation)]));req[4]={(rule['id'],market.instance_id):(True,0)}
    assert not owner.dispatch('observe',tuple(req))['effects']
    # A gate sampled unhealthy while quotes were absent advances only this
    # rule/instance eligibility epoch; unchanged trigger may enter on recovery.
    req=list(payload(owner,market,edited,[quote(3,generation=generation)]));req[4]={(rule['id'],market.instance_id):(True,1)}
    assert len(owner.dispatch('observe',tuple(req))['effects'])==1
    assert len(repository.positions())==2


def test_shared_fences_have_no_crash_sensitive_process_mutex(repository):
    worker=DecisionWorker(repository.database.config)
    assert not hasattr(worker.stream_fences,'get_lock')
    assert not hasattr(worker.market_fences,'get_lock')


def test_wire_burst_waits_for_consumer_without_losing_prices(market):
    async def run():
        stream=DecisionStream(capacity=8,clock=lambda:NOW)
        manager=PublicMarketWS(stream,TelemetryBuffer(1),generation_factory=lambda:1,clock=lambda:NOW)
        await manager.set_subscriptions([market]);manager.begin_generation()
        class WS:
            reads=0
            async def recv(self):
                if self.reads==64:
                    await asyncio.Future()
                self.reads+=1
                message=snapshot(market,market.up_token_id,ask='.74' if self.reads%2 else '.75')
                return json.dumps({**message,'hash':str(self.reads)})
        ws=WS();receiver=asyncio.create_task(manager._receive(ws))
        try:
            for _ in range(1000):
                if stream.capacity_waits:break
                if receiver.done():await receiver
                await asyncio.sleep(0)
            assert stream.capacity_waits and ws.reads==4 and stream.snapshot()['depth']==4
            # Model an owner temporarily occupied with another serialized request.
            await asyncio.sleep(.02)
            assert ws.reads==4 and not stream.reason
            seen=[await asyncio.wait_for(stream.next_observation(),1) for _ in range(64)]
            assert [o.sequence for o in seen]==list(range(1,65))
            assert [o.best_ask for o in seen]==[Decimal('.74' if n%2 else '.75') for n in range(1,65)]
            assert stream.published==stream.delivered==64 and not stream.rejected and not stream.invalidations
            assert len(manager.telemetry.drain())==1
        finally:
            receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
    asyncio.run(run())


def test_backpressure_keeps_existing_deadline_and_generation_fences():
    async def run():
        stream=DecisionStream(capacity=2,clock=lambda:NOW,max_queue_delay_seconds=.02)
        stream.begin_generation(1);stream.publish(quote(at=NOW))
        with pytest.raises(ObservationRejected,match='DECISION_QUEUE_DELAY_EXCEEDED'):
            await stream.wait_for_capacity()
        assert stream.reason=='DECISION_QUEUE_DELAY_EXCEEDED'
        stream.begin_generation(2);stream.publish(quote(at=NOW,generation=2))
        waiter=asyncio.create_task(stream.wait_for_capacity());await asyncio.sleep(0)
        stream.begin_generation(3)
        with pytest.raises(ObservationRejected,match='GENERATION_CHANGED'):
            await waiter
        assert stream._generation==3 and not stream.reason
        stream.publish(quote(at=NOW,generation=3))
        waiter=asyncio.create_task(stream.wait_for_capacity());await asyncio.sleep(0)
        stream.invalidate('DISCONNECTED')
        with pytest.raises(ObservationRejected,match='DISCONNECTED'):
            await waiter
    asyncio.run(run())


def test_shard_health_recovers_without_hiding_current_queue_or_book_faults(repository,market,rule):
    async def run():
        instance=replace(market,starts_at=NOW-timedelta(seconds=1),ends_at=NOW+timedelta(hours=1))
        pool=MarketDataPool(TelemetryBuffer(),generation_factory=lambda:1,config=repository.database.config,on_issue=lambda *args:None)
        worker=runtime(repository,Providers(market_data=pool));worker.clock=lambda:NOW
        worker.database_ok=worker.engine_ok=True;worker.discovery_at=NOW
        worker.instances={instance.instance_id:instance};worker.current={instance.definition.key:instance.instance_id}
        worker.owner.projection['rules']=[rule]
        await worker.demand_once()
        shard=pool.shards['1h'];shard.clock=lambda:NOW;shard.decisions._clock=lambda:NOW
        shard.begin_generation()
        for token in (instance.up_token_id,instance.down_token_id):shard.process_message(snapshot(instance,token))
        worker.issue('market_websocket:1h','OLD_OVERFLOW')
        await worker.demand_once()
        assert worker.health._components['market_websocket:1h'].state==HealthState.READY
        assert 'market_websocket:1h' not in worker._issues
        shard.decisions.invalidate('CURRENT_QUEUE_FAULT')
        await worker.demand_once()
        assert worker.health._components['market_websocket:1h'].reason=='CURRENT_QUEUE_FAULT'
        assert worker.health._components['market_websocket:5m'].state==HealthState.READY
        shard.begin_generation(2);await worker.demand_once()
        assert worker.health._components['market_websocket:1h'].state==HealthState.READY
        assert worker.health._components['market:'+instance.instance_id].state==HealthState.NOT_READY
    asyncio.run(run())


def test_retired_capacity_wait_timeout_cannot_poison_new_generation(monkeypatch):
    async def run():
        stream=DecisionStream(capacity=2,clock=lambda:NOW)
        stream.begin_generation(1);stream.publish(quote(at=NOW))
        async def expired_wait():
            stream.begin_generation(2)
            raise asyncio.TimeoutError()
        monkeypatch.setattr(stream._progress,'wait',expired_wait)
        with pytest.raises(ObservationRejected,match='GENERATION_CHANGED'):
            await stream.wait_for_capacity()
        assert stream._generation==2 and not stream.reason and not stream.invalidations
    asyncio.run(run())
