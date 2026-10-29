"""Single event-loop decision owner. Synchronous atomic writes, no provider awaits.

Inputs are observations from DecisionStream and already fetched evidence. Network
refresh never blocks a decision or selects observations via telemetry sampling.
"""

from datetime import datetime, timezone
from decimal import Decimal

from .domain.eligibility import entry_eligible
from .domain.trading import ExitReason, FeeState, decimal
from .persistence.repository import dump, identifier, text, utc
from .providers.book_primitives import exact_trigger
from .providers.underlying import movement_matches


class DemoEngine:
    def __init__(self, repository, *, clock=lambda: datetime.now(timezone.utc), max_age=5,
                 entry_gate=lambda rule, instance: True):
        self.repository, self.clock, self.max_age, self.entry_gate = repository, clock, max_age, entry_gate
        self.generation = 0
        self.last_sequence = 0
        self.reason = "AWAITING_GENERATION"

    def begin_generation(self, generation):
        if generation <= self.generation:
            raise ValueError("generation must advance")
        self.generation, self.last_sequence, self.reason = generation, 0, ""

    def invalidate(self, reason):
        self.reason = reason

    def _research(self, conn, position_id, instance, context, kind):
        if context is None or not context.valid(instance, self.clock(), self.max_age):
            return
        points = (("MARKET_OPEN",context.baseline),(kind,context.current)) if kind == "ENTRY" else ((kind,context.current),)
        for label, point in points:
            conn.execute("INSERT INTO demo_v2_underlying VALUES (?,?,?,?,?,?,?,?,?,?)", (
                identifier(),instance.instance_id,position_id,label,context.asset,text(point.price),
                utc(point.observed_at),point.source,point.reference_id,dump(context.evidence())))

    def process(self, obs, instance, fee=None, reference=None):
        now = self.clock()
        if self.reason or obs.generation != self.generation:
            return []
        if obs.sequence != self.last_sequence+1 or not obs.book_ready or any(
                not 0 <= (now-at).total_seconds() <= self.max_age for at in (obs.observed_at,obs.received_at)):
            self.invalidate("STALE_OR_UNORDERED_OBSERVATION")
            return []
        self.last_sequence = obs.sequence
        if obs.instance_id != instance.instance_id or obs.token_id not in (instance.up_token_id,instance.down_token_id):
            self.invalidate("OBSERVATION_IDENTITY_MISMATCH")
            return []
        applicable = fee.current(now) if fee is not None and fee.instance_id == instance.instance_id else None
        reference = reference if reference and reference.valid(instance,now,self.max_age) else None
        effects = []
        repo = self.repository
        with repo.database.transaction() as conn:
            # Reject actions racing an explicit reset or newer transport generation.
            durable_generation = int(conn.execute("SELECT value FROM demo_v2_meta WHERE key='generation'").fetchone()[0])
            if durable_generation != obs.generation:
                self.invalidate("DURABLE_GENERATION_CHANGED")
                return []
            stored = conn.execute("SELECT * FROM demo_v2_instances WHERE id=?",(instance.instance_id,)).fetchone()
            if not stored or not stored["verified"] or stored["resolution_status"] != "PENDING":
                return []
            provenance = {"observation":obs,"market":instance,"underlying":reference.evidence() if reference else None}
            # Exits are independent of current rule enablement, schedules and entry health.
            for row in conn.execute("SELECT * FROM demo_v2_positions WHERE status='OPEN' AND instance_id=? AND token_id=?",
                                    (instance.instance_id,obs.token_id)).fetchall():
                bid = obs.best_bid
                reason = None
                if bid is not None and row["tp_text"] is not None and bid >= decimal(row["tp_text"]):
                    reason = ExitReason.TAKE_PROFIT
                elif bid is not None and row["sl_text"] is not None and bid <= decimal(row["sl_text"]):
                    reason = ExitReason.STOP_LOSS
                if reason and applicable and applicable.state == FeeState.VALID:
                    fee_amount = applicable.fee(decimal(row["net_shares_text"]),bid)
                    position = repo.close_position(row["id"],price=bid,sell_fee=fee_amount,reason=reason,
                        occurred_at=obs.observed_at,provenance={**provenance,"fee":applicable.evidence()},conn=conn)
                    self._research(conn,row["id"],instance,reference,"EXIT")
                    effects.append(position)
            rows = conn.execute("""SELECT r.*,v.snapshot_json FROM demo_v2_rules r JOIN demo_v2_rule_versions v
                ON r.current_version_id=v.id WHERE r.enabled=1 AND r.archived=0""").fetchall()
            from .domain.trading import RuleSpec
            for rule in rows:
                spec = RuleSpec.model_validate_json(rule["snapshot_json"])
                if instance.definition.key not in spec.markets:
                    continue
                previous = repo.runtime(rule["id"],obs,conn)
                eligible = (entry_eligible(spec,instance,now) and entry_eligible(spec,instance,obs.observed_at)
                    and stored["status"] == "OPEN" and self.entry_gate(dict(rule),instance)
                    and applicable is not None and applicable.state == FeeState.VALID)
                if spec.underlying_filters:
                    eligible = eligible and reference is not None and all(movement_matches(f,reference) for f in spec.underlying_filters)
                # An eligibility gap rearms first fresh observation on return. An edit
                # alone does not rearm an unchanged price within this generation.
                prior_at_trigger = (previous and previous["generation"] == obs.generation
                    and previous["reason"] != "INELIGIBLE" and previous["last_ask_text"] is not None
                    and exact_trigger(previous["last_ask_text"],spec.entry_trigger))
                trigger = eligible and exact_trigger(obs.best_ask,spec.entry_trigger) and not prior_at_trigger
                if trigger and repo.entry_count(rule["id"],instance.instance_id,conn) < spec.entry_frequency:
                    amounts = applicable.buy(spec.gross_shares,obs.best_ask)
                    position = repo.open_position(rule_id=rule["id"],version_id=rule["current_version_id"],
                        observation=obs,amounts=amounts,fee_evidence=applicable.evidence(),provenance=provenance,
                        idempotency_key=f"{rule['id']}:{instance.instance_id}:{obs.generation}:{obs.sequence}",conn=conn)
                    self._research(conn,position["id"],instance,reference,"ENTRY")
                    effects.append(position)
                else:
                    repo.save_runtime(conn,rule["id"],rule["current_version_id"],obs,obs.best_ask,"OBSERVED" if eligible else "INELIGIBLE")
        return effects
