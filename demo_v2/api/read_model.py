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


def cohort(repository, filters):
    # Financial filtering uses Decimal in Python; no SQLite REAL casts.
    instances={row["id"]:row for row in repository.instances()}
    rows=repository.positions(rule_id=filters.rule_id,instance_id=filters.instance_id,status=filters.status)
    selected=[]
    for row in rows:
        market=instances[row["instance_id"]]
        asset,timeframe=market["definition_id"].split("-")
        at=datetime.fromisoformat(row["entry_at"])
        if ((filters.asset and asset!=filters.asset) or (filters.timeframe and timeframe!=filters.timeframe)
            or (filters.side and row["side"]!=filters.side) or (filters.from_date and at<filters.from_date)
            or (filters.to_date and at>=filters.to_date)):
            continue
        provenance=json.loads(row["entry_provenance_json"])
        underlying=provenance.get("underlying")
        if filters.movement_min is not None or filters.movement_max is not None:
            if not underlying or "movement" not in underlying:
                continue
            movement=decimal(underlying["movement"]["percent" if filters.movement_measurement=="PERCENT" else "absolute"])
            if (filters.movement_min is not None and movement<filters.movement_min) or (filters.movement_max is not None and movement>filters.movement_max):
                continue
        selected.append({**row,"asset":asset,"timeframe":timeframe})
    return list(reversed(selected))


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


def kpi(repository, rows):
    ids={row["id"] for row in rows}
    with repository.database.connect() as conn:
        cash=sum((decimal(row["amount_text"]) for row in conn.execute("SELECT position_id,amount_text FROM demo_v2_ledger") if row["position_id"] in ids),Decimal(0))
    positive=sum(row["result"]=="POSITIVE" for row in rows)
    negative=sum(row["result"]=="NEGATIVE" for row in rows)
    fees=sum((decimal(row["buy_fee_text"])+decimal(row["sell_fee_text"] or "0") for row in rows),Decimal(0))
    return dict(wallet=text(cash),positive=positive,negative=negative,total_trades=len(rows),
        open=sum(row["status"]=="OPEN" for row in rows),break_even=sum(row["result"]=="BREAK_EVEN" for row in rows),
        success_rate=text(Decimal(positive)*100/(positive+negative)) if positive+negative else None,
        total_fees=text(fees))
