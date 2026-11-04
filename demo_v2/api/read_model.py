"""One cohort predicate for trades, fees, success and scoped ledger cash flow."""

from datetime import datetime
from decimal import Decimal
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator, field_validator

from ..domain.models import Asset, Timeframe
from ..domain.trading import FrozenModel, Side, decimal, text


class TradeFilter(FrozenModel):
    rule_id: str | None = None
    instance_id: str | None = None
    asset: Asset | None = None
    timeframe: Timeframe | None = None
    side: Side | None = None
    status: Literal["OPEN","CLOSED"] | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    movement_measurement: Literal["PERCENT","ABSOLUTE"] = "PERCENT"
    movement_min: Decimal | None = None
    movement_max: Decimal | None = None

    @field_validator("movement_min","movement_max",mode="before")
    @classmethod
    def decimals(cls,value):
        return None if value is None else decimal(value)

    @model_validator(mode="after")
    def bounds(self):
        for date in (self.from_date,self.to_date):
            if date and (date.tzinfo is None or date.utcoffset() is None):
                raise ValueError("date filters require timezone")
        if self.from_date and self.to_date and self.from_date>=self.to_date:
            raise ValueError("from_date must precede exclusive to_date")
        if self.movement_min is not None and self.movement_max is not None and self.movement_min>self.movement_max:
            raise ValueError("movement bounds reversed")
        return self


class TradeQuery(TradeFilter):
    limit: Annotated[int,Field(ge=1,le=200)] = 50
    offset: Annotated[int,Field(ge=0)] = 0


def _selection(conn, filters):
    """One parameterized cohort predicate, without loading market metadata.

    SQLite handles selection/count/pagination. Python functions preserve exact
    Decimal movement comparisons and timezone-aware date boundaries; REAL casts
    and lexical comparisons of timestamps with different offsets are unsafe.
    """
    clauses, params = [], []
    for key in ("rule_id", "instance_id", "side", "status"):
        value = getattr(filters, key)
        if value is not None:
            clauses.append(f"p.{key}=?")
            params.append(value)
    for key, expression in (("asset", "substr(i.definition_id,1,instr(i.definition_id,'-')-1)"),
                            ("timeframe", "substr(i.definition_id,instr(i.definition_id,'-')+1)")):
        value = getattr(filters, key)
        if value is not None:
            clauses.append(expression + "=?")
            params.append(value)
    if filters.from_date is not None or filters.to_date is not None:
        def in_interval(value):
            at = datetime.fromisoformat(value)
            return ((filters.from_date is None or at >= filters.from_date)
                    and (filters.to_date is None or at < filters.to_date))
        conn.create_function("demo_entry_interval", 1, in_interval, deterministic=True)
        clauses.append("demo_entry_interval(p.entry_at)")
    if filters.movement_min is not None or filters.movement_max is not None:
        def in_movement(value):
            underlying = json.loads(value).get("underlying")
            if not underlying or "movement" not in underlying:
                return False
            movement = decimal(underlying["movement"][
                "percent" if filters.movement_measurement == "PERCENT" else "absolute"])
            return ((filters.movement_min is None or movement >= filters.movement_min)
                    and (filters.movement_max is None or movement <= filters.movement_max))
        conn.create_function("demo_entry_movement", 1, in_movement, deterministic=True)
        clauses.append("demo_entry_movement(p.entry_provenance_json)")
    source = " FROM demo_v2_positions p"
    if filters.asset is not None or filters.timeframe is not None:
        source += " JOIN demo_v2_instances i ON i.id=p.instance_id"
    return source, (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def trade_page(repository, filters):
    """Load wide evidence only for the requested page, with a consistent total."""
    with repository.database.connect() as conn:
        conn.execute("BEGIN")  # Short read snapshot; does not take a writer lock.
        source, where, params = _selection(conn, filters)
        total = conn.execute("SELECT count(*)" + source + where, params).fetchone()[0]
        # Materialize only IDs/time/definition before loading wide position JSON.
        # Otherwise an unindexed sort can hold the entire history's evidence.
        query = ("WITH page AS MATERIALIZED (SELECT p.id,p.entry_at" + source + where
                 + " ORDER BY p.entry_at DESC,p.id DESC LIMIT ? OFFSET ?) "
                 "SELECT p.*,CAST(m.value AS INTEGER) AS serial_number,i.definition_id "
                 "FROM page JOIN demo_v2_positions p ON p.id=page.id "
                 "JOIN demo_v2_instances i ON i.id=p.instance_id "
                 "LEFT JOIN demo_v2_meta m ON m.key='trade_serial:'||p.id "
                 "ORDER BY page.entry_at DESC,page.id DESC")
        items = []
        for record in conn.execute(query, [*params, filters.limit, filters.offset]):
            row = dict(record)
            row["asset"], row["timeframe"] = row.pop("definition_id").split("-")
            items.append(trade_view(row))
    return dict(items=items, total=total, limit=filters.limit, offset=filters.offset)


def trade_view(row):
    result={}
    for key,value in row.items():
        if key.endswith("_text"):
            result[key[:-5]]=value
        elif key.endswith("_json"):
            result[key[:-5]]=json.loads(value) if value else None
        else:
            result[key]=value
    result["total_fees"]=text(decimal(row["buy_fee_text"])+decimal(row["sell_fee_text"] or "0"))
    entry=result["entry_provenance"].get("underlying")
    exit=(result.get("exit_provenance") or {}).get("underlying")
    during=None
    if entry and exit and entry["current"]["reference_id"]==exit["current"]["reference_id"]:
        first,last=decimal(entry["current"]["price"]),decimal(exit["current"]["price"])
        during={"absolute":text(last-first),"percent":text((last-first)/first*100)}
    result["underlying_movements"]={"open_to_entry":entry.get("movement") if entry else None,
        "open_to_exit":exit.get("movement") if exit else None,"during_trade":during}
    return result


def kpi(repository, filters):
    # Stream narrow financial columns; never materialize history, provenance or
    # an unbounded ID set. All financial arithmetic remains exact Decimal.
    positive = negative = opened = break_even = total = 0
    fees = cash = Decimal(0)
    with repository.database.connect() as conn:
        conn.execute("BEGIN")
        source, where, params = _selection(conn, filters)
        for row in conn.execute("SELECT p.result,p.status,p.buy_fee_text,p.sell_fee_text" + source + where, params):
            total += 1
            positive += row["result"] == "POSITIVE"
            negative += row["result"] == "NEGATIVE"
            opened += row["status"] == "OPEN"
            break_even += row["result"] == "BREAK_EVEN"
            fees += decimal(row["buy_fee_text"]) + decimal(row["sell_fee_text"] or "0")
        ledger_source = source + " JOIN demo_v2_ledger l ON l.position_id=p.id"
        cash = sum((decimal(row[0]) for row in conn.execute(
            "SELECT l.amount_text" + ledger_source + where, params)), Decimal(0))
    return dict(wallet=text(cash),positive=positive,negative=negative,total_trades=total,
        open=opened,break_even=break_even,
        success_rate=text(Decimal(positive)*100/(positive+negative)) if positive+negative else None,
        total_fees=text(fees))


def rule_summary(repository, rule_id):
    """Lifetime rule totals without reading trade/market evidence."""
    count, realized = 0, Decimal(0)
    with repository.database.connect() as conn:
        conn.execute("BEGIN")
        for row in conn.execute("SELECT status,net_pnl_text FROM demo_v2_positions WHERE rule_id=?", (rule_id,)):
            count += 1
            if row["status"] == "CLOSED":
                realized += decimal(row["net_pnl_text"])
        cash = sum((decimal(row[0]) for row in conn.execute(
            "SELECT amount_text FROM demo_v2_ledger WHERE rule_id=?", (rule_id,))), Decimal(0))
    return dict(trade_count=count, wallet=str(cash), net_pnl=text(realized))
