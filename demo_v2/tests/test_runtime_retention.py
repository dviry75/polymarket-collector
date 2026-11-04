"""History remains durable while only active/held market state lives in RAM."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from conftest import open_position
from demo_v2.providers.contracts import Providers
from demo_v2.providers.underlying import BinanceResearchProvider
from test_runtime import runtime


def other(market, name, start, end):
    return replace(market, instance_id=name, external_id=name, condition_id=name,
                   up_token_id=name+'-up', down_token_id=name+'-down',
                   starts_at=start, ends_at=end)


def test_restart_recovers_expired_held_market_without_loading_wide_history(repository, market, rule):
    held = open_position(repository, rule)
    now = market.ends_at + timedelta(hours=1)
    upcoming = other(market, 'upcoming', now+timedelta(minutes=1), now+timedelta(hours=1, minutes=1))
    repository.upsert_instance(upcoming)
    for index in range(80):
        expired = other(market, f'history-{index}', market.starts_at-timedelta(days=2), market.ends_at-timedelta(days=2))
        repository.upsert_instance(expired, {'fake_wide_metadata': 'x'*65536})
    assert {r['id'] for r in repository.runtime_instances(now)} == {market.instance_id, upcoming.instance_id}
    worker = runtime(repository)
    worker.clock = lambda: now
    async def run():
        await worker.start()
        try:
            assert set(worker.instances) == {market.instance_id, upcoming.instance_id}
            assert worker.projection['positions'][0]['id'] == held['id']
            assert len(repository.instances()) == 82  # No automatic history deletion.
        finally:
            await worker.close()
    asyncio.run(run())


def test_rollover_retains_current_held_subscribed_and_inflight_reference_until_retired(repository, market, rule):
    now = market.ends_at + timedelta(hours=1)
    ids = ['current', 'held', 'subscribed', 'reference', 'retired']
    models = {name: other(market, name, market.starts_at, market.ends_at) for name in ids}
    future = other(market, 'future', now+timedelta(minutes=1), now+timedelta(hours=1, minutes=1))
    data = SimpleNamespace(token_map={'fake-token': models['subscribed']})
    underlying = BinanceResearchProvider(None)
    worker = runtime(repository, Providers(market_data=data, underlying=underlying))
    worker.clock = lambda: now
    worker.instances = {**models, 'future': future}
    worker.metadata = {name: {'source': 'fake'} for name in worker.instances}
    worker.owner.projection['positions'] = [{'instance_id': 'held'}]
    worker.current = {'ETH-1h': 'current'}
    worker.reference_tasks = {'reference': object()}
    worker.references = {name: object() for name in models}
    underlying.baselines = {name: object() for name in models}
    for mapping in (worker._gate_epochs, worker._gate_health, worker.stability.since):
        mapping[('r', 'retired')] = 1
        mapping[('r', 'held')] = 1
    worker.prune_runtime_instances(now)
    assert set(worker.instances) == {'current', 'held', 'subscribed', 'reference', 'future'}
    assert 'retired' not in worker.metadata and 'retired' not in worker.references
    assert 'retired' not in underlying.baselines
    assert ('r', 'held') in worker.stability.since and ('r', 'retired') not in worker.stability.since
    worker.reference_tasks.clear()
    data.token_map.clear()
    worker.owner.projection['positions'].clear()
    worker.current.clear()
    worker.prune_runtime_instances(now)
    assert set(worker.instances) == {'future'}
    assert worker.references == {} and underlying.baselines == {}
