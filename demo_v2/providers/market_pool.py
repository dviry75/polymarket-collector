"""Three independently fenced public connections, grouped by market timeframe."""
import asyncio
from .market_ws import PublicMarketWS
from ..domain.models import Timeframe
from ..streams import DecisionStream


class MarketDataPool:
    def __init__(self, telemetry, *, generation_factory, config, on_issue, on_market_invalid=lambda key:None, manager_factory=PublicMarketWS):
        self.shards={}
        for timeframe in Timeframe:
            key=timeframe.value
            stream=DecisionStream(capacity=config.decision_queue_capacity,max_age_seconds=config.observation_max_age_seconds,
                max_queue_delay_seconds=config.decision_max_queue_delay_seconds)
            self.shards[key]=manager_factory(stream,telemetry,generation_factory=generation_factory,
                stale_seconds=config.observation_max_age_seconds,
                transport_idle_seconds=config.transport_idle_seconds,snapshot_timeout_seconds=config.snapshot_timeout_seconds,
                on_issue=lambda code,k=key:on_issue(k,code),on_market_invalid=on_market_invalid)

    @property
    def streams(self):
        return {key:shard.decisions for key,shard in self.shards.items()}

    @property
    def latest(self):
        return {key:value for shard in self.shards.values() for key,value in shard.latest.items()}

    @property
    def token_map(self):
        return {key:value for shard in self.shards.values() for key,value in shard.token_map.items()}

    @property
    def market_epochs(self):
        return {key:value for shard in self.shards.values() for key,value in shard.market_epochs.items()}

    @property
    def status(self):
        wanted=[shard for shard in self.shards.values() if shard.token_map]
        return "CONNECTED" if all(s.status=="CONNECTED" for s in wanted) else "PARTIAL"

    def token_ready(self, token):
        return any(token in shard.token_map and shard.token_ready(token) for shard in self.shards.values())

    def health(self):
        return {"status":self.status,"shards":{k:s.health() for k,s in self.shards.items()},
                "tokens":{token:self.token_ready(token) for token in self.token_map}}

    async def set_subscriptions(self, instances):
        instances=tuple(instances)
        await asyncio.gather(*(s.set_subscriptions(tuple(i for i in instances if i.definition.timeframe.value==key))
                              for key,s in self.shards.items()))

    async def run(self):
        async with asyncio.TaskGroup() as group:
            for key,shard in self.shards.items():
                group.create_task(shard.run(),name="demo-ws-"+key)

    async def resync(self, reason, shard=None):
        selected=self.shards.values() if shard is None else (self.shards[shard],)
        await asyncio.gather(*(s.resync(reason) for s in selected))

    async def close(self):
        await asyncio.gather(*(s.close() for s in self.shards.values()))
