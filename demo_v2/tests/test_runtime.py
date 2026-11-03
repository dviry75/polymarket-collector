import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from demo_v2.config import DemoConfig
from demo_v2.health import HealthRegistry, StabilityGate
from demo_v2.providers.contracts import Providers
from demo_v2.runtime import DemoRuntime
from demo_v2.streams import DecisionStream, TelemetryBuffer
from conftest import quote
from test_fee_underlying import verified_fee, context


def runtime(repository, providers=None, **changes):
    return DemoRuntime(DemoConfig(data_dir=repository.database.config.data_dir,recovery_stability_seconds=0,**changes),
        repository,HealthRegistry(),DecisionStream(),TelemetryBuffer(),providers)


def test_stability_requires_continuous_health():
    time=[0]
    gate=StabilityGate(2,clock=lambda:time[0])
    assert not gate.ready("market",True)
    time[0]=2
    assert gate.ready("market",True)
    assert not gate.ready("market",False)
    time[0]=3
    assert not gate.ready("market",True)
    time[0]=5
    assert gate.ready("market",True)


def test_runtime_starts_offline_and_cleanly_stops_without_network(repository):
    worker=runtime(repository)
    async def run():
        await worker.start()
        await asyncio.sleep(0.01)
        assert worker.database_ok and worker.engine_ok
        assert any(c.reason=="PUBLIC_PROVIDERS_DISABLED" for c in worker.health.snapshot().components)
        await worker.close()
        assert all(task.done() for task in worker.tasks)
    asyncio.run(run())


def test_public_worker_fake_pipeline_and_recovery(repository,market,rule):
    class Discovery:
        metadata={}
        issues={}
        fail=False
        async def discover(self,definitions):
            if self.fail:
                raise ValueError("fake discovery interruption")
            return [market]
    class Fees:
        async def parameters(self,instance):
            return verified_fee(instance.instance_id)
    class Underlying:
        async def context(self,instance,metadata):
            return context(instance)
    class Resolution:
        async def resolve(self,instance):
            return None
    class Data:
        status="CONNECTED"
        _ws=None
        def __init__(self):
            self.wanted=()
            self.latest={}
            self.stop=asyncio.Event()
        def token_ready(self,token):
            return token in self.latest
        async def set_subscriptions(self,instances):
            self.wanted=instances
        async def run(self):
            await self.stop.wait()
        async def close(self):
            self.stop.set()
        async def resync(self,reason):
            self.latest.clear()
    discovery,data=Discovery(),Data()
    worker=runtime(repository,Providers(discovery=discovery,fees=Fees(),underlying=Underlying(),resolution=Resolution(),market_data=data))
    async def run():
        await worker.start()
        try:
            await worker.discover_once()
            await worker.references_once()
            await worker.reference_once(market.instance_id)
            await worker.demand_once()
            assert data.wanted==(market,)
            assert not worker.entry_gate(rule,market)  # Socket is not fresh books.
            data.latest={market.up_token_id:True,market.down_token_id:True}
            await worker.demand_once()
            assert worker.entry_gate(rule,market)
            generation=repository.next_generation()
            worker.decisions.begin_generation(generation)
            worker.decisions.publish(quote(generation=generation))
            for _ in range(50):
                if repository.positions():
                    break
                await asyncio.sleep(0.001)
            assert len(repository.positions())==1
            discovery.fail=True
            try:
                await worker.discover_once()
            except ValueError:
                pass
            assert not worker.entry_gate(rule,market)
            discovery.fail=False
            await worker.discover_once()
            assert worker.entry_gate(rule,market)
            await worker.reset()
            assert repository.positions()==[] and len(repository.rules())==1
            assert not data.wanted and not data.latest
        finally:
            await worker.close()
    asyncio.run(run())


def test_stale_discovery_and_database_gating(repository,market,rule):
    worker=runtime(repository)
    worker.database_ok=worker.engine_ok=True
    worker.current={market.definition.key:market.instance_id}
    worker.discovery_at=datetime.now(timezone.utc)
    assert worker.dependencies(rule,market)
    worker.discovery_at-=timedelta(seconds=31)
    assert not worker.dependencies(rule,market)
    worker.discovery_at=datetime.now(timezone.utc)
    worker.database_ok=False
    assert not worker.dependencies(rule,market)


def test_worker_error_does_not_kill_retry_loop(repository):
    worker=runtime(repository)
    calls=[]
    async def operation():
        calls.append(1)
        if len(calls)==1:
            raise ValueError("fake transient")
        worker._stop.set()
    asyncio.run(worker._worker("database",operation,0.001))
    assert len(calls)==2
    with repository.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_incidents").fetchone()[0]==1


def test_gate_epoch_records_unobserved_local_dependency_gap(repository,market,rule):
    class Data:
        ready=True
        def token_ready(self,token):return self.ready
    data=Data();worker=runtime(repository,Providers(market_data=data))
    worker.database_ok=worker.engine_ok=True
    worker.current={market.definition.key:market.instance_id};worker.discovery_at=worker.clock()
    assert worker.entry_gate(rule,market)
    data.ready=False;assert not worker.entry_gate(rule,market)
    data.ready=True;assert worker.entry_gate(rule,market)
    assert worker._gate_epochs[(rule['id'],market.instance_id)]==1


def test_retired_waiter_cannot_poison_first_snapshot_of_new_generation(repository,market,rule):
    class Data:
        status='CONNECTED'
        latest={}
        def token_ready(self,token):return token in self.latest
        async def resync(self,reason):self.latest.clear()
        async def close(self):pass
    data=Data();worker=runtime(repository,Providers(market_data=data))
    async def run():
        await worker.start()
        try:
            worker.current={market.definition.key:market.instance_id};worker.discovery_at=worker.clock()
            for index,ask in enumerate(('0.75','0.74'),1):
                generation=await worker.command('next_generation')
                data.latest={market.up_token_id:True,market.down_token_id:True}
                worker.decisions.begin_generation(generation)
                worker.decisions.publish(quote(generation=generation,ask=ask))
                for _ in range(200):
                    if worker.engine.generation==generation:break
                    await asyncio.sleep(.002)
                assert worker.engine.generation==generation and not worker.decisions.reason
                await asyncio.sleep(.01)  # consumer now waits on the old queue
            assert len(repository.positions())==1
        finally:
            await worker.close()
    asyncio.run(run())


def test_dead_owner_restarts_without_replay_and_retains_financial_state(repository,market,rule):
    class Data:
        status='CONNECTED'
        latest={}
        resyncs=[]
        def token_ready(self,token):return token in self.latest
        async def resync(self,reason):self.latest.clear();self.resyncs.append(reason)
        async def close(self):pass
    data=Data();worker=runtime(repository,Providers(market_data=data))
    async def publish(ask):
        generation=await worker.command('next_generation')
        data.latest={market.up_token_id:True,market.down_token_id:True}
        worker.decisions.begin_generation(generation);worker.decisions.publish(quote(generation=generation,ask=ask))
        for _ in range(300):
            if worker.engine.generation==generation:break
            await asyncio.sleep(.002)
        assert worker.engine.generation==generation
    async def run():
        await worker.start()
        try:
            # Exercise supervision deterministically instead of racing the
            # periodic heartbeat with explicit fault injection in this test.
            for task in worker.tasks:
                if task.get_name()=='demo-database':
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
            worker.current={market.definition.key:market.instance_id};worker.discovery_at=worker.clock()
            await publish('.74')
            assert len(repository.positions())==1
            old_pid=worker.owner.projection['pid']
            worker.owner.process.kill();await asyncio.to_thread(worker.owner.process.join,2)
            await asyncio.sleep(.12)
            assert not worker.entry_gate(rule,market)
            await worker.database_once()
            assert worker.owner.alive and worker.owner.projection['pid']!=old_pid
            assert data.resyncs[-1]=='DECISION_WORKER_RESTARTED'
            assert len(repository.positions())==repository.entry_count(rule['id'],market.instance_id)==1
            await publish('.75')
            assert len(repository.positions())==1  # no replay of uncertain/old inputs
            await publish('.74')
            assert len(repository.positions())==repository.entry_count(rule['id'],market.instance_id)==2
        finally:
            await worker.close()
    asyncio.run(run())
