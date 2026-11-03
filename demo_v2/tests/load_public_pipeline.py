"""Synthetic public-book load through real parser/FIFOs/runtime/owner, temp DB only.

No connector/run() is started: these are fabricated public frames, not network I/O.
"""
import argparse
import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time

from demo_v2.config import DemoConfig
from demo_v2.domain.models import MarketInstance,market_definitions
from demo_v2.domain.trading import RuleSpec
from demo_v2.health import HealthRegistry
from demo_v2.persistence.database import DemoDatabase
from demo_v2.persistence.repository import DemoRepository
from demo_v2.providers.contracts import Providers
from demo_v2.providers.market_pool import MarketDataPool
from demo_v2.runtime import DemoRuntime
from demo_v2.streams import DecisionStream,TelemetryBuffer


async def load(seconds,rate,*,wire=False,burst=16):
    with tempfile.TemporaryDirectory(prefix='demo-public-pipeline-load-') as folder:
        config=DemoConfig(data_dir=Path(folder),recovery_stability_seconds=0)
        database=DemoDatabase(config);database.migrate();repo=DemoRepository(database)
        now=datetime.now(timezone.utc);instances=[]
        for definition in market_definitions():
            key=definition.key
            instance=MarketInstance(key,definition,key+':c',key+':up',key+':down',now,now+timedelta(seconds=definition.expected_duration),
                external_id=key,discovered_at=now,metadata_source='SYNTHETIC_LOAD',verified=True)
            repo.upsert_instance(instance);instances.append(instance)
        repo.create_rule(RuleSpec(name='SYNTHETIC_LOAD',markets=tuple(d.key for d in market_definitions()),
            entry_trigger='.74',entry_frequency=2,take_profit='.96',stop_loss='.66'))
        telemetry=TelemetryBuffer(1)
        pool=MarketDataPool(telemetry,config=config,generation_factory=lambda:runtime.command('next_generation'),
            on_issue=lambda key,code:None,on_market_invalid=lambda key:runtime.owner.invalidate_market(key))
        runtime=DemoRuntime(config,repo,HealthRegistry(),DecisionStream(),telemetry,Providers(market_data=pool))
        await runtime.start()
        receivers=[]
        class Wire:
            def __init__(self):self.queue=asyncio.Queue(1024)
            async def recv(self):return await self.queue.get()
        wires={key:Wire() for key in pool.shards}
        try:
            runtime.current={i.definition.key:i.instance_id for i in instances};runtime.discovery_at=now
            await pool.set_subscriptions(instances)
            for shard in pool.shards.values():
                shard.begin_generation(await runtime.command('next_generation'))
            if wire:
                receivers=[asyncio.create_task(shard._receive(wires[key])) for key,shard in pool.shards.items()]
            tokens=[(i,t) for i in instances for t in (i.up_token_id,i.down_token_id)]
            async def publish(index,ask='.75',bid='.73'):
                instance,token=tokens[index%len(tokens)];stamp=datetime.now(timezone.utc)
                shard=pool.shards[instance.definition.timeframe.value]
                message={'event_type':'book','asset_id':token,'market':instance.condition_id,'hash':str(index),
                    'timestamp':str(int(stamp.timestamp()*1000)),
                    'bids':[{'price':bid,'size':'100'}],'asks':[{'price':ask,'size':'100'}]}
                if wire:
                    await wires[instance.definition.timeframe.value].queue.put(json.dumps(message))
                else:
                    shard.process_message(message)
            for n in range(len(tokens)):
                await publish(n);await asyncio.sleep(0)
            started=time.monotonic();count=0
            while time.monotonic()-started<seconds:
                runtime.discovery_at=runtime.clock()  # fabricated discovery heartbeat
                for _ in range(burst):
                    count+=1
                    await publish(count,'.74' if count%17==0 else '.75','.97' if count%31==0 else '.73')
                for task in receivers:
                    if task.done():await task
                await asyncio.sleep(max(0,min(.02,started+count/rate-time.monotonic())))
            for _ in range(1000):
                snapshots=[s.snapshot() for s in pool.streams.values()]
                drained=all(runtime.owner.metrics.get(key,{}).get('processed',0)
                    +runtime.owner.metrics.get(key,{}).get('market_fenced_observations',0)>=stream.published
                    for key,stream in pool.streams.items())
                if all(s['depth']==0 for s in snapshots) and drained and all(w.queue.empty() for w in wires.values()):
                    break
                await asyncio.sleep(.005)
            for task in receivers:
                if task.done():await task
            assert all(w.queue.empty() for w in wires.values())
            assert drained,runtime.pipeline_health()
            assert all(not s.reason for s in pool.streams.values()),runtime.pipeline_health()
            assert runtime.engine_ok and runtime.owner.alive
            positions=repo.positions()
            with database.connect() as conn:
                assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert conn.execute('SELECT sum(count) FROM demo_v2_entry_counts').fetchone()[0]==len(positions)
            print(json.dumps({'passed':True,'seconds':round(time.monotonic()-started,2),'frames':count,
                'frames_per_second':round(count/(time.monotonic()-started)),
                'loop_lag_max_ms':round(runtime.loop_lag_max_seconds*1000,2),
                'through_wire_receiver':wire,'producer_burst':burst,
                'streams':{k:s.snapshot() for k,s in pool.streams.items()},'decision_metrics':runtime.owner.metrics,
                'positions':len(positions),'telemetry_retained':len(telemetry.drain())}),flush=True)
        finally:
            for task in receivers:task.cancel()
            await asyncio.gather(*receivers,return_exceptions=True)
            await runtime.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seconds',type=int,default=30);parser.add_argument('--rate',type=int,default=5000)
    parser.add_argument('--wire',action='store_true');parser.add_argument('--burst',type=int,default=16)
    args=parser.parse_args()
    if not 1<=args.seconds<=120 or not 1<=args.rate<=10000 or not 1<=args.burst<=1024:parser.error('bounded test duration/rate/burst required')
    asyncio.run(asyncio.wait_for(load(args.seconds,args.rate,wire=args.wire,burst=args.burst),timeout=args.seconds+15))


if __name__=='__main__':
    main()
