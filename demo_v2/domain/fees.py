"""Deliberate DEMO assumption, independent of Polymarket fee contracts."""

from dataclasses import dataclass
from decimal import Decimal

from .trading import EntryAmounts, decimal


DEFAULT_FLAT_FEE = Decimal("0.068")
FEE_METHOD = "EMPIRICAL_FLAT_AVERAGE"
VIRTUAL_EXECUTION = "OBSERVED_TOP_OF_BOOK_SINGLE_EXECUTION"


@dataclass(frozen=True)
class FlatFeePolicy:
    per_action: Decimal = DEFAULT_FLAT_FEE

    def __post_init__(self):
        value = decimal(self.per_action)
        if value < 0:
            raise ValueError("DEMO_FLAT_FEE_PER_ACTION must be finite and nonnegative")
        object.__setattr__(self, "per_action", value)

    def buy(self, shares: Decimal, price: Decimal) -> EntryAmounts:
        shares, price = decimal(shares), decimal(price)
        if shares <= 0 or not 0 <= price <= 1:
            raise ValueError("invalid simulated execution")
        notional = shares * price
        return EntryAmounts(shares, notional, self.per_action, Decimal(0), shares,
                            notional + self.per_action)

    def evidence(self) -> dict:
        # VALID describes this simulation policy, never exact exchange fees.
        return {"state": "VALID", "method": FEE_METHOD, "source": "DEMO_SIMULATION_ASSUMPTION",
                "policy_version": 1, "per_action": str(self.per_action),
                "buy_units": "CASH", "sell_units": "CASH",
                "aggregation": "ONCE_PER_VIRTUAL_EXECUTION", "settlement_fee": "0",
                "historical_operations": 1644, "historical_total_fees": "111.81",
                "historical_rounded_average": str(DEFAULT_FLAT_FEE),
                "replicates_exact_polymarket_fees": False}
