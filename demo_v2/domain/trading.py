"""Validated immutable rule specifications and decimal financial values."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import market_definitions


def decimal(value) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError("financial values must be decimal strings or integers")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("financial values must be finite")
    return result


def text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


class Side(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class FeeState(StrEnum):
    VALID = "VALID"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"


class ExitReason(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    MARKET_RESOLUTION = "MARKET_RESOLUTION"


class Result(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    BREAK_EVEN = "BREAK_EVEN"
    OPEN = "OPEN"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WeeklyRange(FrozenModel):
    weekday: Annotated[int, Field(ge=0, le=6)]
    start: time
    end: time

    @model_validator(mode="after")
    def validate_range(self):
        if self.start == self.end or self.start.tzinfo or self.end.tzinfo:
            raise ValueError("weekly ranges need distinct local start/end times")
        return self


class EntryWindow(FrozenModel):
    start_seconds: Annotated[int, Field(ge=0)]
    end_seconds: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("window end must follow start")
        return self


class Measurement(StrEnum):
    PERCENT = "PERCENT"
    ABSOLUTE = "ABSOLUTE"


class Comparison(StrEnum):
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN = "LESS_THAN"
    BETWEEN = "BETWEEN"


class Direction(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    ABSOLUTE = "ABSOLUTE"


class MovementFilter(FrozenModel):
    measurement: Measurement
    comparison: Comparison
    direction: Direction
    lower: Decimal
    upper: Decimal | None = None

    @field_validator("lower", "upper", mode="before")
    @classmethod
    def valid_decimal(cls, value):
        return None if value is None else decimal(value)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.lower < 0 or (self.upper is not None and self.upper < 0):
            raise ValueError("direction is separate from nonnegative movement magnitude")
        if self.comparison == Comparison.BETWEEN and (self.upper is None or self.upper < self.lower):
            raise ValueError("BETWEEN requires upper >= lower")
        return self


class RuleSpec(FrozenModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    markets: Annotated[tuple[str, ...], Field(min_length=1)]
    entry_trigger: Decimal
    gross_shares: Decimal = Decimal("5")
    entry_frequency: Annotated[int, Field(ge=1, strict=True)] = 1
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    weekly_schedule: tuple[WeeklyRange, ...] = ()
    entry_windows: tuple[EntryWindow, ...] = ()
    underlying_filters: tuple[MovementFilter, ...] = ()
    enabled: bool = True
    archived: bool = False

    @field_validator("entry_trigger", "gross_shares", "take_profit", "stop_loss", mode="before")
    @classmethod
    def valid_decimal(cls, value):
        return None if value is None else decimal(value)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_rule(self):
        catalog = {item.key for item in market_definitions()}
        if len(set(self.markets)) != len(self.markets) or set(self.markets) - catalog:
            raise ValueError("selected markets must be distinct supported definitions")
        if not 0 < self.entry_trigger < 1 or self.gross_shares < 5:
            raise ValueError("entry price must be inside (0,1), minimum gross shares is 5")
        if self.take_profit is not None and not self.entry_trigger < self.take_profit <= 1:
            raise ValueError("TP must be above entry and <= 1")
        if self.stop_loss is not None and not 0 <= self.stop_loss < self.entry_trigger:
            raise ValueError("SL must be below entry and >= 0")
        if self.archived and self.enabled:
            raise ValueError("archived rules cannot be enabled")
        return self


@dataclass(frozen=True)
class EntryAmounts:
    gross_shares: Decimal
    notional: Decimal
    buy_fee: Decimal
    buy_fee_shares: Decimal
    net_shares: Decimal
    cash_debit: Decimal

    def __post_init__(self):
        for value in self.__dict__.values():
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("entry amounts must be finite nonnegative Decimals")
        if self.net_shares <= 0 or self.net_shares + self.buy_fee_shares != self.gross_shares:
            raise ValueError("gross/net share accounting does not reconcile")
        if self.cash_debit not in (self.notional, self.notional+self.buy_fee):
            raise ValueError("cash debit does not reconcile")
        if self.buy_fee_shares > 0 and self.cash_debit != self.notional:
            raise ValueError("share-deducted buy fees must not also debit cash")


def classify(pnl: Decimal) -> Result:
    return Result.POSITIVE if pnl > 0 else Result.NEGATIVE if pnl < 0 else Result.BREAK_EVEN
