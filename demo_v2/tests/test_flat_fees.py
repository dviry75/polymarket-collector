"""Empirical model regressions; all prices/providers/DBs are fake or temporary."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from demo_v2.api.read_model import trade_view, kpi
from demo_v2.config import DemoConfig
from demo_v2.domain.fees import FlatFeePolicy, DEFAULT_FLAT_FEE, FEE_METHOD
from demo_v2.domain.models import OfficialResolution, market_definitions
from demo_v2.domain.trading import RuleSpec, WeeklyRange
from demo_v2.engine import DemoEngine
from demo_v2.health import ComponentHealth, HealthState
from demo_v2.providers.contracts import Providers
from conftest import quote
from test_runtime import runtime


def test_default_and_explicit_config(tmp_path):
    assert DemoConfig.from_env({'DEMO_DATA_DIR':str(tmp_path)}).flat_fee_per_action == Decimal('0.068')
    assert DemoConfig.from_env({'DEMO_DATA_DIR':str(tmp_path), 'DEMO_FLAT_FEE_PER_ACTION':'0.123456'}).flat_fee_per_action == Decimal('0.123456')
    assert FlatFeePolicy(Decimal(0)).buy(Decimal(5),Decimal('.74')).cash_debit == Decimal('3.70')


@pytest.mark.parametrize('value',['', 'garbage', '-0.001', 'NaN', 'sNaN', 'Infinity', '-Infinity', None, True, 0.068])
def test_invalid_flat_fee_fails_before_database(tmp_path,value):
    with pytest.raises(ValueError):
        DemoConfig(data_dir=tmp_path/'unused', flat_fee_per_action=value)
    assert not (tmp_path/'unused').exists()


@pytest.mark.parametrize('definition',market_definitions(),ids=lambda d:d.key)
def test_flat_fee_independent_of_family_size_price_and_exchange_metadata(repository,market,definition):
    instance=replace(market,instance_id="flat-"+definition.key,condition_id="flat-condition",external_id="flat-external",up_token_id="flat-up",down_token_id="flat-down",definition=definition,ends_at=market.starts_at+timedelta(seconds=definition.expected_duration))
    now=instance.starts_at+timedelta(seconds=1)
    repository.upsert_instance(instance, {'feesEnabled':True, 'feeSchedule':{'rounding':'UNKNOWN'}})
    rule=repository.create_rule(RuleSpec(name='all families',markets=(definition.key,),entry_trigger='0.74',gross_shares='500'))
    class UnavailableExactContract:
        def __getattribute__(self,name):
            pytest.fail('execution consulted exchange fee evidence: '+name)
    engine=DemoEngine(repository,clock=lambda:now)
    engine.begin_generation(repository.next_generation())
    engine.process(quote(at=now,instance_id=instance.instance_id,token=instance.up_token_id),instance,UnavailableExactContract())
    row=repository.positions()[0]
    assert row['rule_id']==rule['id']
    assert row['buy_fee_text']=='0.068' and row['net_shares_text']=='500'
    assert repository.wallet()==Decimal('-370.068')


def test_entry_sell_roundtrip_api_ledger_and_pnl(repository,market,rule):
    engine=DemoEngine(repository)
    engine.begin_generation(repository.next_generation())
    engine.process(quote(),market)
    opened=trade_view(repository.positions()[0])
    assert opened['gross_shares']==opened['net_shares']=='5' and opened['buy_fee_shares']=='0'
    assert opened['cash_debit']=='3.768' and opened['buy_fee']=='0.068'
    engine.process(quote(2,bid='0.96',ask='0.97'),market)
    row=repository.positions()[0]
    closed=trade_view(row)
    assert closed['sell_fee']=='0.068' and closed['total_fees']=='0.136'
    assert closed['net_proceeds']=='4.732' and closed['net_pnl']=='0.964'
    assert repository.wallet()==Decimal('0.964')
    assert kpi(repository,[row])['total_fees']=='0.136'
    for field in ('fee_evidence','entry_provenance','exit_provenance'):
        evidence=closed[field] if field=='fee_evidence' else closed[field]['fee']
        assert evidence['method']==FEE_METHOD and evidence['per_action']=='0.068'
        assert evidence['historical_operations']==1644 and evidence['historical_total_fees']=='111.81'
        assert evidence['replicates_exact_polymarket_fees'] is False
    with repository.database.connect() as conn:
        ledger=conn.execute('SELECT * FROM demo_v2_ledger ORDER BY movement_type').fetchall()
    assert [Decimal(row['amount_text']) for row in ledger]==[Decimal('-3.768'),Decimal('4.732')]
    assert all(json.loads(row['provenance_json'])['fee']['method']==FEE_METHOD for row in ledger)


@pytest.mark.parametrize('bid',['0','0.001'])
def test_flat_sell_fee_is_not_capped_at_proceeds(repository,market,rule,bid):
    engine=DemoEngine(repository)
    engine.begin_generation(repository.next_generation())
    engine.process(quote(),market)
    engine.process(quote(2,bid=bid,ask='.02'),market)
    row=trade_view(repository.positions()[0])
    assert row['exit_reason']=='STOP_LOSS' and row['sell_fee']=='0.068'
    assert Decimal(row['net_proceeds'])==Decimal(5)*Decimal(bid)-DEFAULT_FLAT_FEE < 0
    assert repository.wallet()==Decimal(row['net_pnl'])


@pytest.mark.parametrize('winner,payout',[('eth-up','5'),('eth-down','0')])
def test_resolution_preserves_entry_fee_and_has_no_exit_fee(repository,market,rule,winner,payout):
    engine=DemoEngine(repository)
    engine.begin_generation(repository.next_generation())
    engine.process(quote(),market)
    resolution=OfficialResolution(market.instance_id,winner,datetime.now(timezone.utc),'FAKE_OFFICIAL')
    assert repository.resolve(resolution)==1 and repository.resolve(resolution)==0
    row=trade_view(repository.positions()[0])
    assert row['total_fees']=='0.068' and row['sell_fee']=='0'
    assert Decimal(row['net_proceeds'])==Decimal(payout)
    assert Decimal(row['net_pnl'])==Decimal(payout)-Decimal('3.768')==repository.wallet()


def test_config_change_does_not_rewrite_entry_or_historical_ledger(repository,market,rule):
    engine=DemoEngine(repository,flat_fee_per_action=Decimal('0.123456'))
    engine.begin_generation(repository.next_generation())
    engine.process(quote(),market)
    old=repository.positions()[0]
    replacement=DemoEngine(repository)  # New configuration applies only to new actions.
    replacement.begin_generation(repository.next_generation())
    replacement.process(quote(generation=2,bid='.96',ask='.97'),market)
    row=trade_view(repository.positions()[0])
    assert row['buy_fee']=='0.123456' and row['sell_fee']=='0.068'
    assert row['fee_evidence']['per_action']=='0.123456'
    assert row['entry_provenance']['fee']['per_action']=='0.123456'
    assert row['exit_provenance']['fee']['per_action']=='0.068'
    assert row['cash_debit']==old['cash_debit_text']


@pytest.mark.parametrize('fault',['discovery','database','engine','books','critical'])
def test_non_fee_runtime_guards_still_block_entries(repository,market,rule,fault):
    class Data:
        def token_ready(self,token): return fault!='books'
    worker=runtime(repository,Providers(market_data=Data()))
    worker.database_ok=fault!='database'
    worker.engine_ok=fault!='engine'
    worker.discovery_at=datetime.now(timezone.utc)-timedelta(seconds=31 if fault=='discovery' else 0)
    worker.current[market.definition.key]=market.instance_id
    if fault=='critical': worker.record('other_critical_dependency',False,'FAKE_FAILURE')
    worker.engine.begin_generation(repository.next_generation())
    worker.engine.process(quote(),market)
    assert not repository.positions()


def test_schedule_and_unobserved_exact_trigger_still_block(repository,market,rule):
    now=datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    weekday=now.astimezone(ZoneInfo('Asia/Jerusalem')).weekday()
    repository.update_rule(rule['id'],rule['spec'].model_copy(update={'weekly_schedule':(
        WeeklyRange(weekday=(weekday+2)%7,start='00:00',end='23:59'),)}))
    engine=DemoEngine(repository,clock=lambda:now)
    engine.begin_generation(repository.next_generation())
    engine.process(quote(at=now),market)
    assert not repository.positions()
    repository.update_rule(rule['id'],rule['spec'])
    engine.process(quote(2,ask='.75',at=now),market)
    engine.process(quote(3,ask='.73',at=now),market)
    assert not repository.positions()


def test_runtime_configured_policy_and_api_provenance_without_exchange_fee_worker(tmp_path,market):
    import asyncio
    from demo_v2.app import create_app
    from test_api import app_for, login, PREFIX
    from test_app_isolation import running_client
    config=replace(app_for(tmp_path).state.config,flat_fee_per_action=Decimal('0.123456'))
    app=create_app(config)
    async def run():
        async with running_client(app) as client:
            await login(client)
            worker=app.state.runtime
            worker.instances[market.instance_id]=market
            worker.current[market.definition.key]=market.instance_id
            health=(await client.get(PREFIX+'/health')).json()
            evidence=health['fee_model']
            assert evidence['method']==FEE_METHOD and evidence['per_action']=='0.123456'
            assert not any(c['component']=='fee_provider' for c in health['components'])
            assert not any('fee_provider' in name for name in health['workers'])
            assert 'CURRENT_FEE_ROUNDING_UNVERIFIED' not in health['blocker_codes']
            markets=(await client.get(PREFIX+'/markets')).json()
            assert markets['instances'][0]['fee']==evidence
            assert worker.engine.fee_policy.buy(Decimal(5),Decimal('.74')).cash_debit==Decimal('3.823456')
    asyncio.run(run())
