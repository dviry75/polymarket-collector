from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Iterable

from .order_book import canonical_decimal, decimal_value


MONEY_QUANTUM = Decimal("0.000001")
SHARE_QUANTUM = Decimal("0.000001")


class EventState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    SKIPPED_SIMULTANEOUS_TRIGGER = "SKIPPED_SIMULTANEOUS_TRIGGER"
    ENTRY_INTENT_RESERVED = "ENTRY_INTENT_RESERVED"
    ENTRY_UNKNOWN = "ENTRY_UNKNOWN"
    ENTRY_ZERO_FILL = "ENTRY_ZERO_FILL"
    POSITION_OPEN = "POSITION_OPEN"
    EXITING = "EXITING"
    DUST = "DUST"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class StrategyPolicy:
    entry_price: Decimal = Decimal("0.74")
    entry_max_price: Decimal = Decimal("0.76")
    take_profit_price: Decimal = Decimal("0.96")
    stop_price: Decimal = Decimal("0.66")
    emergency_price: Decimal = Decimal("0.60")
    stop_min_price: Decimal = Decimal("0.55")
    emergency_min_price: Decimal = Decimal("0.01")
    max_spend: Decimal = Decimal("5.00")
    max_exposure: Decimal = Decimal("5.00")
    entry_window_seconds: int = 120

    def validate(self) -> None:
        expected = StrategyPolicy()
        if self != expected:
            raise ValueError("strategy policy values are immutable in this build")


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str
    side: str | None = None
    token_id: str | None = None
    simultaneous: bool = False


@dataclass(frozen=True)
class FillEstimate:
    requested: Decimal
    filled_shares: Decimal
    average_price: Decimal
    notional: Decimal
    fee: Decimal
    all_in: Decimal
    remaining_request: Decimal


class AllInBudget:
    """Conservative preview; the SDK max_spend field is the final hard cap."""

    def __init__(self, max_spend: Decimal = Decimal("5.00")):
        self.max_spend = max_spend.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)

    def sdk_buy_parameters(self) -> dict[str, str]:
        # Unified SDK 0.1.0b21 documents max_spend as amount including fees.
        amount = self.max_spend.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
        return {"amount": canonical_decimal(amount), "max_spend": canonical_decimal(amount)}

    def conservative_notional(self, maximum_fee_fraction: Decimal) -> Decimal:
        if maximum_fee_fraction < 0:
            raise ValueError("negative fee")
        return (self.max_spend / (Decimal("1") + maximum_fee_fraction)).quantize(
            MONEY_QUANTUM, rounding=ROUND_DOWN
        )

    def minimum_viable(
        self,
        *,
        min_order_shares: Decimal,
        maximum_price: Decimal,
        maximum_fee_fraction: Decimal,
    ) -> tuple[bool, str]:
        if min_order_shares <= 0 or maximum_price <= 0:
            return False, "INVALID_MARKET_CONSTRAINTS"
        notional = self.conservative_notional(maximum_fee_fraction)
        shares = (notional / maximum_price).quantize(SHARE_QUANTUM, rounding=ROUND_DOWN)
        if shares < min_order_shares:
            return False, "MINIMUM_ORDER_EXCEEDS_5_ALL_IN"
        return True, "VIABLE"


def exact_trigger(observed: Any, expected: Decimal) -> bool:
    value = decimal_value(observed)
    return value is not None and value == expected


def event_end_from_id(event_id: str) -> datetime | None:
    try:
        start = int(str(event_id).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None
    return datetime.fromtimestamp(start + 300, tz=timezone.utc)


def remaining_seconds(event_id: str, observed_at: datetime) -> Decimal | None:
    end = event_end_from_id(event_id)
    if end is None:
        return None
    return Decimal(str((end - observed_at.astimezone(timezone.utc)).total_seconds()))


def entry_window_reason(
    event_id: str, observed_at: datetime, window_seconds: int = 120
) -> str | None:
    remaining = remaining_seconds(event_id, observed_at)
    if remaining is None:
        return "MISSING_EVENT_END_TIME"
    if remaining <= 0:
        return "EVENT_ENDED"
    if remaining > Decimal(window_seconds):
        return "BEFORE_ENTRY_WINDOW"
    return None


def choose_entry(
    *,
    updates: Iterable[dict[str, Any]],
    yes_token_id: str,
    no_token_id: str,
    event_ready: bool,
    paused: bool,
    event_locked: bool,
    active_exposure: Decimal,
    observed_at: datetime,
    event_id: str,
    policy: StrategyPolicy = StrategyPolicy(),
) -> EntryDecision:
    update_list = list(updates)
    exact_assets = [
        str(update.get("asset_id"))
        for update in update_list
        if exact_trigger(update.get("best_ask"), policy.entry_price)
    ]
    if event_locked:
        return EntryDecision(False, "EVENT_LOCKED")
    if paused:
        return EntryDecision(False, "PAUSE_ENTRIES")
    if not event_ready:
        return EntryDecision(False, "MARKET_DATA_NOT_READY")
    if yes_token_id in exact_assets and no_token_id in exact_assets:
        return EntryDecision(
            False, "SKIPPED_SIMULTANEOUS_TRIGGER", simultaneous=True
        )
    window = entry_window_reason(event_id, observed_at, policy.entry_window_seconds)
    if window:
        return EntryDecision(False, window)
    if active_exposure >= policy.max_exposure:
        return EntryDecision(False, "EXPOSURE_CAP")
    if not exact_assets:
        return EntryDecision(False, "ENTRY_PRICE_NOT_EXACT")
    first = exact_assets[0]
    if first == yes_token_id:
        return EntryDecision(True, "ENTRY_PRICE_EXACT", "YES", yes_token_id)
    if first == no_token_id:
        return EntryDecision(True, "ENTRY_PRICE_EXACT", "NO", no_token_id)
    return EntryDecision(False, "TOKEN_EVENT_MISMATCH")


def fee_amount(
    shares: Decimal, price: Decimal, fee_rate: Decimal
) -> Decimal:
    if shares <= 0 or price <= 0 or fee_rate <= 0:
        return Decimal("0")
    return (shares * price * (Decimal("1") - price) * fee_rate).quantize(
        MONEY_QUANTUM, rounding=ROUND_DOWN
    )


def simulate_buy_fak(
    asks: Iterable[dict[str, Any]],
    *,
    max_price: Decimal,
    max_spend: Decimal,
    fee_rate: Decimal,
) -> FillEstimate:
    remaining_budget = max_spend
    shares = Decimal("0")
    notional = Decimal("0")
    fees = Decimal("0")
    for raw in sorted(
        asks,
        key=lambda level: decimal_value(level.get("price")) or Decimal("Infinity"),
    ):
        price = decimal_value(raw.get("price"))
        available = decimal_value(raw.get("size"))
        if price is None or available is None or available <= 0 or price > max_price:
            continue
        fee_per_share = price * (Decimal("1") - price) * max(fee_rate, Decimal("0"))
        all_in_per_share = price + fee_per_share
        affordable = (remaining_budget / all_in_per_share).quantize(
            SHARE_QUANTUM, rounding=ROUND_DOWN
        )
        fill = min(available, affordable)
        if fill <= 0:
            continue
        level_notional = fill * price
        level_fee = fee_amount(fill, price, fee_rate)
        level_all_in = level_notional + level_fee
        if level_all_in > remaining_budget:
            continue
        shares += fill
        notional += level_notional
        fees += level_fee
        remaining_budget -= level_all_in
    average = notional / shares if shares else Decimal("0")
    all_in = notional + fees
    if all_in > max_spend:
        raise AssertionError("paper fill exceeded all-in cap")
    return FillEstimate(
        requested=max_spend,
        filled_shares=shares,
        average_price=average,
        notional=notional,
        fee=fees,
        all_in=all_in,
        remaining_request=max_spend - all_in,
    )


def simulate_sell_fak(
    bids: Iterable[dict[str, Any]],
    *,
    shares: Decimal,
    min_price: Decimal,
    fee_rate: Decimal,
) -> FillEstimate:
    remaining = shares
    filled = Decimal("0")
    notional = Decimal("0")
    fees = Decimal("0")
    for raw in sorted(
        bids,
        key=lambda level: decimal_value(level.get("price")) or Decimal("-1"),
        reverse=True,
    ):
        price = decimal_value(raw.get("price"))
        available = decimal_value(raw.get("size"))
        if (
            price is None or available is None or available <= 0
            or price < min_price or remaining <= 0
        ):
            continue
        level_fill = min(remaining, available)
        filled += level_fill
        remaining -= level_fill
        notional += level_fill * price
        fees += fee_amount(level_fill, price, fee_rate)
    average = notional / filled if filled else Decimal("0")
    return FillEstimate(
        requested=shares,
        filled_shares=filled,
        average_price=average,
        notional=notional,
        fee=fees,
        all_in=notional - fees,
        remaining_request=remaining,
    )
