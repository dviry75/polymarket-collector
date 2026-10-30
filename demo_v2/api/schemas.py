"""Public response contracts. Prices, shares, money and percentages are strings."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from ..domain.trading import RuleSpec


class ExtensibleView(BaseModel):
    model_config=ConfigDict(extra="allow")


class SessionView(BaseModel):
    user: str
    csrf_token: str = Field(description="Send as X-Demo-CSRF on every authenticated mutation")


class RuleView(ExtensibleView):
    id: str
    current_version_id: str
    version: int
    spec: RuleSpec
    created_at: datetime
    updated_at: datetime
    trade_count: int
    wallet: str
    net_pnl: str = Field(description="Realized net P&L over all closed positions of this rule; open positions excluded")


class RuleList(BaseModel):
    items: list[RuleView]


class TradeView(ExtensibleView):
    id: str
    rule_id: str
    rule_version_id: str
    instance_id: str
    token_id: str
    side: Literal["UP","DOWN"]
    status: Literal["OPEN","CLOSED"]
    result: Literal["OPEN","POSITIVE","NEGATIVE","BREAK_EVEN"]
    entry_at: datetime
    entry_price: str
    gross_shares: str
    net_shares: str
    buy_fee: str
    total_fees: str
    cash_debit: str
    exit_at: datetime | None
    exit_price: str | None
    exit_reason: Literal["TAKE_PROFIT","STOP_LOSS","MARKET_RESOLUTION"] | None
    sell_fee: str | None
    net_proceeds: str | None
    net_pnl: str | None
    entry_provenance: dict
    fee_evidence: dict


class TradePage(BaseModel):
    items: list[TradeView]
    total: int
    limit: int
    offset: int


class TradeDetail(TradeView):
    rule_snapshot: RuleSpec
    market: dict
    underlying_observations: list[dict]
    ledger: list[dict]


class KPIView(BaseModel):
    wallet: str = Field(description="Signed ledger cash flow for the same entry cohort as trades")
    positive: int
    negative: int
    total_trades: int
    open: int
    break_even: int
    success_rate: str | None = Field(description="Percent, positive/(positive+negative)*100; null when no eligible closed trades")
    total_fees: str


class MarketList(BaseModel):
    definitions: list[dict]
    instances: list[dict]
    as_of: datetime


class HealthView(ExtensibleView):
    state: Literal["READY","DEGRADED","NOT_READY"]
    components: list[dict]
    entries_allowed: bool
    blocker_codes: list[str]
    discovery_at: datetime | None
    engine_generation: int
    engine_reason: str
    workers: dict[str,str]
