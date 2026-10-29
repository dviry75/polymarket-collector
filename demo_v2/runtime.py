"""DEMO-only public-data workers and single ordered decision consumer."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json

from .domain.eligibility import entry_eligible
from .domain.models import market_definitions
from .domain.trading import FeeState
from .engine import DemoEngine
from .health import ComponentHealth, HealthState, StabilityGate
from .persistence.repository import dump, instance_model, utc
from .providers.contracts import Providers
from .providers.discovery import GammaDiscovery, GammaResolution
from .providers.fees import GammaFeeProvider
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
        self.instances, self.metadata, self.current, self.fees, self.references = {}, {}, {}, {}, {}
        self.discovery_at = None
        self.database_ok = self.engine_ok = False
        self.stability = StabilityGate(config.recovery_stability_seconds)
        self.engine = DemoEngine(repository,clock=clock,max_age=config.observation_max_age_seconds,entry_gate=self.entry_gate)
        self._stop = asyncio.Event()
        self._issues = {}

    def record(self, component, healthy, reason="", *, critical=True):
        self.health.record(ComponentHealth(component,HealthState.READY if healthy else HealthState.NOT_READY,reason,critical))

    def issue(self, component, code):
        self.record(component,False,code)
        if self._issues.get(component) != code:
            with suppress(Exception):
                self.repository.incident(component,code)
        self._issues[component] = code

    def discovery_fresh(self, instance):
        return bool(self.discovery_at and 0 <= (self.clock()-self.discovery_at).total_seconds() <= 30
                    and self.current.get(instance.definition.key) == instance.instance_id)

    def dependencies(self, rule, instance):
        fee = self.fees.get(instance.instance_id)
        reference = self.references.get(instance.instance_id)
        spec = rule.get("spec")
        if spec is None:
            from .domain.trading import RuleSpec
            spec = RuleSpec.model_validate_json(rule["snapshot_json"])
        return bool(self.database_ok and self.engine_ok and self.discovery_fresh(instance)
            and fee is not None and fee.current(self.clock()).state == FeeState.VALID
            and (not spec.underlying_filters or (reference and reference.valid(instance,self.clock(),self.config.observation_max_age_seconds))))

    def entry_gate(self, rule, instance):
        data = self.providers.market_data
        tokens = (instance.up_token_id,instance.down_token_id)
        healthy = self.dependencies(rule,instance) and data is not None and all(data.token_ready(token) for token in tokens)
        return self.stability.ready((rule["id"],instance.instance_id),healthy)

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
                self.repository.upsert_instance(instance,metadata.get(instance.instance_id,{}))
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

    async def fees_once(self):
        provider = self.providers.fees
        if provider is None:
            raise RuntimeError("FEES_NOT_CONFIGURED")
        wanted = set(self.current.values()) | {p["instance_id"] for p in self.repository.positions(status="OPEN")}
        for instance_id in wanted:
            try:
                evidence = await provider.parameters(self.instances[instance_id])
                if evidence is None or evidence.instance_id != instance_id:
                    self.fees.pop(instance_id,None)
                    continue
                self.fees[instance_id] = evidence
                with self.repository.database.transaction() as conn:
                    conn.execute("INSERT OR REPLACE INTO demo_v2_fee_evidence VALUES (?,?,?,?,?)",(
                        instance_id,evidence.state.value,utc(evidence.observed_at),evidence.source,dump(evidence.evidence())))
            except Exception:
                self.fees.pop(instance_id,None)
        ready = bool(wanted) and all(i in self.fees and self.fees[i].current(self.clock()).state==FeeState.VALID for i in wanted)
        self.record("fee_provider",ready,"" if ready else "VALID_FEE_EVIDENCE_UNAVAILABLE",critical=False)

    async def references_once(self):
        provider = self.providers.underlying
        if provider is None:
            raise RuntimeError("UNDERLYING_NOT_CONFIGURED")
        # Research snapshots are best effort for every active/held instance;
        # only a rule explicitly requiring them is gated by their availability.
        wanted = set(self.current.values()) | {p["instance_id"] for p in self.repository.positions(status="OPEN")}
        for instance_id in wanted:
            try:
                sample = await provider.context(self.instances[instance_id],self.metadata.get(instance_id,{}))
                if sample is not None and sample.valid(self.instances[instance_id],self.clock(),self.config.observation_max_age_seconds):
                    self.references[instance_id] = sample
                else:
                    self.references.pop(instance_id,None)
            except Exception:
                self.references.pop(instance_id,None)
        self.record("underlying_provider",bool(wanted) and wanted <= self.references.keys(),"REFERENCE_PARTIAL_OR_UNAVAILABLE",critical=False)

    async def resolution_once(self):
        provider = self.providers.resolution
        if provider is None:
            raise RuntimeError("RESOLUTION_NOT_CONFIGURED")
        failed = False
        for instance_id in {p["instance_id"] for p in self.repository.positions(status="OPEN")}:
            instance = self.instances[instance_id]
            if self.clock() < instance.ends_at:
                continue
            try:
                evidence = await provider.resolve(instance)
                if evidence is not None:
                    self.repository.resolve(evidence)
                    self.instances[instance_id] = instance_model(self.repository.instance(instance_id))
            except Exception:
                failed = True
        self.record("resolution_provider",not failed,"RESOLUTION_REFRESH_FAILED" if failed else "",critical=False)

    async def demand_once(self):
        data = self.providers.market_data
        if data is None:
            raise RuntimeError("MARKET_DATA_NOT_CONFIGURED")
        rules = self.repository.rules()
        now = self.clock()
        instances = [self.instances[i] for i in set(self.current.values()) | {
            p["instance_id"] for p in self.repository.positions(status="OPEN")} if i in self.instances]
        demand = subscription_demand(instances,rules,self.repository.positions(status="OPEN"),now,
            lambda rule,instance,at:entry_eligible(rule["spec"],instance,at) and self.dependencies(rule,instance))
        await data.set_subscriptions(demand)
        # Sample unhealthy states continuously even when no actionable tick arrives.
        for rule in rules:
            for instance in instances:
                if instance.definition.key in rule["spec"].markets:
                    self.entry_gate(rule,instance)
        self.record("market_websocket",not demand or data.status=="CONNECTED","" if not demand or data.status=="CONNECTED" else "SOCKET_DISCONNECTED")
        fresh = not demand or all(data.token_ready(token) for instance in demand for token in (instance.up_token_id,instance.down_token_id))
        self.record("market_data_freshness",fresh,"" if fresh else "BOOKS_STALE_OR_MISSING")

    async def database_once(self):
        self.database_ok = False
        self.database_ok = self.repository.database.probe()
        self.record("database",self.database_ok)
        consumer = next((task for task in self.tasks if task.get_name()=="demo-decisions"),None)
        if self.database_ok and consumer is not None and not consumer.done():
            # Recover dependency eligibility; the invalidated engine still needs
            # a new stream generation before any action can be committed.
            self.engine_ok=True
            self.record("engine_worker",True)

    async def _worker(self, component, operation, interval):
        while not self._stop.is_set():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.issue(component,type(exc).__name__)
                if component == "database":
                    self.database_ok=False
            try:
                await asyncio.wait_for(self._stop.wait(),timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def consume(self):
        while not self._stop.is_set():
            try:
                observation = await self.decisions.next_observation()
                # There are no awaits between generation validation and transaction.
                if observation.generation != self.engine.generation:
                    self.engine.begin_generation(observation.generation)
                    self.stability.clear()
                instance = self.instances.get(observation.instance_id)
                if instance is None:
                    raise ValueError("UNKNOWN_INSTANCE")
                self.engine.process(observation,instance,self.fees.get(instance.instance_id),self.references.get(instance.instance_id))
                self.engine_ok=True
                self.record("engine_worker",True)
            except asyncio.CancelledError:
                raise
            except ObservationRejected:
                self.engine.invalidate(self.decisions.reason or "STREAM_INVALIDATED")
                self.stability.clear()
                await asyncio.sleep(0.02)
            except Exception as exc:
                self.engine_ok=False
                self.engine.invalidate("ENGINE_FAILURE")
                self.decisions.invalidate("ENGINE_FAILURE")
                self.issue("engine_worker",type(exc).__name__)
                # Require new snapshots; do not replay a failed decision.
                data=self.providers.market_data
                if data is not None and data._ws is not None:
                    with suppress(Exception):
                        await data._ws.close()
                await asyncio.sleep(0.05)

    async def start(self):
        self.repository.database.migrate()
        self.database_ok=self.repository.database.probe()
        self.record("database",self.database_ok)
        for row in self.repository.instances():
            self.instances[row["id"]]=instance_model(row)
            self.metadata[row["id"]]=json.loads(row["metadata_json"])
        if self.config.public_data_enabled:
            gamma=PublicHTTP("https://gamma-api.polymarket.com")
            binance=PublicHTTP("https://api.binance.com")
            self.clients=[gamma,binance]
            self.providers=Providers(discovery=GammaDiscovery(gamma),resolution=GammaResolution(gamma),
                fees=GammaFeeProvider(gamma),underlying=BinanceResearchProvider(binance))
        if self.providers.discovery is not None and self.providers.market_data is None:
            self.providers=Providers(discovery=self.providers.discovery,resolution=self.providers.resolution,
                fees=self.providers.fees,underlying=self.providers.underlying,credentials=self.providers.credentials,
                market_data=PublicMarketWS(self.decisions,self.telemetry,generation_factory=self.repository.next_generation,
                    stale_seconds=self.config.observation_max_age_seconds,on_issue=lambda code:self.issue("market_websocket",code)))
        self.engine_ok=True
        self.record("engine_worker",True)
        jobs=[("database",self.database_once,2)]
        if self.providers.discovery is not None:
            jobs += [("discovery",self.discover_once,10),("fee_provider",self.fees_once,30),
                ("underlying_provider",self.references_once,1),("resolution_provider",self.resolution_once,10),
                ("market_websocket",self.demand_once,0.25)]
            self.tasks.append(asyncio.create_task(self.providers.market_data.run(),name="demo-market-ws"))
        else:
            for name in ("discovery","market_websocket","market_data_freshness","fee_provider","underlying_provider","resolution_provider"):
                self.record(name,False,"PUBLIC_PROVIDERS_DISABLED",critical=False)
        self.tasks.append(asyncio.create_task(self.consume(),name="demo-decisions"))
        for name,operation,interval in jobs:
            self.tasks.append(asyncio.create_task(self._worker(name,operation,interval),name="demo-"+name))

    async def reset(self, factory=False):
        self.decisions.invalidate("EXPLICIT_SIMULATION_RESET")
        self.engine.invalidate("EXPLICIT_SIMULATION_RESET")
        self.stability.clear()
        self.repository.reset(factory=factory)
        data=self.providers.market_data
        if data is not None:
            data.latest.clear()
            await data.set_subscriptions(())
        self.references.clear()
        return {"reset":"FACTORY" if factory else "TRADING_DATA","rules_preserved":not factory}

    async def close(self):
        self._stop.set()
        if self.providers.market_data is not None:
            await self.providers.market_data.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for client in self.clients:
            await client.close()
        self.record("engine_worker",False,"STOPPED")
