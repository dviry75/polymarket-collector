"""Cross-layer regressions found while completing the V2 validation matrix."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from demo_v2.api.read_model import TradeFilter,cohort,kpi,trade_view
from demo_v2.domain.models import OfficialResolution
from demo_v2.domain.trading import ExitReason,FeeState,RuleSpec
from demo_v2.engine import DemoEngine
from demo_v2.persistence.repository import instance_model
from demo_v2.providers.contracts import Providers
from demo_v2.providers.fees import GammaFeeProvider
from demo_v2.providers.discovery import GammaDiscovery,candidate_family
from conftest import quote,open_position
from test_engine import engine
from test_fee_underlying import context,verified_fee
from test_runtime import runtime


def test_database_lock_wait_cannot_age_quote_into_action(repository,market,rule,monkeypatch):
    now=datetime.now(timezone.utc)
    clock=[now]
    worker=DemoEngine(repository,clock=lambda:clock[0])
    worker.begin_generation(repository.next_generation())
    original=repository.database.transaction
    @contextmanager
    def delayed():
        with original() as conn:
            clock[0]+=timedelta(seconds=6)
            yield conn
    monkeypatch.setattr(repository.database,"transaction",delayed)
    assert worker.process(quote(at=now),market,verified_fee(observed_at=now))==[]
    assert repository.positions()==[]
    assert worker.reason=="OBSERVATION_AGED_WHILE_WAITING_FOR_DATABASE"


def test_engine_restart_preserves_quota_and_fresh_first_snapshot(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee())
    restarted=engine(repository,market)
    restarted.process(quote(generation=restarted.generation),market,verified_fee())
    assert repository.entry_count(rule["id"],market.instance_id)==2
    assert len(repository.positions(status="OPEN"))==2


def test_no_retroactive_sl_when_reconnect_has_recovered_bid(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee())
    worker.invalidate("DISCONNECTED")
    worker.process(quote(2,bid="0.63",ask="0.7"),market,verified_fee())
    worker.begin_generation(repository.next_generation())
    worker.process(quote(generation=2,bid="0.72",ask="0.75"),market,verified_fee())
    assert repository.positions()[0]["status"]=="OPEN"


def test_schedule_close_blocks_entry_but_not_exit(repository,market,rule):
    from demo_v2.domain.trading import WeeklyRange
    from zoneinfo import ZoneInfo
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee())
    weekday=(datetime.now(ZoneInfo("Asia/Jerusalem")).weekday()+2)%7
    spec=RuleSpec.model_validate({**rule["spec"].model_dump(),"weekly_schedule":[WeeklyRange(weekday=weekday,start="01:00",end="02:00")]})
    repository.update_rule(rule["id"],spec)
    worker.process(quote(2,token="eth-down"),market,verified_fee())
    assert len(repository.positions())==1
    worker.process(quote(3,bid="0.97",ask="0.98"),market,verified_fee())
    assert repository.positions()[0]["exit_reason"]=="TAKE_PROFIT"


def test_unknown_exit_fees_never_fabricate_exit_but_official_resolution_can_settle(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee())
    position=repository.positions()[0]
    worker.process(quote(2,bid="0.97",ask="0.98"),market,verified_fee(state=FeeState.UNKNOWN,rate=Decimal(0)))
    assert repository.positions()[0]["status"]=="OPEN"
    sample=context(market,"102")
    evidence=OfficialResolution(market.instance_id,market.up_token_id,datetime.now(timezone.utc),"fake-official")
    assert repository.resolve(evidence,reference=sample)==1
    closed=repository.positions()[0]
    assert closed["exit_price_text"]=="1" and closed["sell_fee_text"]=="0"
    assert repository.wallet()==Decimal(position["net_shares_text"])-Decimal(position["cash_debit_text"])
    assert "102" in closed["exit_provenance_json"]
    with repository.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_underlying WHERE context='EXIT'").fetchone()[0]==1


def test_share_fee_ledger_not_double_counted_and_movement_cohort(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee(),context(market,"101.5"))
    worker.process(quote(2,bid="0.97",ask="0.98"),market,verified_fee(),context(market,"103"))
    rows=cohort(repository,TradeFilter(movement_min="1",movement_max="2"))
    assert len(rows)==1
    row=rows[0]
    assert Decimal(row["cash_debit_text"])==Decimal(row["entry_notional_text"])
    assert Decimal(row["net_proceeds_text"])-Decimal(row["cash_debit_text"])==repository.wallet()==Decimal(row["net_pnl_text"])
    stats=kpi(repository,rows)
    assert Decimal(stats["wallet"])==repository.wallet()
    assert Decimal(stats["total_fees"])==Decimal(row["buy_fee_text"])+Decimal(row["sell_fee_text"])
    view=trade_view(row)
    assert view["underlying_movements"]["open_to_entry"]["percent"]=="1.5"
    assert view["underlying_movements"]["during_trade"]["absolute"]=="1.5"
    assert cohort(repository,TradeFilter(movement_min="2",movement_max="3"))==[]


def test_catalog_retains_verified_provider_identity(repository,market):
    verified=replace(market,definition=replace(market.definition,family_id="verified-series",provenance="FAKE_PUBLIC"))
    repository.upsert_instance(verified,{"description":"source rules"})
    row=next(r for r in repository.definitions() if r["id"]==market.definition.key)
    assert row["family_id"]=="verified-series" and json.loads(row["provenance_json"])["provider_verified"]
    restored=instance_model(repository.instance(market.instance_id))
    assert restored.definition.family_id=="verified-series"


def test_ambiguous_series_identity_is_never_last_row_wins():
    from demo_v2.domain.models import market_definitions
    definition=market_definitions()[0]
    class HTTP:
        async def get(self,path,params=None):
            assert path=="/series"
            return [{"id":str(i),"slug":candidate_family(definition),"recurrence":"5m"} for i in (1,2)]
    provider=GammaDiscovery(HTTP())
    assert asyncio.run(provider.discover([definition]))==[]
    assert provider.issues[definition.key]=="FAMILY_NOT_VERIFIED"


@pytest.mark.parametrize("metadata,state", [({"feesEnabled":False},FeeState.VALID),
    ({"feesEnabled":True},FeeState.UNKNOWN),
    ({"feesEnabled":True,"feeSchedule":{"rate":"0","exponent":1}},FeeState.UNSUPPORTED),
    ({"feesEnabled":True,"feeSchedule":{"rate":"0.02","exponent":2}},FeeState.UNSUPPORTED)])
def test_public_fee_states_never_promote_incomplete_zero(market,metadata,state):
    market=replace(market,up_token_id="11",down_token_id="22")
    class HTTP:
        async def get(self,path):
            return {"id":market.external_id,"conditionId":market.condition_id,"outcomes":["Up","Down"],
                "clobTokenIds":["11","22"],**metadata}
    evidence=asyncio.run(GammaFeeProvider(HTTP()).parameters(market))
    assert evidence.state==state
    if state==FeeState.VALID:
        assert evidence.buy(Decimal(5),Decimal("0.74")).buy_fee==0


def test_exhausted_rule_unsubscribes_after_last_position_closes(repository,market,rule):
    rule=repository.update_rule(rule["id"],RuleSpec.model_validate({**rule["spec"].model_dump(),"entry_frequency":1}))
    class Data:
        status="CONNECTED"
        wanted=()
        def token_ready(self,token): return True
        async def set_subscriptions(self,instances): self.wanted=instances
    data=Data()
    worker=runtime(repository,Providers(market_data=data))
    worker.database_ok=worker.engine_ok=True
    worker.instances[market.instance_id]=market
    worker.current[market.definition.key]=market.instance_id
    worker.discovery_at=datetime.now(timezone.utc)
    worker.fees[market.instance_id]=verified_fee()
    asyncio.run(worker.demand_once())
    assert data.wanted==(market,)
    position=open_position(repository,rule)
    asyncio.run(worker.demand_once())
    assert data.wanted==(market,)
    repository.close_position(position["id"],price=Decimal("0.74"),sell_fee=Decimal(0),reason=ExitReason.TAKE_PROFIT,
        occurred_at=datetime.now(timezone.utc),provenance={})
    asyncio.run(worker.demand_once())
    assert not data.wanted


def test_blocked_research_instance_cannot_starve_healthy_instance(repository,market):
    second=replace(market,instance_id="second",external_id="second",condition_id="second",up_token_id="second-up",down_token_id="second-down")
    repository.upsert_instance(second)
    release=asyncio.Event()
    class Underlying:
        async def context(self,instance,metadata):
            if instance.instance_id==market.instance_id:
                await release.wait()
            return context(instance)
    worker=runtime(repository,Providers(underlying=Underlying()))
    worker.instances={market.instance_id:market,second.instance_id:second}
    worker.current={"first":market.instance_id,"second":second.instance_id}
    async def run():
        await worker.references_once()
        try:
            for _ in range(100):
                if second.instance_id in worker.references:
                    break
                await asyncio.sleep(0)
            assert second.instance_id in worker.references
            assert market.instance_id not in worker.references
        finally:
            await worker.close()
        assert all(task.done() for task in worker.all_tasks())
    asyncio.run(run())
