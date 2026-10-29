from dataclasses import dataclass
from typing import AsyncIterator, Protocol, Sequence

from ..domain.models import (
    Asset, FeeParameters, MarketDefinition, MarketInstance, MarketObservation,
    OfficialResolution, Principal, ReferencePrice,
)
from .fees import FeeEvidence
from .underlying import ReferenceContext


class MarketDiscovery(Protocol):
    async def discover(self, definitions: Sequence[MarketDefinition]) -> Sequence[MarketInstance]: ...


class MarketData(Protocol):
    async def set_subscriptions(self, instances: Sequence[MarketInstance]) -> None:
        """Desired union of entry-eligible markets and all monitored positions."""
        ...

    def observations(self) -> AsyncIterator[MarketObservation]:
        """Ordered fresh observations, independent of telemetry persistence.

        Reconnect starts a new generation; never replay missed trade events.
        A validated fresh first snapshot may be emitted immediately.
        """
        ...

    async def close(self) -> None: ...

    async def run(self) -> None: ...

    def token_ready(self, token_id: str) -> bool: ...

    async def resync(self, reason: str) -> None: ...


class MarketResolution(Protocol):
    async def resolve(self, instance: MarketInstance) -> OfficialResolution | None:
        """Return verified official evidence for the exact instance, or None."""
        ...


class FeeProvider(Protocol):
    async def parameters(self, instance: MarketInstance) -> FeeEvidence | None:
        """None means unavailable/unreliable and must block affected entries."""
        ...


class UnderlyingPriceProvider(Protocol):
    async def context(self, instance: MarketInstance, metadata: dict) -> ReferenceContext | None: ...


class CredentialValidator(Protocol):
    async def validate(self, username: str, password: str) -> Principal | None:
        """Use only DEMO-owned credential config; no LIVE state/session access."""
        ...


@dataclass(frozen=True)
class Providers:
    discovery: MarketDiscovery | None = None
    market_data: MarketData | None = None
    resolution: MarketResolution | None = None
    fees: FeeProvider | None = None
    underlying: UnderlyingPriceProvider | None = None
    credentials: CredentialValidator | None = None
