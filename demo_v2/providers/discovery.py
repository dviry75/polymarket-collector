"""Gamma-verified recurring families. Candidate names are never identity proof."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from dataclasses import replace
from urllib.parse import quote

from ..domain.models import MarketDefinition, MarketInstance, OfficialResolution
from ..domain.trading import decimal


def timestamp(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public metadata timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def array(value) -> list:
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("expected metadata array")
    return result


def candidate_family(definition: MarketDefinition) -> str:
    suffix = "hourly" if definition.timeframe.value == "1h" else definition.timeframe.value
    # Gamma's verified hourly SOL series uses the full asset name (series 10122).
    asset="solana" if definition.asset.value=="SOL" and suffix=="hourly" else definition.asset.value.lower()
    return f"{asset}-up-or-down-{suffix}"


def labeled_tokens(market: dict) -> tuple[str, str]:
    if market.get("version") not in (None,"v1"):
        # Protocol-v2 Position IDs are outside this CTF integration; never select
        # legacy token IDs merely because both fields happen to be populated.
        raise ValueError("unsupported public market protocol")
    labels, tokens = array(market.get("outcomes")), array(market.get("clobTokenIds"))
    if len(labels) != 2 or len(tokens) != 2 or len(set(map(str, tokens))) != 2:
        raise ValueError("ambiguous binary outcomes/tokens")
    mapping = {str(label).strip().lower():str(token) for label, token in zip(labels, tokens, strict=True)}
    if set(mapping) != {"up", "down"} or not all(token.isdigit() and int(token)>0 for token in mapping.values()):
        raise ValueError("explicit UP/DOWN labels and unique positive token IDs required")
    return mapping["up"], mapping["down"]


def parse_instance(event: dict, definition: MarketDefinition, series_id: str, now: datetime) -> tuple[MarketInstance, dict]:
    series = event.get("series") or []
    if not any(str(s.get("id")) == str(series_id) and s.get("slug") == candidate_family(definition) for s in series):
        raise ValueError("event does not belong to verified recurring family")
    candidates = []
    for market in event.get("markets") or []:
        try:
            up, down = labeled_tokens(market)
            start, end = timestamp(market["eventStartTime"]), timestamp(market["endDate"])
            if (end-start).total_seconds() != definition.expected_duration:
                continue
            condition = str(market.get("conditionId") or "")
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition) or not str(market.get("id") or ""):
                continue
            if "up or down" not in str(market.get("question") or event.get("title") or "").lower():
                continue
            status = "OPEN" if (market.get("active") is True and market.get("closed") is False
                and market.get("acceptingOrders") is True and market.get("enableOrderBook") is True) else "UNAVAILABLE"
            verified_definition=replace(definition,family_id=str(series_id),provenance="GAMMA_PUBLIC_SERIES")
            instance = MarketInstance(condition, verified_definition, condition, up, down, start, end,
                external_id=str(market["id"]), slug=str(market.get("slug") or event.get("slug") or ""),
                discovered_at=now, metadata_source="GAMMA_PUBLIC", verified=True, status=status)
            candidates.append((instance, market))
        except (ValueError, TypeError, KeyError):
            continue
    if len(candidates) != 1:
        raise ValueError("ambiguous or invalid recurring market instance")
    return candidates[0]


class GammaDiscovery:
    def __init__(self, http, *, clock=lambda: datetime.now(timezone.utc)):
        self.http, self.clock = http, clock
        self.issues: dict[str, str] = {}
        self.metadata: dict[str, dict] = {}
        self._series = {}
        self._series_at = None

    async def discover(self, definitions):
        now = self.clock()
        if self._series_at is None or (now-self._series_at).total_seconds() > 300:
            response = await self.http.get("/series", params=[("slug",candidate_family(d)) for d in definitions]+[("limit","50")])
            if not isinstance(response, list):
                raise ValueError("invalid series response")
            grouped={}
            for row in response:
                grouped.setdefault(str(row.get("slug")),[]).append(row)
            self._series = {slug:rows[0] for slug,rows in grouped.items() if len(rows)==1}
            self._series_at = now
        semaphore = asyncio.Semaphore(3)
        self.issues = {}
        self.metadata = {}
        async def family(definition):
            key = definition.key
            series = self._series.get(candidate_family(definition))
            recurrence = "hourly" if definition.timeframe.value == "1h" else definition.timeframe.value
            if not series or series.get("recurrence") != recurrence:
                self.issues[key] = "FAMILY_NOT_VERIFIED"
                return []
            try:
                async with semaphore:
                    events = await self.http.get("/events", params={
                        "series_id":str(series["id"]), "active":"true", "closed":"false", "limit":12,
                        "end_date_min":now.isoformat(), "end_date_max":(now+timedelta(seconds=2*definition.expected_duration)).isoformat(),
                        "order":"endDate", "ascending":"true",
                    })
                found = []
                for event in events:
                    try:
                        instance, metadata = parse_instance(event, definition, str(series["id"]), now)
                        if instance.ends_at > now:
                            found.append(instance)
                            self.metadata[instance.instance_id] = metadata
                    except (ValueError, TypeError, KeyError):
                        continue
                active = [i for i in found if i.starts_at <= now < i.ends_at and i.status == "OPEN"]
                if len(active) != 1:
                    self.issues[key] = "AMBIGUOUS_CURRENT_INSTANCE" if len(active)>1 else "CURRENT_INSTANCE_UNAVAILABLE"
                    return []
                return found
            except Exception as exc:
                self.issues[key] = f"DISCOVERY_FAILED:{type(exc).__name__}"
                return []
        groups = await asyncio.gather(*(family(d) for d in definitions if d.enabled))
        return [instance for group in groups for instance in group]


class GammaResolution:
    def __init__(self, http, *, clock=lambda: datetime.now(timezone.utc)):
        self.http, self.clock = http, clock

    async def resolve(self, instance: MarketInstance) -> OfficialResolution | None:
        if not instance.verified or not instance.slug:
            return None
        response = await self.http.get("/markets/"+quote(instance.external_id, safe=""))
        # Read exact market ID; a market slug need not equal its parent event slug.
        markets = [m for m in [response] if str(m.get("id")) == instance.external_id
                   and str(m.get("conditionId")) == instance.condition_id]
        if len(markets) != 1:
            raise ValueError("official resolution market identity mismatch")
        market = markets[0]
        if labeled_tokens(market) != (instance.up_token_id,instance.down_token_id):
            raise ValueError("official resolution tokens changed")
        if market.get("closed") is not True or market.get("umaResolutionStatus") != "resolved":
            return None
        labels, prices = array(market.get("outcomes")), array(market.get("outcomePrices"))
        if len(prices) != 2 or sorted(decimal(p) for p in prices) != [0,1]:
            return None
        winner_label = str(labels[next(i for i,p in enumerate(prices) if decimal(p)==1)]).lower()
        winner = instance.up_token_id if winner_label == "up" else instance.down_token_id
        return OfficialResolution(instance.instance_id,winner,self.clock(),"GAMMA_PUBLIC_CLOSED_UMA_RESOLVED")
