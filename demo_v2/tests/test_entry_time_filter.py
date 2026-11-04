"""Elapsed entry cohorts use each actual market interval, never rule windows."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from demo_v2.api.read_model import TradeFilter, TradeQuery, trade_page, kpi
from demo_v2.app import create_app
from demo_v2.domain.models import MarketInstance, market_definitions
from demo_v2.domain.trading import ExitReason, RuleSpec
from conftest import quote, amounts
from test_api import app_for, login, PREFIX
from test_app_isolation import running_client


@pytest.fixture
def elapsed_history(repository):
    start=datetime(2026,1,2,12,tzinfo=timezone.utc)
    definitions=market_definitions()
    rule=repository.create_rule(RuleSpec(name='Elapsed entries',markets=tuple(d.key for d in definitions),
        entry_trigger='0.74',entry_frequency=20,entry_windows=({'start_seconds':20,'end_seconds':40},)))
    expected=[]
    for d in definitions:
        duration=d.timeframe.seconds
        market=MarketInstance(d.key,d,'condition-'+d.key,'up-'+d.key,'down-'+d.key,
            start,start+timedelta(seconds=duration),verified=True,external_id='fake-'+d.key,
            metadata_source='fake-public',discovered_at=start)
        repository.upsert_instance(market)
        # Include exact boundaries and a fraction before each exclusive end.
        for n,offset in enumerate((0,60,119.999999,120,duration-0.000001,duration)):
            at=start+timedelta(seconds=offset)
            row=repository.open_position(rule_id=rule['id'],version_id=rule['current_version_id'],
                observation=quote(n+1,market.up_token_id,market.instance_id,at=at),amounts=amounts(),
                fee_evidence={'state':'VALID'},provenance={},idempotency_key=f'{d.key}-{n}')
            closed=n%2==0
            if closed:
                repository.close_position(row['id'],price=Decimal('0.8'),sell_fee=Decimal('0.01'),
                    reason=ExitReason.TAKE_PROFIT,occurred_at=at+timedelta(seconds=1),provenance={})
            expected.append((row['id'],d,offset,closed))
    return expected


@pytest.mark.parametrize('bounds',[(None,None),(0,None),(60,120),(120,None),(300,900),(900,3600),(0,300)])
def test_elapsed_cohort_all_assets_frames_pagination_kpi_and_exact_boundaries(repository,elapsed_history,bounds):
    lower,upper=bounds
    filters=dict(entry_elapsed_min_seconds=lower,entry_elapsed_max_seconds=upper)
    wanted=[row for row,d,offset,closed in elapsed_history
        if (lower is None and upper is None) or
        (offset<d.timeframe.seconds and offset>=(lower or 0) and (upper is None or offset<upper))]
    page=trade_page(repository,TradeQuery(**filters,limit=2,offset=1))
    stats=kpi(repository,TradeFilter(**filters))
    assert page['total']==stats['total_trades']==len(wanted)
    assert len(page['items'])==min(2,max(0,len(wanted)-1))
    assert all(row['id'] in wanted for row in page['items'])
    assert stats['open']+stats['positive']==len(wanted)
    for frame in ('5m','15m','1h'):
        combined=TradeQuery(**filters,asset='ETH',timeframe=frame,status='CLOSED',limit=200)
        ids={row['id'] for row in trade_page(repository,combined)['items']}
        expected={row for row,d,offset,closed in elapsed_history
            if row in wanted and d.key==f'ETH-{frame}' and closed}
        assert ids==expected
        assert kpi(repository,combined)['total_trades']==len(expected)


def test_elapsed_uses_aware_instants_not_text_or_rule_window(repository,elapsed_history):
    row=next(row for row,d,offset,closed in elapsed_history if d.key=='ETH-5m' and offset==60)
    with repository.database.transaction() as conn:
        conn.execute('UPDATE demo_v2_positions SET entry_at=? WHERE id=?',('2026-01-02T14:01:00+02:00',row))
    page=trade_page(repository,TradeQuery(asset='ETH',timeframe='5m',entry_elapsed_min_seconds=60,
        entry_elapsed_max_seconds=61))
    assert [item['id'] for item in page['items']]==[row]


@pytest.mark.parametrize('values',[
    {'entry_elapsed_min_seconds':-1},{'entry_elapsed_max_seconds':3601},
    {'entry_elapsed_min_seconds':3600},{'entry_elapsed_max_seconds':0},
    {'entry_elapsed_min_seconds':60,'entry_elapsed_max_seconds':60},
    {'entry_elapsed_min_seconds':120,'entry_elapsed_max_seconds':60},
    {'entry_elapsed_min_seconds':'NaN'},{'entry_elapsed_max_seconds':'1.5'},
])
def test_bad_elapsed_bounds_rejected(values):
    with pytest.raises(ValidationError):TradeFilter(**values)


def test_elapsed_api_query_is_shared_and_invalid_ranges_are_422(tmp_path,repository,elapsed_history):
    config=replace(app_for(tmp_path).state.config,data_dir=repository.database.config.data_dir,
        db_path=repository.database.config.db_path)
    async def run():
        async with running_client(create_app(config)) as client:
            await login(client)
            params={'entry_elapsed_min_seconds':60,'entry_elapsed_max_seconds':120,'timeframe':'15m'}
            trades=await client.get(PREFIX+'/trades',params=params)
            stats=await client.get(PREFIX+'/kpi',params=params)
            assert trades.status_code==stats.status_code==200
            assert trades.json()['total']==stats.json()['total_trades']==14
            for endpoint in ('trades','kpi'):
                assert (await client.get(PREFIX+'/'+endpoint,params={**params,'entry_elapsed_max_seconds':59})).status_code==422
    asyncio.run(run())
