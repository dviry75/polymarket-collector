"""DEMO-only public-data workers and single ordered decision consumer."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone, timedelta
import time
import json

from .domain.eligibility import entry_eligible
from .domain.models import market_definitions
from .engine import DemoEngine
from .decision_worker import DecisionWorker
from .providers.market_pool import MarketDataPool
from .health import ComponentHealth, HealthState, StabilityGate
from .persistence.repository import instance_model
from .providers.contracts import Providers
from .providers.discovery import GammaDiscovery, GammaResolution
from .providers.market_ws import PublicMarketWS
from .providers.public_http import PublicHTTP
from .providers.subscriptions import current_instances, subscription_demand
from .providers.underlying import BinanceResearchProvider
from .streams import ObservationRejected


class DemoRuntime:
    def __init__(self, config, repository, health, decisions, telemetry, providers=None,
                 *, clock=lambda:datetime.now(timezone.utc)):
        self.config, self.repository, self.health, self.decisions = config, repository, health, decisions
        self.clock, self.telemetry = clock, telemetry
        self.providers = providers or Providers()
        self.clients, self.tasks = [], []
        self.reference_tasks={}
        self.instances, self.metadata, self.current, self.references = {}, {}, {}, {}
        self.reference_diagnostics={}
        self.discovery_at = None
        self.database_ok = self.engine_ok = False
        self.stability = StabilityGate(config.recovery_stability_seconds)
        self.engine = DemoEngine(repository,clock=clock,max_age=config.observation_max_age_seconds,entry_gate=self.entry_gate,
            flat_fee_per_action=config.flat_fee_per_action)
        self._stop = asyncio.Event()
        self._issues = {}
        self._incident_tasks=set()
        self.owner=DecisionWorker(config)
        self.streams={"default":decisions}
        self.loop_lag_seconds=0.0
        self.loop_lag_max_seconds=0.0
        self._projection_at=0.0
        self._generation_seen={}
        self._gate_epochs={}
        self._gate_health={}
        self._resync_requested={}
        self._restart_lock=asyncio.Lock()

    def record(self, component, healthy, reason="", *, critical=True):
        self.health.record(ComponentHealth(component,HealthState.READY if healthy else HealthState.NOT_READY,reason,critical))

    def issue(self, component, code):
        critical=component in {"database","engine_worker"}
        self.record(component,False,code,critical=critical)
        if self._issues.get(component) != code:
            # Incident persistence is cold-path work; never block a socket.
            if self.owner.alive:
                task=asyncio.create_task(self._incident(component,code),name="demo-incident")
                self._incident_tasks.add(task)
                task.add_done_callback(self._incident_tasks.discard)
        self._issues[component] = code

    async def _incident(self, component, code):
        with suppress(Exception):
            await self.command("incident",component,code)

    @property
    def projection(self):
        return self.owner.projection

    async def command(self, name, *args, **kwargs):
        try:
            result=await self.owner.command(name,*args,**kwargs)
            self._projection_at=time.monotonic()
            return result
        except RuntimeError:
            self.engine_ok=False
            self.record("engine_worker",False,"DECISION_WORKER_UNAVAILABLE")
            raise

    def discovery_fresh(self, instance):
        return bool(self.discovery_at and 0 <= (self.clock()-self.discovery_at).total_seconds() <= 30
                    and self.current.get(instance.definition.key) == instance.instance_id)

    def dependencies(self, rule, instance):
        reference = self.references.get(instance.instance_id)
        spec = rule.get("spec")
        if spec is None:
            from .domain.trading import RuleSpec
            spec = RuleSpec.model_validate_json(rule["snapshot_json"])
        return bool(self.database_ok and self.engine_ok and self.discovery_fresh(instance)
            and (not spec.underlying_filters or (reference and reference.valid(instance,self.clock(),self.config.observation_max_age_seconds))))

    def entry_gate(self, rule, instance):
        data=self.providers.market_data
        tokens=(instance.up_token_id,instance.down_token_id)
        spec=rule.get("spec")
        if spec is None:
            from .domain.trading import RuleSpec
            spec=RuleSpec.model_validate_json(rule["snapshot_json"])
        healthy=(self.dependencies(rule,instance) and entry_eligible(spec,instance,self.clock()) and data is not None
            and all(data.token_ready(token) for token in tokens)
            and all(c.state==HealthState.READY for c in self.health.snapshot().components if c.critical)
            and (not self.owner.alive or time.monotonic()-self._projection_at<=5))
        key=(rule["id"],instance.instance_id)
        if self._gate_health.get(key,False) and not healthy:
            self._gate_epochs[key]=self._gate_epochs.get(key,0)+1
        self._gate_health[key]=healthy
        return self.stability.ready(key,healthy)

    async def discover_once(self):
        provider = self.providers.discovery
        if provider is None:
            raise RuntimeError("DISCOVERY_NOT_CONFIGURED")
        try:
            found = list(await provider.discover(market_definitions()))
            now = self.clock()
            current = current_instances(found,now)
            metadata = getattr(provider,"metadata",{})
            for instance in found:
                await self.command("upsert_instance",instance,metadata.get(instance.instance_id,{}))
                self.instances[instance.instance_id] = instance
                self.metadata[instance.instance_id] = metadata.get(instance.instance_id,{})
            self.current = {key:value.instance_id for key,value in current.items()}
            self.discovery_at = now
            issues = getattr(provider,"issues",{})
            self.record("discovery",len(current)==21,"" if len(current)==21 else "FAMILIES_UNAVAILABLE",critical=False)
            for definition in market_definitions():
                self.record("discovery:"+definition.key,definition.key in current,issues.get(definition.key,""),critical=False)
        except Exception:
            self.current = {}
            self.discovery_at = None
            raise

    async def references_once(self):
        if self.providers.underlying is None:
            raise RuntimeError("UNDERLYING_NOT_CONFIGURED")
        # Research snapshots are best effort for every active/held instance;
        # only a rule explicitly requiring them is gated by their availability.
        wanted = set(self.current.values()) | {p["instance_id"] for p in self.projection["positions"]}
        for instance_id,task in tuple(self.reference_tasks.items()):
            if instance_id not in wanted or task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                self.reference_tasks.pop(instance_id)
                self.references.pop(instance_id,None)
                self.health.forget("underlying:"+instance_id)
                self.reference_diagnostics.pop(instance_id,None)
        for instance_id in wanted-self.reference_tasks.keys():
            self.reference_tasks[instance_id]=asyncio.create_task(self._worker("underlying:"+instance_id,
                lambda key=instance_id:self.reference_once(key),1),name="demo-reference-"+instance_id)
        ready=bool(wanted) and all(i in self.references and self.references[i].valid(
            self.instances[i],self.clock(),self.config.observation_max_age_seconds) for i in wanted)
        self.record("underlying_provider",ready,"" if ready else "REFERENCE_PARTIAL_OR_UNAVAILABLE",critical=False)

    async def reference_once(self, instance_id):
        try:
            sample=await self.providers.underlying.context(self.instances[instance_id],self.metadata.get(instance_id,{}))
            valid=sample is not None and sample.valid(self.instances[instance_id],self.clock(),self.config.observation_max_age_seconds)
            if valid:
                self.references[instance_id]=sample
            else:
                self.references.pop(instance_id,None)
            reason="" if valid else ("REFERENCE_SOURCE_STALE" if sample and
                (self.clock()-sample.current.observed_at).total_seconds()>self.config.observation_max_age_seconds else "REFERENCE_UNAVAILABLE")
            diagnostics=getattr(self.providers.underlying,"diagnostics",lambda *args:{})
            self.reference_diagnostics[instance_id]=diagnostics(self.instances[instance_id],self.metadata.get(instance_id,{}))
            self.record("underlying:"+instance_id,valid,reason,critical=False)
        except Exception:
            self.references.pop(instance_id,None)
            self.record("underlying:"+instance_id,False,"REFERENCE_UNAVAILABLE",critical=False)

    def all_tasks(self):
        return (*self.tasks,*self.reference_tasks.values())

    async def resolution_once(self):
        provider = self.providers.resolution
        if provider is None:
            raise RuntimeError("RESOLUTION_NOT_CONFIGURED")
        failed = False
        for instance_id in {p["instance_id"] for p in self.projection["positions"]}:
            instance = self.instances[instance_id]
            if self.clock() < instance.ends_at:
                continue
            try:
                evidence = await provider.resolve(instance)
                if evidence is not None:
                    await self.command("resolve",evidence,reference=self.references.get(instance_id))
                    self.instances[instance_id] = instance_model(await asyncio.to_thread(self.repository.instance,instance_id))
            except Exception:
                failed = True
        self.record("resolution_provider",not failed,"RESOLUTION_REFRESH_FAILED" if failed else "",critical=False)

    async def demand_once(self):
        data = self.providers.market_data
        if data is None:
            raise RuntimeError("MARKET_DATA_NOT_CONFIGURED")
        rules = self.projection["rules"]
        now = self.clock()
        instances = [self.instances[i] for i in set(self.current.values()) | {
            p["instance_id"] for p in self.projection["positions"]} if i in self.instances]
        demand = subscription_demand(instances,rules,self.projection["positions"],now,
            lambda rule,instance,at:entry_eligible(rule["spec"],instance,at) and self.dependencies(rule,instance)
                and self.projection["counts"].get((rule["id"],instance.instance_id),0)<rule["spec"].entry_frequency)
        await data.set_subscriptions(demand)
        self.owner.retain_markets({i.instance_id for i in demand})
        if isinstance(data,MarketDataPool):
            for key,shard in data.shards.items():
                ready=not shard.token_map or (shard.status=="CONNECTED" and not shard.decisions.reason)
                component="market_websocket:"+key
                self.record(component,ready,"" if ready else shard.decisions.reason or "SOCKET_DISCONNECTED",critical=False)
                if ready:
                    self._issues.pop(component,None)
        # Sample unhealthy states continuously even when no actionable tick arrives.
        for rule in rules:
            for instance in instances:
                if instance.definition.key in rule["spec"].markets:
                    self.entry_gate(rule,instance)
        self.record("market_websocket",not demand or data.status=="CONNECTED","" if not demand or data.status=="CONNECTED" else "SOCKET_DISCONNECTED",critical=False)
        fresh = not demand or all(data.token_ready(token) for instance in demand for token in (instance.up_token_id,instance.down_token_id))
        self.record("market_data_freshness",fresh,"" if fresh else "BOOKS_STALE_OR_MISSING",critical=False)
        wanted={i.instance_id for i in demand}
        for component in tuple(self.health._components):
            if component.startswith("market:") and component[7:] not in wanted:
                self.health.forget(component)
        for instance in demand:
            ready=all(data.token_ready(token) for token in (instance.up_token_id,instance.down_token_id))
            self.record("market:"+instance.instance_id,ready,"" if ready else "BOOKS_STALE_OR_MISSING",critical=False)
        self.prune_runtime_instances(now)

    def prune_runtime_instances(self, now):
        """Bound volatile market metadata, never delete durable research history.

        Keep held/subscribed markets and reference workers until their existing
        lifecycle has retired them. This also protects an in-flight reference
        lookup while discovery advances the current market.
        """
        data = self.providers.market_data
        retained = (set(self.current.values())
                    | {p["instance_id"] for p in self.projection["positions"]}
                    | {i.instance_id for i in getattr(data, "token_map", {}).values()}
                    | set(self.reference_tasks))
        expired = {key for key, instance in self.instances.items()
                   if key not in retained and instance.ends_at <= now}
        for key in expired:
            self.instances.pop(key)
            self.metadata.pop(key, None)
            self.references.pop(key, None)
            self.reference_diagnostics.pop(key, None)
            self.health.forget("market:" + key)
            self.health.forget("underlying:" + key)
        for mapping in (self._gate_epochs, self._gate_health, self.stability.since):
            for key in tuple(mapping):
                if key[1] in expired:
                    mapping.pop(key)
        underlying = self.providers.underlying
        retain = getattr(underlying, "retain_instances", None)
        if retain is not None:
            retain(set(self.instances))

    async def database_once(self):
        try:
            if not self.owner.alive:
                self.engine_ok=False
                self.record("engine_worker",False,"DECISION_WORKER_UNAVAILABLE")
                for stream in self.streams.values():
                    stream.invalidate("DECISION_WORKER_UNAVAILABLE")
                async with self._restart_lock:
                    if not self.owner.alive:
                        await self.owner.close()
                        await self.owner.start()
                        self.stability.clear()
                        data=self.providers.market_data
                        if data is not None:
                            await data.resync("DECISION_WORKER_RESTARTED")
            self.owner.projection=await self.owner.call("projection",None)
            self._projection_at=time.monotonic()
            for index,task in enumerate(self.tasks):
                if task.done() and task.get_name().startswith("demo-decisions-"):
                    key=task.get_name()[len("demo-decisions-"):]
                    self.streams[key].invalidate("DECISION_CONSUMER_RESTARTED")
                    await self._resync("DECISION_CONSUMER_RESTARTED",key)
                    self.tasks[index]=asyncio.create_task(self.consume(key,self.streams[key]),name=task.get_name())
            self.database_ok=True
            self.engine_ok=True
            self.record("database",True)
            self.record("engine_worker",True)
        except Exception:
            self.database_ok=False
            self.engine_ok=False
            self.record("engine_worker",False,"DECISION_WORKER_UNAVAILABLE")
            self.record("database",False,"DATABASE_UNAVAILABLE")
            raise

    async def monitor_loop(self):
        while not self._stop.is_set():
            started=time.monotonic()
            await asyncio.sleep(0.1)
            self.loop_lag_seconds=max(0,time.monotonic()-started-0.1)
            self.loop_lag_max_seconds=max(self.loop_lag_max_seconds,self.loop_lag_seconds)
            if not self.owner.alive or time.monotonic()-self._projection_at>5:
                self.engine_ok=False
                self.record("engine_worker",False,"DECISION_OWNER_HEARTBEAT_STALE")
            if any(task.done() for task in self.tasks if task.get_name().startswith("demo-decisions-")):
                self.engine_ok=False
                self.record("engine_worker",False,"DECISION_CONSUMER_STOPPED")

    def pipeline_health(self):
        return {"owner_pid":self.projection["pid"],"owner_alive":self.owner.alive,
            "owner_last_rpc_seconds":self.owner.last_rpc_seconds,
            "projection_age_seconds":time.monotonic()-self._projection_at if self._projection_at else None,
            "loop_lag_seconds":self.loop_lag_seconds,"loop_lag_max_seconds":self.loop_lag_max_seconds,
            "streams":{key:stream.snapshot() for key,stream in self.streams.items()},
            "decision_metrics":self.owner.metrics}

    async def _worker(self, component, operation, interval):
        while not self._stop.is_set():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.issue(component,type(exc).__name__)
                if not self.owner.alive:
                    with suppress(Exception):
                        await asyncio.to_thread(self.repository.incident,component,type(exc).__name__)
                if component == "database":
                    self.database_ok=False
            try:
                await asyncio.wait_for(self._stop.wait(),timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _context(self, observations):
        data=self.providers.market_data
        instances={o.instance_id:self.instances[o.instance_id] for o in observations}
        rules=self.projection["rules"]
        latest=getattr(data,"latest",{})
        gates={}
        until={}
        now=self.clock()
        for key,instance in instances.items():
            until[key]=min(self.discovery_at+timedelta(seconds=30),now+timedelta(seconds=self.config.observation_max_age_seconds)) if self.discovery_at else now-timedelta(seconds=1)
            for token in (instance.up_token_id,instance.down_token_id):
                sample=latest.get(token)
                if sample is not None and hasattr(sample,"observed_at"):
                    until[key]=min(until[key],sample.observed_at+timedelta(seconds=self.config.observation_max_age_seconds),
                                   sample.received_at+timedelta(seconds=self.config.observation_max_age_seconds))
            for rule in rules:
                gate_key=(rule["id"],key)
                gates[gate_key]=(self.entry_gate(rule,instance),self._gate_epochs.get(gate_key,0))
        return instances,gates,until

    async def _resync(self, reason, key):
        data=self.providers.market_data
        if isinstance(data,MarketDataPool):
            await data.resync(reason,shard=key)
        elif data is not None:
            await data.resync(reason)

    async def consume(self, key="default", stream=None):
        stream=stream or self.decisions
        while not self._stop.is_set():
            generation=stream._generation
            try:
                observation=await stream.next_observation()
                if self._generation_seen.get(key)!=observation.generation:
                    self._generation_seen[key]=observation.generation
                    # A reconnect resets stability only for this connection.
                    affected={i.instance_id for i in self.instances.values()
                        if key=="default" or i.definition.timeframe.value==key}
                    for gate in tuple(self.stability.since):
                        if gate[1] in affected:
                            self.stability.since.pop(gate,None)
                observations=[observation]
                deadline=time.monotonic()+self.config.decision_max_queue_delay_seconds-stream.last_queue_delay
                for _ in range(63):
                    if stream._queue.empty():
                        break
                    observations.append(await stream.next_observation())
                instances,gates,until=self._context(observations)
                epochs=getattr(self.providers.market_data,"market_epochs",{})
                result=await self.owner.call("observe",(key,observations,instances,dict(self.references),gates,until,self.owner.epoch,
                    {i:self.owner.market_lease(i) for i in instances},dict(epochs),deadline))
                self._projection_at=time.monotonic()
                if generation!=stream._generation:
                    # A retired queue/RPC cannot invalidate its successor.
                    continue
                self.engine.generation=observations[-1].generation
                self.engine.last_sequence=observations[-1].sequence
                self.engine.reason=result["reason"]
                if result["reason"]:
                    raise ObservationRejected(result["reason"])
                self.engine_ok=True
                self.record("engine_worker",True)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except ObservationRejected as exc:
                reason=str(exc)
                if generation!=stream._generation or reason=="GENERATION_CHANGED":
                    continue
                if reason!="AWAITING_GENERATION" and self._resync_requested.get(key)!=(generation,reason):
                    self._resync_requested[key]=(generation,reason)
                    if not stream.reason:
                        stream.invalidate(reason)
                    with suppress(Exception):
                        await self._resync(reason,key)
                await asyncio.sleep(0.02)
            except Exception:
                self.engine_ok=False
                self.engine.invalidate("ENGINE_FAILURE")
                stream.invalidate("ENGINE_FAILURE")
                self.issue("engine_worker","DECISION_WORKER_UNAVAILABLE")
                with suppress(Exception):
                    await self._resync("ENGINE_FAILURE",key)
                await asyncio.sleep(0.05)

    async def start(self):
        await asyncio.to_thread(self.repository.database.migrate)
        await self.owner.start()
        self._projection_at=time.monotonic()
        self.database_ok=True
        self.record("database",self.database_ok)
        for row in await asyncio.to_thread(self.repository.runtime_instances, self.clock()):
            self.instances[row["id"]]=instance_model(row)
            self.metadata[row["id"]]=json.loads(row["metadata_json"])
        if self.config.public_data_enabled:
            gamma=PublicHTTP("https://gamma-api.polymarket.com")
            binance=PublicHTTP("https://api.binance.com")
            self.clients=[gamma,binance]
            self.providers=Providers(discovery=GammaDiscovery(gamma),resolution=GammaResolution(gamma),
                underlying=BinanceResearchProvider(binance))
        if self.providers.discovery is not None and self.providers.market_data is None:
            self.providers=Providers(discovery=self.providers.discovery,resolution=self.providers.resolution,
                fees=self.providers.fees,underlying=self.providers.underlying,credentials=self.providers.credentials,
                market_data=(MarketDataPool(self.telemetry,generation_factory=lambda:self.command("next_generation"),
                    config=self.config,on_issue=lambda key,code:self.issue("market_websocket:"+key,code),
                    on_market_invalid=self.owner.invalidate_market)
                    if self.config.public_data_enabled else PublicMarketWS(self.decisions,self.telemetry,
                    generation_factory=lambda:self.command("next_generation"),stale_seconds=self.config.observation_max_age_seconds,
                    on_issue=lambda code:self.issue("market_websocket",code),on_market_invalid=self.owner.invalidate_market)))
        if isinstance(self.providers.market_data,MarketDataPool):
            self.streams=self.providers.market_data.streams
        for key,stream in self.streams.items():
            stream.on_state_change=lambda generation,active,k=key:self.owner.fence_stream(k,generation,active)
        self.engine_ok=True
        self.record("engine_worker",True)
        jobs=[("database",self.database_once,2)]
        if self.providers.discovery is not None:
            jobs += [("discovery",self.discover_once,10),
                ("underlying_provider",self.references_once,1),("resolution_provider",self.resolution_once,10),
                ("market_websocket",self.demand_once,0.25)]
            self.tasks.append(asyncio.create_task(self.providers.market_data.run(),name="demo-market-ws"))
        else:
            for name in ("discovery","market_websocket","market_data_freshness","underlying_provider","resolution_provider"):
                self.record(name,False,"PUBLIC_PROVIDERS_DISABLED",critical=False)
        for key,stream in self.streams.items():
            self.tasks.append(asyncio.create_task(self.consume(key,stream),name="demo-decisions-"+key))
        self.tasks.append(asyncio.create_task(self.monitor_loop(),name="demo-loop-monitor"))
        for name,operation,interval in jobs:
            self.tasks.append(asyncio.create_task(self._worker(name,operation,interval),name="demo-"+name))

    async def reset(self, factory=False):
        for stream in self.streams.values():
            stream.invalidate("EXPLICIT_SIMULATION_RESET")
        self.engine.invalidate("EXPLICIT_SIMULATION_RESET")
        self.stability.clear()
        await self.command("reset",factory=factory)
        data=self.providers.market_data
        if data is not None:
            await data.resync("EXPLICIT_SIMULATION_RESET")
            await data.set_subscriptions(())
        self.references.clear()
        return {"reset":"FACTORY" if factory else "TRADING_DATA","rules_preserved":not factory}

    async def close(self):
        self._stop.set()
        if self.providers.market_data is not None:
            await self.providers.market_data.close()
        for task in self.all_tasks():
            task.cancel()
        await asyncio.gather(*self.all_tasks(),return_exceptions=True)
        for task in tuple(self._incident_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._incident_tasks),return_exceptions=True)
        await self.owner.close()
        for client in self.clients:
            await client.close()
        self.record("engine_worker",False,"STOPPED")
