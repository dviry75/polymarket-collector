"""Explicit Phase 9 read-only verifier, never collected or run by pytest.

Run from repo root with PYTHONPATH=.:polymarket-collector and --output /tmp/...
No account client, authentication, trading write or production state is used.
"""

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from contextlib import asynccontextmanager,suppress

import httpx

from demo_v2.domain.models import market_definitions
from demo_v2.persistence.repository import dump
from demo_v2.providers.discovery import GammaDiscovery, GammaResolution, candidate_family, parse_instance
from demo_v2.providers.fees import GammaFeeProvider
from demo_v2.providers.public_http import PublicHTTP
from demo_v2.providers.underlying import BinanceResearchProvider, source_mapping


def stamp():
    return datetime.now(timezone.utc)


async def verify(output):
    gamma=PublicHTTP("https://gamma-api.polymarket.com")
    binance=PublicHTTP("https://api.binance.com")
    report={"observed_at":stamp(),"mode":"PUBLIC_READ_ONLY","markets":[],"underlying":[],"clob":[]}
    try:
        discovery=GammaDiscovery(gamma)
        found=await discovery.discover(market_definitions())
        now=stamp()
        for definition in market_definitions():
            instances=sorted((i for i in found if i.definition.key==definition.key),key=lambda i:i.starts_at)
            current=[i for i in instances if i.starts_at<=now<i.ends_at and i.status=="OPEN"]
            next_instances=[i for i in instances if i.starts_at>now]
            item={"family":definition.key,"candidate_series":candidate_family(definition),
                "status":"VERIFIED" if len(current)==1 else "TEMPORARILY_UNAVAILABLE",
                "issue":discovery.issues.get(definition.key),"current":[asdict(i) for i in current],
                "next":[asdict(i) for i in next_instances],"next_contiguous":bool(current and next_instances and current[0].ends_at==next_instances[0].starts_at)}
            if current:
                instance=current[0]
                metadata=discovery.metadata[instance.instance_id]
                item["market_metadata"]={key:metadata.get(key) for key in (
                    "id","question","slug","conditionId","version","clobTokenIds","positionIds","outcomes","outcomePrices",
                    "eventStartTime","endDate","active","closed","acceptingOrders","enableOrderBook",
                    "feesEnabled","feeSchedule","feeType","umaResolutionStatus","description","resolutionSource")}
                item["source_mapping"]=source_mapping(instance,metadata)
            report["markets"].append(item)
        print("discovery complete:",sum(row["status"]=="VERIFIED" for row in report["markets"]),"/21",flush=True)
        semaphore=asyncio.Semaphore(3)
        async def resolution_and_fee(item):
            if not item["current"]:
                return
            definition=next(d for d in market_definitions() if d.key==item["family"])
            instance=next(i for i in found if i.instance_id==item["current"][0]["instance_id"])
            try:
                async with semaphore:
                    evidence=await GammaFeeProvider(gamma).parameters(instance)
                item["fee_evidence"]=evidence.evidence()
            except Exception as exc:
                item["fee_error"]=type(exc).__name__
            try:
                async with semaphore:
                    events=await gamma.get("/events",params={"series_id":instance.definition.family_id,
                        "closed":"true","limit":2,"order":"endDate","ascending":"false",
                        "end_date_max":(now-timedelta(hours=3)).isoformat()})
                resolved=[]
                for event in events:
                    try:
                        old,metadata=parse_instance(event,definition,instance.definition.family_id,now)
                        async with semaphore:
                            evidence=await GammaResolution(gamma).resolve(old)
                        resolved.append({"instance":asdict(old),"resolution":asdict(evidence) if evidence else None,
                            "uma_status":metadata.get("umaResolutionStatus"),"closed":metadata.get("closed"),"outcome_prices":metadata.get("outcomePrices")})
                        if evidence:
                            break
                    except (ValueError,TypeError,KeyError):
                        continue
                item["historical_resolution"]=resolved
            except Exception as exc:
                item["resolution_error"]=type(exc).__name__
        await asyncio.gather(*(resolution_and_fee(item) for item in report["markets"]))
        print("fee/resolution evidence complete",flush=True)
        provider=BinanceResearchProvider(binance)
        for asset in dict.fromkeys(d.asset for d in market_definitions()):
            candidates=[i for i in found if i.definition.asset==asset and i.starts_at<=stamp()<i.ends_at]
            row={"asset":asset.value,"provider":"BINANCE_PUBLIC","pair":asset.value+"USDT","research_proxy":True,"contexts":[]}
            for instance in candidates:
                try:
                    sample=await provider.context(instance,discovery.metadata[instance.instance_id])
                    row["contexts"].append({"family":instance.definition.key,"mapping":source_mapping(instance,discovery.metadata[instance.instance_id]),
                        "available":sample is not None,"fresh":bool(sample and sample.valid(instance,stamp())),"evidence":sample.evidence() if sample else None})
                except Exception as exc:
                    row["contexts"].append({"family":instance.definition.key,"error":type(exc).__name__,"detail":str(exc)[:200]})
            row["status"]="VERIFIED" if row["contexts"] and all(c.get("fresh") for c in row["contexts"]) else "BLOCKED"
            report["underlying"].append(row)
            print("underlying",asset.value,row["status"],flush=True)
        representative=next((i for i in found if i.definition.key=="BTC-5m" and i.starts_at<=stamp()<i.ends_at),None)
        if representative:
            async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
                for path,params in [("/markets/"+representative.condition_id,None),("/book",{"token_id":representative.up_token_id}),
                                    ("/fee-rate",{"token_id":representative.up_token_id}),("/clob-markets/"+representative.condition_id,None)]:
                    try:
                        response=await client.get("https://clob.polymarket.com"+path,params=params)
                        report["clob"].append({"path":path,"params":params,"status":response.status_code,
                            "content_type":response.headers.get("content-type"),"server":response.headers.get("server"),
                            "cf_ray":response.headers.get("cf-ray"),"body_excerpt":response.text[:500]})
                    except Exception as exc:
                        report["clob"].append({"path":path,"error":type(exc).__name__})
        # Missing SOL-hourly convention is investigated without inventing identity.
        missing=[row for row in report["markets"] if not row["current"]]
        if missing:
            async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
                for row in missing:
                    asset=row["family"].split("-")[0]
                    query={"q":("Solana" if asset=="SOL" else asset)+" Up or Down","events_status":"active","limit_per_type":8}
                    try:
                        response=await client.get("https://gamma-api.polymarket.com/public-search",params=query)
                        response.raise_for_status()
                        data=response.json(parse_float=Decimal)
                        row["search_investigation"]=[{"slug":event.get("slug"),"title":event.get("title"),"series":event.get("series"),
                            "markets":[{key:market.get(key) for key in ("id","slug","conditionId","eventStartTime","endDate")} for market in event.get("markets",[])]}
                            for event in data.get("events",[])]
                    except Exception as exc:
                        row["search_error"]=type(exc).__name__
    finally:
        await gamma.close()
        await binance.close()
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_text(dump(report)+"\n")
        print("public evidence saved",output,flush=True)
    return report


async def verify_decision_flow(output):
    from websockets.asyncio.client import connect
    from demo_v2.config import DemoConfig
    from demo_v2.domain.trading import RuleSpec
    from demo_v2.engine import DemoEngine
    from demo_v2.health import ComponentHealth,HealthRegistry,HealthState
    from demo_v2.persistence.database import DemoDatabase
    from demo_v2.persistence.repository import DemoRepository
    from demo_v2.providers.market_ws import PublicMarketWS
    from demo_v2.streams import DecisionStream,TelemetryBuffer,ObservationRejected
    report={"observed_at":stamp(),"mode":"PUBLIC_OBSERVATIONS_NO_TRADING","sent":[],"observations":[],"health":[]}
    gamma=PublicHTTP("https://gamma-api.polymarket.com")
    manager=task=None
    try:
        definitions=[d for d in market_definitions() if d.key=="BTC-15m"]
        discovery=GammaDiscovery(gamma)
        found=await discovery.discover(definitions)
        instance=next(i for i in found if i.starts_at<=stamp()<i.ends_at)
        fee=await GammaFeeProvider(gamma).parameters(instance)
        report["instance"]=asdict(instance)
        report["fee"]=fee.evidence()
        with tempfile.TemporaryDirectory(prefix="demo-v2-phase9-") as folder:
            database=DemoDatabase(DemoConfig(data_dir=Path(folder)))
            database.migrate()
            repository=DemoRepository(database)
            repository.upsert_instance(instance,discovery.metadata[instance.instance_id])
            rule=repository.create_rule(RuleSpec(name="PHASE9_PUBLIC_VERIFICATION_NO_FILL",markets=(instance.definition.key,),entry_trigger="0.5"))
            stream=DecisionStream()
            engine=DemoEngine(repository)
            health=HealthRegistry()
            def health_sample():
                ready=all(manager.token_ready(t) for t in (instance.up_token_id,instance.down_token_id))
                health.record(ComponentHealth("market_data",HealthState.READY if ready else HealthState.NOT_READY,"" if ready else "NO_FRESH_DATA"))
                report["health"].append(health.snapshot().state.value)
            class Socket:
                def __init__(self,ws): self.ws=ws
                async def send(self,raw):
                    if raw not in ("PING","PONG"):
                        report["sent"].append(json.loads(raw))
                    await self.ws.send(raw)
                async def recv(self): return await self.ws.recv()
                async def close(self): await self.ws.close()
            @asynccontextmanager
            async def connection(url,**kwargs):
                async with connect(url,proxy=None,open_timeout=8,**kwargs) as ws:
                    yield Socket(ws)
            manager=PublicMarketWS(stream,TelemetryBuffer(1),generation_factory=repository.next_generation,connector=connection)
            health_sample()
            await manager.set_subscriptions([instance])
            task=asyncio.create_task(manager.run())
            async def receive(after_generation=0):
                deadline=asyncio.get_running_loop().time()+20
                while asyncio.get_running_loop().time()<deadline:
                    try:
                        observation=await asyncio.wait_for(stream.next_observation(),1)
                    except (ObservationRejected,asyncio.TimeoutError):
                        await asyncio.sleep(0.02)
                        continue
                    if observation.generation<=after_generation:
                        continue
                    if engine.generation!=observation.generation:
                        engine.begin_generation(observation.generation)
                    engine.process(observation,instance,fee)
                    report["observations"].append(asdict(observation))
                    if all(manager.token_ready(t) for t in (instance.up_token_id,instance.down_token_id)):
                        return observation
                raise TimeoutError("no fresh actionable public observations within 20s")
            first=await receive()
            health_sample()
            with database.connect() as conn:
                report["persisted_rule_runtime_rows"]=conn.execute("SELECT COUNT(*) FROM demo_v2_entry_runtime WHERE rule_id=?",(rule["id"],)).fetchone()[0]
            assert report["persisted_rule_runtime_rows"]>0
            await manager.resync("PHASE9_EXPLICIT_RECONNECT")
            health_sample()
            second=await receive(after_generation=first.generation)
            health_sample()
            report["generation_advanced"]=second.generation>first.generation
            report["no_old_generation_replay"]=all(o["generation"]>=first.generation for o in report["observations"])
            report["positions"]=len(repository.positions())
            report["expected_entry_blocker"]=fee.reason
            assert report["positions"]==0  # Production fee contract is still UNSUPPORTED.
            await manager.set_subscriptions(())
            report["unsubscribed"]=not manager.token_map and any(m.get("operation")=="unsubscribe" for m in report["sent"])
            report["result"]="PASS"
    except Exception as exc:
        report["result"]="BLOCKED"
        report["error"]={"class":type(exc).__name__,"detail":str(exc)[:200]}
    finally:
        if manager:
            with suppress(Exception): await manager.close()
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        await gamma.close()
        Path(output).write_text(dump(report)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k not in ("instance","observations","sent","fee")},default=str),flush=True)
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    parser.add_argument("--mode",choices=("matrix","e2e"),default="matrix")
    args=parser.parse_args()
    asyncio.run(verify_decision_flow(args.output) if args.mode=="e2e" else verify(args.output))
