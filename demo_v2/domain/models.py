from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Asset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"
    DOGE = "DOGE"
    HYPE = "HYPE"
    BNB = "BNB"


class Timeframe(StrEnum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"

    @property
    def seconds(self) -> int:
        return {self.M5: 300, self.M15: 900, self.H1: 3600}[self]


def require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")


@dataclass(frozen=True)
class MarketDefinition:
    asset: Asset
    timeframe: Timeframe


def market_definitions() -> tuple[MarketDefinition, ...]:
    """Product catalog only; does not discover live instances or infer slugs."""
    return tuple(MarketDefinition(asset, timeframe) for asset in Asset for timeframe in Timeframe)


@dataclass(frozen=True)
class MarketInstance:
    instance_id: str
    definition: MarketDefinition
    condition_id: str
    up_token_id: str
    down_token_id: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.starts_at)
        require_aware(self.ends_at)
        if self.ends_at <= self.starts_at:
            raise ValueError("instance end must follow its start")
        if not all((self.instance_id, self.condition_id, self.up_token_id, self.down_token_id)):
            raise ValueError("instance and token identities are required")
        if self.up_token_id == self.down_token_id:
            raise ValueError("UP and DOWN tokens must differ")


@dataclass(frozen=True)
class MarketObservation:
    """One ordered token observation, never reconstructed from telemetry.

    sequence is a local observation ordinal per connection generation, assigned
    by a future provider *after* expanding a frame's ordered top transitions.
    It is not an exchange timestamp or an assumed exchange sequence number.
    """

    instance_id: str
    token_id: str
    generation: int
    sequence: int
    observed_at: datetime
    received_at: datetime
    source: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    book_ready: bool = True

    def __post_init__(self) -> None:
        require_aware(self.observed_at)
        require_aware(self.received_at)
        if not all((self.instance_id, self.token_id, self.source)):
            raise ValueError("observation identity and provenance are required")
        if self.generation < 1 or self.sequence < 1:
            raise ValueError("generation and sequence must be positive")
        for price in (self.best_bid, self.best_ask):
            if price is not None and (
                not isinstance(price, Decimal) or not price.is_finite() or not 0 <= price <= 1
            ):
                raise ValueError("prices must be finite Decimal probabilities")


@dataclass(frozen=True)
class OfficialResolution:
    instance_id: str
    winner_token_id: str
    resolved_at: datetime
    source: str


@dataclass(frozen=True)
class FeeParameters:
    """Applicable evidence only; no default rate or fee computation here."""

    instance_id: str
    fees_enabled: bool
    parameters: tuple[tuple[str, str], ...]
    observed_at: datetime
    source: str
    version: str


@dataclass(frozen=True)
class ReferencePrice:
    asset: Asset
    price: Decimal
    observed_at: datetime
    source: str
    reference_type: str


@dataclass(frozen=True)
class Principal:
    subject: str
