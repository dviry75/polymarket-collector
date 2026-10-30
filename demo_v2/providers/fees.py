"""Applicable evidence and exact simulation arithmetic. No remembered fee rate."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from ..domain.trading import EntryAmounts, FeeState, decimal
from ..domain.models import require_aware


@dataclass(frozen=True)
class FeeEvidence:
    instance_id: str
    state: FeeState
    observed_at: datetime
    source: str
    rate: Decimal | None = None
    exponent: int | None = None
    buy_units: str = "UNKNOWN"
    sell_units: str = "UNKNOWN"
    rounding: str = "UNKNOWN"
    quantum: Decimal = Decimal("0.00001")
    method: str = "UNVERIFIED"
    reason: str = ""
    max_age_seconds: int = 300

    def __post_init__(self):
        require_aware(self.observed_at)
        if not self.instance_id or not self.source or self.max_age_seconds<=0:
            raise ValueError("fee identity, source and freshness bound are required")
        if self.rate is not None and (not isinstance(self.rate,Decimal) or not self.rate.is_finite() or not 0<=self.rate<=1):
            raise ValueError("fee rate must be a finite Decimal fraction")
        if not isinstance(self.quantum,Decimal) or not self.quantum.is_finite() or self.quantum<=0:
            raise ValueError("fee quantum must be a positive Decimal")

    def current(self, now: datetime):
        age = (now-self.observed_at).total_seconds()
        if self.state == FeeState.VALID and not 0 <= age <= self.max_age_seconds:
            return replace(self, state=FeeState.STALE, reason="FEE_EVIDENCE_STALE")
        return self

    def evidence(self):
        return asdict(self)

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        if self.state != FeeState.VALID or self.rate is None:
            raise ValueError("fee evidence is not valid")
        if self.rate == 0:
            return Decimal(0)
        if (self.exponent != 1 or self.buy_units not in ("SHARES", "CASH")
                or self.sell_units != "CASH" or self.rounding not in ("HALF_UP", "DOWN")):
            raise ValueError("unsupported fee contract")
        if not 0 <= self.rate <= 1 or shares < 0 or not 0 <= price <= 1:
            raise ValueError("invalid fee inputs")
        amount = shares*self.rate*price*(1-price)
        return amount.quantize(self.quantum, rounding=ROUND_HALF_UP if self.rounding == "HALF_UP" else ROUND_DOWN)

    def buy(self, shares: Decimal, price: Decimal) -> EntryAmounts:
        amount = self.fee(shares, price)
        if price <= 0:
            raise ValueError("entry price must be positive")
        fee_shares = amount/price if self.buy_units == "SHARES" else Decimal(0)
        return EntryAmounts(shares, shares*price, amount, fee_shares, shares-fee_shares,
                            shares*price + (amount if self.buy_units == "CASH" else 0))


class GammaFeeProvider:
    """Gamma parameters are authoritative; incomplete current unit policy blocks entries.

    CTF Exchange V2 source establishes cash debit units. Current public docs
    establish formula and five decimals, but not tie/per-fill rounding policy.
    """

    def __init__(self, http, *, clock=lambda: datetime.now(timezone.utc)):
        self.http, self.clock = http, clock

    async def parameters(self, instance):
        observed = self.clock()
        market = await self.http.get("/markets/"+instance.external_id)
        from .discovery import labeled_tokens
        if (str(market.get("id")) != instance.external_id or market.get("conditionId") != instance.condition_id
                or labeled_tokens(market) != (instance.up_token_id, instance.down_token_id)):
            raise ValueError("fee evidence market identity mismatch")
        common = dict(instance_id=instance.instance_id, observed_at=observed, source="GAMMA_PUBLIC")
        if market.get("feesEnabled") is False:
            return FeeEvidence(**common, state=FeeState.VALID, rate=Decimal(0), exponent=1,
                buy_units="CASH", sell_units="CASH", rounding="DOWN", method="EXPLICIT_FEES_DISABLED")
        schedule = market.get("feeSchedule") or {}
        if market.get("feesEnabled") is not True or "rate" not in schedule:
            return FeeEvidence(**common, state=FeeState.UNKNOWN, reason="FEE_PARAMETERS_MISSING")
        try:
            rate = decimal(schedule["rate"])
            exponent = int(schedule["exponent"])
            if not 0 <= rate <= 1 or decimal(schedule["exponent"]) != exponent:
                raise ValueError("bad schedule")
        except (ValueError, KeyError, TypeError):
            return FeeEvidence(**common, state=FeeState.UNKNOWN, reason="INVALID_FEE_SCHEDULE")
        ctf=market.get("version")=="v1"
        reason=("UNSUPPORTED_FEE_EXPONENT" if exponent!=1 else "CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED"
                if ctf else "MARKET_PROTOCOL_FEE_UNITS_UNVERIFIED")
        return FeeEvidence(**common,state=FeeState.UNSUPPORTED,rate=rate,exponent=exponent,
            buy_units="CASH" if ctf else "UNKNOWN",sell_units="CASH" if ctf else "UNKNOWN",
            method="GAMMA_CTF_V2_CASH_UNITS_CCC0596" if ctf else "GAMMA_SCHEDULE_DOCUMENTED_CURVE",reason=reason)
