"""Explicit Binance research proxy; never represents a Chainlink settlement oracle."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import asyncio

from ..domain.trading import Comparison, Direction, Measurement, decimal
from ..domain.models import require_aware


@dataclass(frozen=True)
class ReferencePoint:
    price: Decimal
    observed_at: datetime
    source: str
    reference_id: str
    reference_type: str

    def __post_init__(self):
        require_aware(self.observed_at)
        if not isinstance(self.price,Decimal) or not self.price.is_finite() or self.price<=0:
            raise ValueError("reference price must be a positive finite Decimal")
        if not all((self.source,self.reference_id,self.reference_type)):
            raise ValueError("reference provenance is required")


@dataclass(frozen=True)
class ReferenceContext:
    instance_id: str
    asset: str
    baseline: ReferencePoint
    current: ReferencePoint
    settlement_reference: str
    is_research_proxy: bool = True

    def valid(self, instance, now, max_age=5):
        return (self.instance_id == instance.instance_id and self.asset == instance.definition.asset.value
            and self.baseline.observed_at == instance.starts_at and self.baseline.price > 0
            and self.current.price > 0 and self.current.observed_at >= self.baseline.observed_at
            and 0 <= (now-self.current.observed_at).total_seconds() <= max_age
            and self.baseline.source == self.current.source
            and self.baseline.reference_id == self.current.reference_id
            and self.baseline.reference_type == self.current.reference_type)

    def values(self):
        absolute = self.current.price-self.baseline.price
        return {"absolute": absolute, "percent": absolute/self.baseline.price*100}

    def evidence(self):
        return {**asdict(self), "movement": self.values()}


def movement_matches(condition, context: ReferenceContext):
    movement = context.values()["percent" if condition.measurement == Measurement.PERCENT else "absolute"]
    if condition.direction == Direction.POSITIVE and movement <= 0:
        return False
    if condition.direction == Direction.NEGATIVE and movement >= 0:
        return False
    value = abs(movement)
    if condition.comparison == Comparison.GREATER_THAN:
        return value > condition.lower
    if condition.comparison == Comparison.LESS_THAN:
        return value < condition.lower
    return condition.lower <= value <= condition.upper


def source_mapping(instance, metadata):
    description = str(metadata.get("description", "")).lower()
    pair = instance.definition.asset.value+"USDT"
    if instance.definition.timeframe.value == "1h" and "binance" in description and "1 hour" in description:
        authority=str(metadata.get("resolutionSource","")).lower()+" "+description
        # HYPE hourly explicitly names a futures candle. The research provider
        # still returns labelled spot data, never an authoritative futures price.
        return pair,"BINANCE_USDM_1H_CANDLE" if "/futures/" in authority else "BINANCE_1H_CANDLE"
    if instance.definition.timeframe.value in ("5m", "15m") and "chainlink" in description and "twap" in description:
        return pair, "CHAINLINK_TWAP_60S"
    return None


class BinanceResearchProvider:
    def __init__(self, http, *, clock=lambda: datetime.now(timezone.utc)):
        self.http, self.clock = http, clock
        self.symbols, self.baselines, self.latest_points = {}, {}, {}
        self._locks,self._latest_fetched={},{}

    async def context(self, instance, metadata):
        mapping = source_mapping(instance, metadata)
        if mapping is None:
            return None
        async with self._locks.setdefault(mapping[0],asyncio.Lock()):
            return await self._context(instance,mapping)

    async def _context(self,instance,mapping):
        symbol, authority = mapping
        if symbol not in self.symbols:
            response = await self.http.get("/api/v3/exchangeInfo", params={"symbol":symbol})
            valid = [s for s in response.get("symbols", []) if s.get("symbol") == symbol and
                s.get("baseAsset") == instance.definition.asset.value and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
            if len(valid) != 1:
                return None
            self.symbols[symbol] = True
        reference_id = "BINANCE_SPOT:"+symbol
        if instance.instance_id not in self.baselines:
            start_ms = int(instance.starts_at.timestamp()*1000)
            candles = await self.http.get("/api/v3/klines", params={"symbol":symbol,
                "interval":instance.definition.timeframe.value,"startTime":start_ms,"limit":1})
            if len(candles) != 1 or int(candles[0][0]) != start_ms:
                return None
            self.baselines[instance.instance_id] = ReferencePoint(decimal(candles[0][1]), instance.starts_at,
                "BINANCE_PUBLIC", reference_id, "SPOT_USDT_RESEARCH")
        now = self.clock()
        point = self.latest_points.get(symbol)
        fetched=self._latest_fetched.get(symbol)
        if point is None or fetched is None or (now-fetched).total_seconds() >= 2:
            trades = await self.http.get("/api/v3/trades", params={"symbol":symbol,"limit":1})
            if len(trades) != 1:
                return None
            point = ReferencePoint(decimal(trades[0]["price"]), datetime.fromtimestamp(int(trades[0]["time"])/1000, timezone.utc),
                "BINANCE_PUBLIC", reference_id, "SPOT_USDT_RESEARCH")
            self.latest_points[symbol] = point
            self._latest_fetched[symbol] = self.clock()
        return ReferenceContext(instance.instance_id, instance.definition.asset.value,
            self.baselines[instance.instance_id], point, authority)
