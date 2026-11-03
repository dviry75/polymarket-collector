"""Private bounded RPC to the sole DEMO decision/financial writer process.

Only public observations and DEMO paths/policy cross the inherited local pipe.
The explicit IPC/settings payload excludes credentials, clients and the ASGI app.
The spawned child inherits the service sandbox/environment but never loads env config.
No replay after failure: the parent fences ingress and requests new snapshots.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import multiprocessing
import os
import time

from .config import DemoConfig
from .fences import AtomicFenceSlots
from .engine import DemoEngine
from .domain.eligibility import entry_eligible
from .domain.trading import decimal
from .persistence.database import DemoDatabase
from .persistence.repository import DemoRepository
from .providers.book_primitives import exact_trigger
from .providers.underlying import movement_matches


class DecisionWorkerUnavailable(RuntimeError):
    pass


class HotRepository(DemoRepository):
    """Volatile rearm state; only financial actions write durable runtime rows.

    Recovery always starts a new generation, so replaying these volatile rows
    across process failure would be incorrect. Financial idempotency stays SQL.
    """
    def __init__(self, database, max_age=5):
        super().__init__(database)
        self.previous = {}
        self.max_age=max_age
        self.gate_epochs={}

    def runtime(self, rule_id, observation, conn):
        previous=self.previous.get((rule_id, observation.instance_id, observation.token_id))
        if previous and ((observation.received_at-previous["observed_at"]).total_seconds()>self.max_age or
                previous.get("gate_epoch",0)!=self.gate_epochs.get((rule_id,observation.instance_id),0)):
            return None
        return previous

    def save_runtime(self, conn, rule_id, version_id, obs, last_ask, reason):
        self.previous[(rule_id, obs.instance_id, obs.token_id)] = {
            "generation":obs.generation, "reason":reason, "last_ask_text":last_ask,
            "observed_at":obs.observed_at, "version_id":version_id,
            "gate_epoch":self.gate_epochs.get((rule_id,obs.instance_id),0)}
        # The action marker is part of the same financial transaction.
        if reason == "ENTRY":
            super().save_runtime(conn,rule_id,version_id,obs,last_ask,reason)


class CachedEngine(DemoEngine):
    """Inspect every ordered observation; SQL is needed only for possible actions."""
    def __init__(self, repository, **kwargs):
        super().__init__(repository, **kwargs)
        self.refresh()
        self.processed = self.commits = self.market_rejections = 0
        self.processing_seconds = self.commit_seconds = 0.0

    def refresh(self):
        self.rules = self.repository.rules()
        self.open_positions = self.repository.positions(status="OPEN")
        with self.repository.database.connect() as conn:
            self.counts = {(r["rule_id"],r["instance_id"]):r["count"] for r in conn.execute("SELECT * FROM demo_v2_entry_counts")}

    def _eligible(self, rule, obs, instance, reference, now):
        spec=rule["spec"]
        return (bool(rule["enabled"] and not rule["archived"]) and instance.status=="OPEN"
            and entry_eligible(spec,instance,now) and entry_eligible(spec,instance,obs.observed_at)
            and self.entry_gate(rule,instance)
            and (not spec.underlying_filters or (reference is not None and reference.valid(instance,now,self.max_age)
                and all(movement_matches(f,reference) for f in spec.underlying_filters))))

    def process(self, obs, instance, fee=None, reference=None):
        started=time.monotonic()
        now=self.clock()
        fence=self.observation_guard(obs)
        if fence and fence!="MARKET_FENCED":
            self.invalidate(fence)
        if self.reason or obs.generation != self.generation:
            return []
        if obs.sequence != self.last_sequence+1 or not obs.book_ready or any(
                not 0 <= (now-at).total_seconds() <= self.max_age for at in (obs.observed_at,obs.received_at)):
            self.invalidate("STALE_OR_UNORDERED_OBSERVATION")
            return []
        if obs.instance_id!=instance.instance_id or obs.token_id not in (instance.up_token_id,instance.down_token_id):
            self.invalidate("OBSERVATION_IDENTITY_MISMATCH")
            return []
        if fence=="MARKET_FENCED":
            self.market_rejections+=1
            self.last_sequence=obs.sequence
            self.repository.previous={key:value for key,value in self.repository.previous.items() if key[1]!=obs.instance_id}
            return []
        rules=[r for r in self.rules if instance.definition.key in r["spec"].markets]
        eligible={r["id"]:self._eligible(r,obs,instance,reference,now) for r in rules}
        prior=self.repository.previous
        action=False
        for rule in rules:
            spec=rule["spec"]
            previous=prior.get((rule["id"],obs.instance_id,obs.token_id))
            at_trigger=bool(previous and previous["generation"]==obs.generation and previous["reason"]!="INELIGIBLE"
                and previous.get("gate_epoch",0)==self.repository.gate_epochs.get((rule["id"],obs.instance_id),0)
                and 0 <= (obs.received_at-previous["observed_at"]).total_seconds()<=self.max_age
                and exact_trigger(previous["last_ask_text"],spec.entry_trigger))
            if (eligible[rule["id"]] and exact_trigger(obs.best_ask,spec.entry_trigger) and not at_trigger
                    and self.counts.get((rule["id"],obs.instance_id),0)<spec.entry_frequency):
                action=True
        if obs.best_bid is not None:
            action=action or any(p["instance_id"]==obs.instance_id and p["token_id"]==obs.token_id
                and obs.observed_at>=datetime.fromisoformat(p["entry_at"])
                and ((p["tp_text"] is not None and obs.best_bid>=decimal(p["tp_text"]))
                    or (p["sl_text"] is not None and obs.best_bid<=decimal(p["sl_text"]))) for p in self.open_positions)
        effects=[]
        if action:
            commit_start=time.monotonic()
            try:
                effects=super().process(obs,instance,reference=reference)
            except BaseException:
                # The caller kills/fences this owner; no volatile state survives
                # a failed/uncertain financial commit.
                self.invalidate("FINANCIAL_TRANSACTION_FAILED")
                raise
            self.commit_seconds+=time.monotonic()-commit_start
            self.commits+=1
            self.refresh()
        else:
            self.last_sequence=obs.sequence
        if self.observation_guard(obs)=="MARKET_FENCED":
            self.market_rejections+=1
            self.repository.previous={key:value for key,value in self.repository.previous.items() if key[1]!=obs.instance_id}
            return effects
        if not self.reason:
            for rule in rules:
                self.repository.save_runtime(None,rule["id"],rule["current_version_id"],obs,obs.best_ask,
                    "OBSERVED" if eligible[rule["id"]] else "INELIGIBLE")
        self.processed+=1
        self.processing_seconds+=time.monotonic()-started
        return effects


# Explicit command surface: no arbitrary object/method execution over IPC.
COMMANDS={"create_rule","update_rule","upsert_instance","resolve","reset","incident","next_generation"}


class DecisionOwner:
    """Synchronous child-side state owner, also usable with temp DBs in tests."""
    def __init__(self, config, stream_fences=None, market_fences=None):
        self.stream_fences=stream_fences
        self.market_fences=market_fences
        self.leases={}
        self.market_epochs={}
        self.deadline=None
        self.repository=HotRepository(DemoDatabase(config),config.observation_max_age_seconds)
        self.repository.ensure_trade_serials()
        self.config=config
        self.engines={}
        self.generation=0
        self.gates={}
        self.gate_until={}
        self.gate_epoch=0

    def entry_gate(self, rule, instance):
        until=self.gate_until.get(instance.instance_id)
        value=self.gates.get((rule["id"],instance.instance_id),False)
        allowed=value[0] if isinstance(value,tuple) else value
        return bool(until and datetime.now(timezone.utc)<=until and allowed)

    def engine(self, stream, generation):
        engine=self.engines.get(stream)
        if engine is None:
            engine=CachedEngine(self.repository,max_age=self.config.observation_max_age_seconds,
                flat_fee_per_action=self.config.flat_fee_per_action,entry_gate=self.entry_gate)
            self.engines[stream]=engine
        if generation>engine.generation:
            engine.begin_generation(generation)
        engine.durable_generation=self.generation or generation
        index={"default":0,"5m":1,"15m":2,"1h":3}[stream]
        def guard(obs):
            if self.deadline is not None and time.monotonic()>self.deadline:
                return "DECISION_QUEUE_DELAY_EXCEEDED"
            if self.stream_fences is not None and self.stream_fences[index]!=obs.generation:
                return "STREAM_FENCED"
            lease=self.leases.get(obs.instance_id)
            if (obs.market_epoch!=self.market_epochs.get(obs.instance_id,0) or
                    (lease is not None and self.market_fences is not None and self.market_fences[lease[0]]!=lease[1])):
                return "MARKET_FENCED"
            return ""
        engine.observation_guard=guard
        return engine

    def projection(self):
        repo=self.repository
        with repo.database.connect() as conn:
            counts={(r["rule_id"],r["instance_id"]):r["count"] for r in conn.execute("SELECT * FROM demo_v2_entry_counts")}
        return {"rules":repo.rules(),"positions":repo.positions(status="OPEN"),"counts":counts,
                "generation":self.generation,"pid":os.getpid()}

    def dispatch(self, operation, payload):
        if operation=="projection":
            return self.projection()
        if operation=="observe":
            stream, observations, instances, references, gates, until, epoch, leases, market_epochs, deadline=payload
            if len(observations)>64:
                raise ValueError("decision batch exceeds bounded IPC contract")
            if epoch != self.gate_epoch:
                # Context computed before a serialized admin command is stale.
                return {"reason":"CONTROL_CONTEXT_CHANGED","effects":[],"projection":self.projection()}
            self.gates,self.gate_until=gates,until
            self.repository.gate_epochs={key:value[1] if isinstance(value,tuple) else 0 for key,value in gates.items()}
            self.leases,self.market_epochs,self.deadline=leases,market_epochs,deadline
            effects=[]
            for obs in observations:
                engine=self.engine(stream,obs.generation)
                effects.extend(engine.process(obs,instances[obs.instance_id],reference=references.get(obs.instance_id)))
                if engine.reason:
                    return {"reason":engine.reason,"effects":effects,"projection":self.projection()}
            return {"reason":"","effects":effects,"projection":self.projection() if effects else None,
                "metrics":{"processed":engine.processed,"commits":engine.commits,
                    "stream":stream,"processing_seconds":engine.processing_seconds,"commit_seconds":engine.commit_seconds,
                    "market_fenced_observations":engine.market_rejections}}
        if operation not in COMMANDS:
            raise ValueError("unsupported decision command")
        args,kwargs=payload
        result=getattr(self.repository,operation)(*args,**kwargs)
        if operation=="next_generation":
            self.generation=result
        elif operation in {"create_rule","update_rule","reset"}:
            self.gate_epoch+=1
        if operation=="reset":
            self.engines.clear()
            self.repository.previous.clear()
            with self.repository.database.connect() as conn:
                self.generation=int(conn.execute("SELECT value FROM demo_v2_meta WHERE key='generation'").fetchone()[0])
        for engine in self.engines.values():
            engine.refresh()
        return {"result":result,"projection":self.projection(),"epoch":self.gate_epoch}


def _serve(connection, settings, stream_fences, market_fences):
    # Minimal explicit settings, never DemoConfig.from_env / parent credentials.
    owner=DecisionOwner(DemoConfig(**settings),stream_fences,market_fences)
    try:
        while True:
            operation,payload=connection.recv()
            if operation=="stop":
                break
            try:
                result=owner.dispatch(operation,payload)
                connection.send((True,result))
            except (ValueError,KeyError) as exc:
                if operation=="observe":
                    raise  # Any financial uncertainty retires this process.
                connection.send((False,(type(exc).__name__,str(exc))))
    except (EOFError,BrokenPipeError):
        pass
    finally:
        connection.close()


class DecisionWorker:
    """One in-flight bounded pipe request; waiting never blocks the event loop."""
    def __init__(self, config, *, timeout=6):
        self.settings={"data_dir":config.data_dir,"observation_max_age_seconds":config.observation_max_age_seconds,
                       "flat_fee_per_action":config.flat_fee_per_action}
        self.timeout=timeout
        self.process=self.connection=None
        self.lock=asyncio.Lock()
        self.stream_fences=AtomicFenceSlots(4)
        self.market_fences=AtomicFenceSlots(2048)
        self.market_slots={}
        self._lease_counter=0
        self.projection={"rules":[],"positions":[],"counts":{},"generation":0,"pid":None}
        self.metrics={}
        self.epoch=0
        self.incarnation=0
        self.last_success=0.0
        self.last_rpc_seconds=0.0

    @property
    def alive(self):
        return self.process is not None and self.process.is_alive()

    async def start(self):
        if self.alive:
            raise RuntimeError("decision owner already running")
        self.epoch=0
        self.metrics={}
        self.incarnation+=1
        context=multiprocessing.get_context("spawn")
        parent,child=context.Pipe()
        self.process=context.Process(target=_serve,args=(child,self.settings,self.stream_fences,self.market_fences),name="demo-decision-owner",daemon=True)
        self.process.start()
        child.close()
        self.connection=parent
        self.projection=await self.call("projection",None)
        return self.projection

    def _exchange(self, connection, operation, payload):
        connection.send((operation,payload))
        if not connection.poll(self.timeout):
            raise TimeoutError("DECISION_WORKER_TIMEOUT")
        return connection.recv()

    async def call(self, operation, payload):
        incarnation=self.incarnation
        async with self.lock:
            if incarnation!=self.incarnation or not self.alive:
                raise DecisionWorkerUnavailable("DECISION_WORKER_UNAVAILABLE")
            started=time.monotonic()
            try:
                ok,result=await asyncio.to_thread(self._exchange,self.connection,operation,payload)
            except asyncio.CancelledError:
                await self.close()
                raise
            except (EOFError,OSError,TimeoutError):
                await self.close()
                raise DecisionWorkerUnavailable("DECISION_WORKER_UNAVAILABLE") from None
            self.last_rpc_seconds=time.monotonic()-started
            self.last_success=time.monotonic()
            if not ok:
                kind,message=result
                raise (KeyError if kind=="KeyError" else ValueError)(message)
            if isinstance(result,dict):
                if result.get("projection") is not None:
                    self.projection=result["projection"]
                if "epoch" in result:
                    self.epoch=result["epoch"]
                if "metrics" in result:
                    self.metrics[result["metrics"].get("stream",operation)]=result["metrics"]
            return result

    def market_lease(self, instance_id):
        if instance_id not in self.market_slots:
            used=set(self.market_slots.values())
            slot=next((i for i in range(len(self.market_fences)) if i not in used),None)
            if slot is None:
                raise DecisionWorkerUnavailable("MARKET_FENCE_CAPACITY_EXCEEDED")
            self.market_slots[instance_id]=slot
            self.invalidate_market(instance_id)
        slot=self.market_slots[instance_id]
        return slot,self.market_fences[slot]

    def invalidate_market(self, instance_id):
        slot=self.market_slots.get(instance_id)
        if slot is not None:
            self._lease_counter+=1
            self.market_fences[slot]=self._lease_counter

    def retain_markets(self, instance_ids):
        for key in tuple(self.market_slots):
            if key not in instance_ids:
                self.invalidate_market(key)
                self.market_slots.pop(key)

    def fence_stream(self, key, generation, active):
        self.stream_fences[{"default":0,"5m":1,"15m":2,"1h":3}[key]]=generation if active else 0

    async def command(self, name, *args, **kwargs):
        return (await self.call(name,(args,kwargs)))["result"]

    async def close(self):
        process,self.process=self.process,None
        self.incarnation+=1
        if process is not None:
            # Do not reuse a pipe/thread with an uncertain in-flight commit.
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join,2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join,2)
        if self.connection is not None:
            with suppress(OSError):
                self.connection.close()
            self.connection=None
