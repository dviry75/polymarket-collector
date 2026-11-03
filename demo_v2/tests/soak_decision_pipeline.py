"""Explicit isolated load/soak test. Temp DEMO state, synthetic feed, no network.

Run as a module with --seconds 3600 for an hour boundary. Never reads config
from environment, real DBs or credentials. Results are aggregate counters only.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import tempfile
import time

from demo_v2.config import DemoConfig
from demo_v2.decision_worker import DecisionWorker
from demo_v2.domain.models import MarketInstance,MarketObservation,OfficialResolution,market_definitions
from demo_v2.domain.trading import RuleSpec
from demo_v2.persistence.database import DemoDatabase
from demo_v2.persistence.repository import DemoRepository


async def soak(seconds, rate):
    with tempfile.TemporaryDirectory(prefix="demo-isolated-pipeline-soak-") as folder:
        config=DemoConfig(data_dir=Path(folder))
        database=DemoDatabase(config);database.migrate();repo=DemoRepository(database)
        worker=DecisionWorker(config)
        await worker.start()
        started=time.monotonic();sequence=0;rollovers=0;max_rpc=0;max_lag=0;reported=-1
        instances={};serial={};gates={};rule=None
        rule=await worker.command("create_rule",RuleSpec(name="SYNTHETIC_SOAK",markets=tuple(d.key for d in market_definitions()),
            entry_trigger=".74",entry_frequency=2,take_profit=".96",stop_loss=".66"))
        generation=await worker.command("next_generation");worker.fence_stream("default",generation,True)
        try:
            while time.monotonic()-started<seconds:
                now=datetime.now(timezone.utc)
                changed=False
                for definition in market_definitions():
                    old=instances.get(definition.key)
                    if old is not None and old.ends_at>now:
                        continue
                    if old is not None:
                        await worker.command("resolve",OfficialResolution(old.instance_id,old.up_token_id,now,"SYNTHETIC_SOAK_OFFICIAL"))
                        rollovers+=1
                    serial[definition.key]=serial.get(definition.key,0)+1
                    key=definition.key+":"+str(serial[definition.key])
                    # Short runs accelerate the entire hour lifecycle; the full
                    # 3600s run uses actual market durations and crosses all.
                    duration=definition.timeframe.seconds if seconds>=3600 else max(.1,definition.timeframe.seconds*seconds/4000)
                    instance=MarketInstance(key,definition,key+":condition",key+":up",key+":down",now,now+timedelta(seconds=duration),
                        external_id=key,discovered_at=now,metadata_source="SYNTHETIC_SOAK",verified=True)
                    await worker.command("upsert_instance",instance)
                    instances[definition.key]=instance;changed=True
                if changed:
                    worker.retain_markets({i.instance_id for i in instances.values()})
                current={i.instance_id:i for i in instances.values()}
                until={i:now+timedelta(seconds=5) for i in current}
                gates={(rule["id"],i):True for i in current}
                samples=[];values=list(instances.values())
                for _ in range(64):
                    sequence+=1;instance=values[sequence%len(values)]
                    ask=Decimal(".74") if sequence%17==0 else Decimal(".75")
                    bid=Decimal(".97") if sequence%31==0 else Decimal(".73")
                    samples.append(MarketObservation(instance.instance_id,instance.up_token_id if sequence%2 else instance.down_token_id,
                        generation,sequence,now,now,"SYNTHETIC_SOAK",bid,ask))
                begin=time.monotonic()
                response=await worker.call("observe",("default",samples,current,{},gates,until,worker.epoch,
                    {i:worker.market_lease(i) for i in current},{},time.monotonic()+1))
                assert not response["reason"],response["reason"]
                max_rpc=max(max_rpc,time.monotonic()-begin)
                scheduled=started+sequence/rate
                if scheduled>time.monotonic():
                    before=time.monotonic();await asyncio.sleep(min(scheduled-before,.05))
                    max_lag=max(max_lag,max(0,time.monotonic()-min(scheduled,before+.05)))
                minute=int((time.monotonic()-started)//60)
                if minute!=reported:
                    reported=minute
                    print(json.dumps({"elapsed_seconds":round(time.monotonic()-started,1),"processed":sequence,"rollovers":rollovers,
                        "max_rpc_ms":round(max_rpc*1000,2),"max_timer_lag_ms":round(max_lag*1000,2)}),flush=True)
            positions=repo.positions()
            with database.connect() as conn:
                assert conn.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                count=conn.execute("SELECT sum(count) FROM demo_v2_entry_counts").fetchone()[0]
                assert count==len(positions)
                ledger=list(conn.execute("SELECT * FROM demo_v2_ledger"))
                assert len(ledger)==len(positions)+sum(p["status"]=="CLOSED" for p in positions)
                assert len({p["idempotency_key"] for p in positions})==len(positions)
                assert sum((Decimal(row["amount_text"]) for row in ledger),Decimal(0))==repo.wallet()
            result={"passed":True,"seconds":round(time.monotonic()-started,2),"processed":sequence,"rollovers":rollovers,
                "positions":len(positions),"ledger_rows":len(ledger),"observations_per_second":round(sequence/(time.monotonic()-started)),
                "max_rpc_ms":round(max_rpc*1000,2),"max_timer_lag_ms":round(max_lag*1000,2)}
            print(json.dumps(result),flush=True)
            return result
        finally:
            await worker.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--seconds",type=float,default=60);parser.add_argument("--rate",type=int,default=5000)
    args=parser.parse_args()
    if not math.isfinite(args.seconds) or not 1<=args.seconds<=7200 or not 1<=args.rate<=20000:
        parser.error("bounded duration/rate required")
    asyncio.run(soak(args.seconds,args.rate))


if __name__=="__main__":
    main()
