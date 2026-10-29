"""DEMO research state. All financial aggregation uses Decimal, never SQL REAL."""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import sqlite3
from uuid import uuid4

from ..domain.models import MarketInstance, MarketObservation, OfficialResolution
from ..domain.trading import EntryAmounts, ExitReason, RuleSpec, Side, classify, decimal, text
from .database import DemoDatabase


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier() -> str:
    return uuid4().hex


def json_value(value):
    from pydantic import BaseModel
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def dump(value) -> str:
    return json.dumps(value, default=json_value, sort_keys=True, separators=(",", ":"))


def utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc).isoformat()


class DemoRepository:
    def __init__(self, database: DemoDatabase):
        self.database = database

    def definitions(self) -> list[dict]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM demo_v2_definitions ORDER BY id")]

    def upsert_instance(self, instance: MarketInstance, metadata: dict | None = None) -> None:
        if not instance.verified or not instance.external_id or not instance.metadata_source or instance.discovered_at is None:
            raise ValueError("market instance requires verified identity and provenance")
        metadata={**(metadata or {}),"_verified_definition":{"family_id":instance.definition.family_id,
            "provenance":instance.definition.provenance}}
        with self.database.transaction() as conn:
            if instance.definition.family_id:
                conn.execute("UPDATE demo_v2_definitions SET family_id=?,provenance_json=? WHERE id=?",(
                    instance.definition.family_id,dump({"source":instance.definition.provenance,"provider_verified":True}),instance.definition.key))
            existing = conn.execute("SELECT * FROM demo_v2_instances WHERE id=?", (instance.instance_id,)).fetchone()
            identities = (instance.definition.key, instance.external_id, instance.condition_id, instance.up_token_id,
                          instance.down_token_id, utc(instance.starts_at), utc(instance.ends_at))
            if existing:
                stored = tuple(existing[key] for key in ("definition_id", "external_id", "condition_id", "up_token_id", "down_token_id", "starts_at", "ends_at"))
                if stored != identities:
                    raise ValueError("immutable market identity changed")
            else:
                collision = conn.execute("SELECT id FROM demo_v2_instances WHERE up_token_id IN (?,?) OR down_token_id IN (?,?)",
                                         (instance.up_token_id, instance.down_token_id)*2).fetchone()
                if collision:
                    raise ValueError("token is already bound to another instance")
                conn.execute("""INSERT INTO demo_v2_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    instance.instance_id, *identities[:5], instance.slug, *identities[5:],
                    utc(instance.discovered_at), instance.metadata_source, 1, instance.status,
                    "PENDING", None, "{}", dump(metadata or {}),
                ))
            conn.execute("UPDATE demo_v2_instances SET metadata_json=? WHERE id=?", (dump(metadata or {}), instance.instance_id))
            conn.execute("UPDATE demo_v2_instances SET status=?,discovered_at=? WHERE id=? AND resolution_status='PENDING'",
                         (instance.status,utc(instance.discovered_at),instance.instance_id))

    def instances(self) -> list[dict]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM demo_v2_instances ORDER BY starts_at,id")]

    def instance(self, instance_id: str) -> dict:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM demo_v2_instances WHERE id=?", (instance_id,)).fetchone()
            if not row:
                raise KeyError("market instance not found")
            return dict(row)

    def create_rule(self, spec: RuleSpec) -> dict:
        rule_id, version_id, timestamp = identifier(), identifier(), now_iso()
        with self.database.transaction() as conn:
            conn.execute("INSERT INTO demo_v2_rules VALUES (?,?,?,?,NULL,?,?)", (
                rule_id, spec.name, int(spec.enabled), int(spec.archived), timestamp, timestamp,
            ))
            conn.execute("INSERT INTO demo_v2_rule_versions VALUES (?,?,?,?,?)", (version_id, rule_id, 1, spec.model_dump_json(), timestamp))
            conn.execute("UPDATE demo_v2_rules SET current_version_id=? WHERE id=?", (version_id, rule_id))
        return self.rule(rule_id)

    def rule(self, rule_id: str) -> dict:
        with self.database.connect() as conn:
            row = conn.execute("""SELECT r.*,v.version,v.snapshot_json FROM demo_v2_rules r
                JOIN demo_v2_rule_versions v ON v.id=r.current_version_id WHERE r.id=?""", (rule_id,)).fetchone()
            if row is None:
                raise KeyError("rule not found")
            return {**dict(row), "spec": RuleSpec.model_validate_json(row["snapshot_json"])}

    def rules(self) -> list[dict]:
        with self.database.connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM demo_v2_rules ORDER BY created_at,id")]
        return [self.rule(rule_id) for rule_id in ids]

    def update_rule(self, rule_id: str, spec: RuleSpec, *, expected_version: str | None = None) -> dict:
        with self.database.transaction() as conn:
            current = conn.execute("SELECT * FROM demo_v2_rules WHERE id=?", (rule_id,)).fetchone()
            if current is None:
                raise KeyError("rule not found")
            if expected_version and current["current_version_id"] != expected_version:
                raise ValueError("rule version changed; reload before editing")
            version = conn.execute("SELECT MAX(version) FROM demo_v2_rule_versions WHERE rule_id=?", (rule_id,)).fetchone()[0]+1
            version_id, timestamp = identifier(), now_iso()
            conn.execute("INSERT INTO demo_v2_rule_versions VALUES (?,?,?,?,?)", (version_id, rule_id, version, spec.model_dump_json(), timestamp))
            conn.execute("UPDATE demo_v2_rules SET name=?,enabled=?,archived=?,current_version_id=?,updated_at=? WHERE id=?", (
                spec.name, int(spec.enabled), int(spec.archived), version_id, timestamp, rule_id,
            ))
        return self.rule(rule_id)

    def next_generation(self) -> int:
        with self.database.transaction() as conn:
            conn.execute("UPDATE demo_v2_meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
            return int(conn.execute("SELECT value FROM demo_v2_meta WHERE key='generation'").fetchone()[0])

    def entry_count(self, rule_id: str, instance_id: str, conn=None) -> int:
        if conn is None:
            with self.database.connect() as connection:
                return self.entry_count(rule_id, instance_id, connection)
        row = conn.execute("SELECT count FROM demo_v2_entry_counts WHERE rule_id=? AND instance_id=?", (rule_id, instance_id)).fetchone()
        return row[0] if row else 0

    def runtime(self, rule_id: str, observation: MarketObservation, conn) -> dict | None:
        row = conn.execute("SELECT * FROM demo_v2_entry_runtime WHERE rule_id=? AND instance_id=? AND token_id=?", (
            rule_id, observation.instance_id, observation.token_id,
        )).fetchone()
        return dict(row) if row else None

    def save_runtime(self, conn, rule_id: str, version_id: str, obs: MarketObservation, last_ask: Decimal | None, reason: str) -> None:
        conn.execute("""INSERT INTO demo_v2_entry_runtime VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rule_id,instance_id,token_id) DO UPDATE SET
            version_id=excluded.version_id,last_ask_text=excluded.last_ask_text,generation=excluded.generation,
            sequence=excluded.sequence,observed_at=excluded.observed_at,reason=excluded.reason""", (
            rule_id, obs.instance_id, obs.token_id, version_id, text(last_ask) if last_ask is not None else None,
            obs.generation, obs.sequence, utc(obs.observed_at), reason,
        ))

    def open_position(self, *, rule_id: str, version_id: str, observation: MarketObservation,
                      amounts: EntryAmounts, fee_evidence: dict, provenance: dict,
                      idempotency_key: str, conn=None) -> dict:
        if conn is None:
            with self.database.transaction() as connection:
                return self.open_position(rule_id=rule_id, version_id=version_id, observation=observation,
                    amounts=amounts, fee_evidence=fee_evidence, provenance=provenance,
                    idempotency_key=idempotency_key, conn=connection)
        previous = conn.execute("SELECT * FROM demo_v2_positions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if previous:
            return dict(previous)
        rule = conn.execute("""SELECT r.*,v.snapshot_json FROM demo_v2_rules r JOIN demo_v2_rule_versions v
            ON v.id=r.current_version_id WHERE r.id=?""", (rule_id,)).fetchone()
        if not rule or not rule["enabled"] or rule["archived"] or rule["current_version_id"] != version_id:
            raise ValueError("rule is no longer entry eligible")
        spec = RuleSpec.model_validate_json(rule["snapshot_json"])
        instance = conn.execute("SELECT * FROM demo_v2_instances WHERE id=?", (observation.instance_id,)).fetchone()
        if not instance or not instance["verified"] or instance["status"] != "OPEN" or instance["resolution_status"] != "PENDING":
            raise ValueError("market is not eligible")
        if fee_evidence.get("state") != "VALID":
            raise ValueError("valid fee evidence is required")
        if instance["definition_id"] not in spec.markets or observation.token_id not in (instance["up_token_id"], instance["down_token_id"]):
            raise ValueError("rule/market/token mismatch")
        if observation.best_ask != spec.entry_trigger or amounts.gross_shares != spec.gross_shares:
            raise ValueError("entry amounts/trigger mismatch")
        if amounts.notional != amounts.gross_shares * observation.best_ask:
            raise ValueError("entry notional does not reconcile")
        count = self.entry_count(rule_id, observation.instance_id, conn)
        if count >= spec.entry_frequency:
            raise ValueError("shared entry frequency exhausted")
        position_id = identifier()
        fields = dict(
            id=position_id, idempotency_key=idempotency_key, rule_id=rule_id, rule_version_id=version_id,
            instance_id=observation.instance_id, token_id=observation.token_id,
            side=Side.UP.value if observation.token_id == instance["up_token_id"] else Side.DOWN.value,
            status="OPEN", entry_trigger_text=text(spec.entry_trigger), entry_price_text=text(observation.best_ask),
            entry_at=utc(observation.observed_at), generation=observation.generation, sequence=observation.sequence,
            entry_source=observation.source, gross_shares_text=text(amounts.gross_shares), buy_fee_text=text(amounts.buy_fee),
            buy_fee_shares_text=text(amounts.buy_fee_shares), net_shares_text=text(amounts.net_shares),
            entry_notional_text=text(amounts.notional), cash_debit_text=text(amounts.cash_debit),
            tp_text=text(spec.take_profit) if spec.take_profit is not None else None,
            sl_text=text(spec.stop_loss) if spec.stop_loss is not None else None,
            entry_provenance_json=dump(provenance), fee_evidence_json=dump(fee_evidence), result="OPEN",
        )
        conn.execute(f"INSERT INTO demo_v2_positions ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))
        conn.execute("""INSERT INTO demo_v2_entry_counts VALUES (?,?,1)
            ON CONFLICT(rule_id,instance_id) DO UPDATE SET count=count+1""", (rule_id, observation.instance_id))
        self.save_runtime(conn, rule_id, version_id, observation, observation.best_ask, "ENTRY")
        self._ledger(conn, position_id, rule_id, "ENTRY", -amounts.cash_debit, utc(observation.observed_at), provenance)
        return dict(conn.execute("SELECT * FROM demo_v2_positions WHERE id=?", (position_id,)).fetchone())

    def _ledger(self, conn, position_id, rule_id, kind, amount, timestamp, provenance):
        conn.execute("INSERT INTO demo_v2_ledger VALUES (?,?,?,?,?,?,?,?)", (
            identifier(), timestamp, rule_id, position_id, kind, text(amount), f"{position_id}:{kind}", dump(provenance),
        ))

    def close_position(self, position_id: str, *, price: Decimal, sell_fee: Decimal, reason: ExitReason,
                       occurred_at: datetime, provenance: dict, conn=None) -> dict:
        if conn is None:
            with self.database.transaction() as connection:
                return self.close_position(position_id, price=price, sell_fee=sell_fee, reason=reason,
                                           occurred_at=occurred_at, provenance=provenance, conn=connection)
        row = conn.execute("SELECT * FROM demo_v2_positions WHERE id=?", (position_id,)).fetchone()
        if not row:
            raise KeyError("position not found")
        if row["status"] == "CLOSED":
            return dict(row)
        price, sell_fee = decimal(price), decimal(sell_fee)
        if not 0 <= price <= 1 or sell_fee < 0:
            raise ValueError("invalid exit financials")
        gross = decimal(row["net_shares_text"])*price
        if sell_fee > gross:
            raise ValueError("sell fee exceeds proceeds")
        if reason == ExitReason.MARKET_RESOLUTION and (price not in (0, 1) or sell_fee != 0):
            raise ValueError("settlement must use 1/0 with no sell fee")
        net = gross-sell_fee
        pnl = net-decimal(row["cash_debit_text"])
        timestamp = utc(occurred_at)
        conn.execute("""UPDATE demo_v2_positions SET status='CLOSED',exit_at=?,exit_price_text=?,exit_reason=?,
            exit_provenance_json=?,sell_fee_text=?,gross_proceeds_text=?,net_proceeds_text=?,net_pnl_text=?,result=? WHERE id=?""", (
            timestamp, text(price), reason.value, dump(provenance), text(sell_fee), text(gross), text(net), text(pnl), classify(pnl).value, position_id,
        ))
        self._ledger(conn, position_id, row["rule_id"], "SETTLEMENT" if reason == ExitReason.MARKET_RESOLUTION else "EXIT", net, timestamp, provenance)
        return dict(conn.execute("SELECT * FROM demo_v2_positions WHERE id=?", (position_id,)).fetchone())

    def resolve(self, resolution: OfficialResolution, *, reference=None) -> int:
        with self.database.transaction() as conn:
            market = conn.execute("SELECT * FROM demo_v2_instances WHERE id=?", (resolution.instance_id,)).fetchone()
            if not market or resolution.winner_token_id not in (market["up_token_id"], market["down_token_id"]) or not resolution.source:
                raise ValueError("resolution identity/provenance mismatch")
            if market["resolution_status"] == "RESOLVED":
                if market["winner_token_id"] != resolution.winner_token_id:
                    raise ValueError("conflicting official resolution")
                return 0
            reference=reference if reference and reference.valid(instance_model(dict(market)),datetime.now(timezone.utc)) else None
            provenance = {**asdict(resolution),"underlying":reference.evidence() if reference else None}
            conn.execute("UPDATE demo_v2_instances SET status='CLOSED',resolution_status='RESOLVED',winner_token_id=?,resolution_json=? WHERE id=?", (
                resolution.winner_token_id, dump(provenance), resolution.instance_id,
            ))
            positions = conn.execute("SELECT * FROM demo_v2_positions WHERE instance_id=? AND status='OPEN'", (resolution.instance_id,)).fetchall()
            for row in positions:
                self.close_position(row["id"], price=Decimal(int(row["token_id"] == resolution.winner_token_id)),
                    sell_fee=Decimal(0), reason=ExitReason.MARKET_RESOLUTION,
                    occurred_at=resolution.resolved_at, provenance=provenance, conn=conn)
                if reference is not None:
                    point=reference.current
                    conn.execute("INSERT INTO demo_v2_underlying VALUES (?,?,?,?,?,?,?,?,?,?)",(
                        identifier(),resolution.instance_id,row["id"],"EXIT",reference.asset,text(point.price),
                        utc(point.observed_at),point.source,point.reference_id,dump(reference.evidence())))
            return len(positions)

    def positions(self, *, rule_id=None, instance_id=None, status=None) -> list[dict]:
        filters = {"rule_id": rule_id, "instance_id": instance_id, "status": status}
        clauses, params = [], []
        for key, value in filters.items():
            if value is not None:
                clauses.append(f"{key}=?")
                params.append(value)
        query = "SELECT * FROM demo_v2_positions" + (" WHERE "+" AND ".join(clauses) if clauses else "") + " ORDER BY entry_at,id"
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def wallet(self, rule_id: str | None = None) -> Decimal:
        with self.database.connect() as conn:
            query = "SELECT amount_text FROM demo_v2_ledger" + (" WHERE rule_id=?" if rule_id else "")
            return sum((decimal(row[0]) for row in conn.execute(query, (rule_id,) if rule_id else ())), Decimal(0))

    def reset(self, *, factory: bool = False) -> None:
        with self.database.transaction() as conn:
            conn.execute("UPDATE demo_v2_meta SET value='1' WHERE key='resetting'")
            for table in ("ledger", "underlying", "positions", "entry_runtime", "entry_counts"):
                conn.execute(f"DELETE FROM demo_v2_{table}")
            if factory:
                conn.execute("UPDATE demo_v2_rules SET current_version_id=NULL")
                conn.execute("DELETE FROM demo_v2_rule_versions")
                conn.execute("DELETE FROM demo_v2_rules")
            conn.execute("UPDATE demo_v2_meta SET value='0' WHERE key='resetting'")
            conn.execute("UPDATE demo_v2_meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")

    def incident(self, component: str, code: str, details: dict | None = None) -> None:
        with self.database.transaction() as conn:
            conn.execute("INSERT INTO demo_v2_incidents VALUES (?,?,?,?,?)", (identifier(), now_iso(), component, code, dump(details or {})))


def instance_model(row):
    from ..domain.models import Asset, Timeframe, MarketDefinition
    asset, timeframe = row["definition_id"].split("-")
    definition=json.loads(row["metadata_json"]).get("_verified_definition",{})
    return MarketInstance(row["id"],MarketDefinition(Asset(asset),Timeframe(timeframe),**definition),row["condition_id"],
        row["up_token_id"],row["down_token_id"],datetime.fromisoformat(row["starts_at"]),datetime.fromisoformat(row["ends_at"]),
        external_id=row["external_id"],slug=row["slug"],discovered_at=datetime.fromisoformat(row["discovered_at"]),
        metadata_source=row["metadata_source"],verified=bool(row["verified"]),status=row["status"],
        resolution_status=row["resolution_status"],winner_token_id=row["winner_token_id"],resolution_provenance=row["resolution_json"])
