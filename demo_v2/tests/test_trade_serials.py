"""Stable global trade labels; all DBs are disposable and providers are fake."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from decimal import Decimal

import pytest

from conftest import open_position
from demo_v2.domain.trading import ExitReason
from demo_v2.persistence.repository import DemoRepository
from demo_v2.persistence import trade_serials
from test_api import app_for,login,PREFIX
from test_app_isolation import running_client


def test_serials_global_idempotent_close_restart_and_metadata_only(repository,rule):
    first=open_position(repository,rule,1)
    other=repository.create_rule(rule['spec'])
    second=open_position(repository,other,1)
    assert [first['serial_number'],second['serial_number']]==[1,2]
    assert open_position(repository,rule,1)['serial_number']==1
    closed=repository.close_position(first['id'],price=Decimal('0.96'),sell_fee=Decimal('0.068'),
        reason=ExitReason.TAKE_PROFIT,occurred_at=datetime.now(timezone.utc),provenance={})
    assert closed['serial_number']==1
    restarted=DemoRepository(repository.database);restarted.ensure_trade_serials()
    assert {p['id']:p['serial_number'] for p in restarted.positions()}=={first['id']:1,second['id']:2}
    with repository.database.connect() as conn:
        assert 'serial_number' not in [row['name'] for row in conn.execute('PRAGMA table_info(demo_v2_positions)')]
        assert conn.execute('SELECT COUNT(*) FROM demo_v2_schema').fetchone()[0]==1


def test_existing_trades_backfill_chronological_stable_and_no_financial_changes(repository,rule):
    positions=[open_position(repository,rule,i) for i in range(1,4)]
    with repository.database.transaction() as conn:
        # Reproduce a pre-feature DB: financial rows already exist, no serial metadata.
        conn.execute("DELETE FROM demo_v2_meta WHERE key GLOB 'trade_serial*'")
        conn.execute('UPDATE demo_v2_positions SET entry_at=? WHERE id=?',('2026-01-01T00:00:00+00:00',positions[-1]['id']))
        before=[dict(row) for row in conn.execute('SELECT * FROM demo_v2_positions ORDER BY id')]
        ledger=[dict(row) for row in conn.execute('SELECT * FROM demo_v2_ledger ORDER BY id')]
        expected=[row[0] for row in conn.execute('SELECT id FROM demo_v2_positions ORDER BY entry_at,id')]
    repository.ensure_trade_serials();repository.ensure_trade_serials()
    assert {p['id']:p['serial_number'] for p in repository.positions()}==dict(zip(expected,[1,2,3]))
    with repository.database.connect() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM demo_v2_positions ORDER BY id')]==before
        assert [dict(row) for row in conn.execute('SELECT * FROM demo_v2_ledger ORDER BY id')]==ledger


@pytest.mark.parametrize('factory',[False,True])
def test_serials_are_not_reused_after_reset(repository,rule,factory):
    first=open_position(repository,rule)
    repository.reset(factory=factory)
    repository.ensure_trade_serials()
    if factory:rule=repository.create_rule(rule['spec'])
    second=open_position(repository,rule)
    assert second['serial_number']==first['serial_number']+1
    with repository.database.connect() as conn:
        assert conn.execute('SELECT 1 FROM demo_v2_meta WHERE key=?',(trade_serials.PREFIX+first['id'],)).fetchone() is None


def test_atomic_allocation_rollback_and_concurrent_uniqueness(repository,rule):
    with pytest.raises(RuntimeError):
        with repository.database.transaction() as conn:
            from conftest import quote,amounts
            repository.open_position(rule_id=rule['id'],version_id=rule['current_version_id'],
                observation=quote(),amounts=amounts(),fee_evidence={'state':'VALID'},
                provenance={},idempotency_key='rolled-back',conn=conn)
            raise RuntimeError('abort simulated entry')
    assert repository.positions()==[]
    other=repository.create_rule(rule['spec'])
    entries=[(rule,1),(rule,2),(other,1),(other,2)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda pair:open_position(repository,*pair),entries))
    assert sorted(p['serial_number'] for p in rows)==[1,2,3,4]


def test_api_serials_are_stable_across_pagination_filters_detail_and_restart(tmp_path):
    app=app_for(tmp_path)
    async def run():
        async with running_client(app) as client:
            from conftest import market as market_fixture
            # Reuse the isolated fixture setup without contacting providers.
            market=market_fixture.__wrapped__(app.state.repository)
            from demo_v2.domain.trading import RuleSpec
            repo=app.state.repository
            rule=repo.create_rule(RuleSpec(name='Serial test',markets=(market.definition.key,),entry_trigger='0.74',entry_frequency=3))
            created=[open_position(repo,rule,i) for i in (1,2,3)]
            await login(client)
            pages=[]
            for offset in range(3):
                response=await client.get(PREFIX+'/trades',params={'limit':1,'offset':offset})
                assert response.status_code==200,response.text
                pages.extend(response.json()['items'])
            expected={p['id']:p['serial_number'] for p in created}
            assert {p['id']:p['serial_number'] for p in pages}==expected
            filtered=(await client.get(PREFIX+'/trades',params={'rule_id':rule['id']})).json()['items']
            assert {p['id']:p['serial_number'] for p in filtered}==expected
            for position in pages:
                detail=(await client.get(PREFIX+'/trades/'+position['id'])).json()
                assert detail['serial_number']==position['serial_number']
            restarted=DemoRepository(repo.database);restarted.ensure_trade_serials()
            assert {p['id']:p['serial_number'] for p in restarted.positions()}==expected
    asyncio.run(run())
