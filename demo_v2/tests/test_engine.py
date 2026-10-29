from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from demo_v2.domain.eligibility import entry_eligible, scheduled
from demo_v2.domain.trading import FeeState, RuleSpec
from demo_v2.engine import DemoEngine
from conftest import quote
from test_fee_underlying import verified_fee, context


def engine(repository, market):
    result = DemoEngine(repository)
    result.begin_generation(repository.next_generation())
    return result


@pytest.mark.parametrize("prices,count", [(["0.75","0.74"],1),(["0.75","0.73"],0),
    (["0.740","0.74","0.74"],1),(["0.74","0.73","0.74"],2)])
def test_exact_rearm(repository,market,rule,prices,count):
    worker=engine(repository,market)
    for seq,price in enumerate(prices,1):
        worker.process(quote(seq,ask=price),market,verified_fee())
    assert len(repository.positions()) == count


def test_shared_quota_concurrent_positions_independent_rules(repository,market,rule):
    second=repository.create_rule(rule["spec"].model_copy(update={"entry_frequency":1}))
    worker=engine(repository,market)
    for seq,(token,ask) in enumerate([("eth-up","0.74"),("eth-down","0.74"),
        ("eth-up","0.73"),("eth-up","0.74"),("eth-down","0.73"),("eth-down","0.74")],1):
        worker.process(quote(seq,token=token,ask=ask),market,verified_fee())
    assert len(repository.positions(rule_id=rule["id"],status="OPEN")) == 3
    assert len(repository.positions(rule_id=second["id"])) == 1


@pytest.mark.parametrize("state", [FeeState.UNKNOWN,FeeState.STALE,FeeState.UNSUPPORTED])
def test_invalid_fee_blocks_without_default(repository,market,rule,state):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee(state=state,rate=Decimal(0)))
    assert repository.positions() == [] and repository.wallet() == 0


@pytest.mark.parametrize("bid,reason", [("0.63","STOP_LOSS"),("0.97","TAKE_PROFIT")])
def test_jump_exit_snapshot_survives_edit_and_archive(repository,market,rule,bid,reason):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee(),context(market))
    repository.update_rule(rule["id"],rule["spec"].model_copy(update={"enabled":False,"archived":True,"take_profit":None,"stop_loss":None}))
    worker.process(quote(2,ask="0.98",bid=bid),market,verified_fee(),context(market,"102"))
    position=repository.positions()[0]
    assert position["exit_price_text"] == bid and position["exit_reason"] == reason
    assert position["rule_version_id"] == rule["current_version_id"]
    assert repository.wallet() == Decimal(position["net_pnl_text"])
    with repository.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_underlying").fetchone()[0] == 3


def test_reconnect_no_replay_old_generation_or_stale_observation(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(ask="0.75"),market,verified_fee())
    worker.invalidate("DISCONNECTED")
    worker.process(quote(2),market,verified_fee())
    assert not repository.positions()
    worker.begin_generation(repository.next_generation())
    worker.process(quote(2,generation=1),market,verified_fee())
    worker.process(quote(generation=2),market,verified_fee())
    assert len(repository.positions()) == 1
    worker.process(quote(2,generation=2,bid="0.63",at=datetime.now(timezone.utc)-timedelta(seconds=6)),market,verified_fee())
    assert repository.positions()[0]["status"] == "OPEN"
    worker.begin_generation(repository.next_generation())
    worker.process(quote(generation=3,bid="0.63",ask="0.7"),market,verified_fee())
    assert repository.positions()[0]["exit_price_text"] == "0.63"


def test_underlying_required_fresh_same_context(repository,market,rule):
    from demo_v2.domain.trading import MovementFilter
    spec=rule["spec"].model_copy(update={"underlying_filters":(MovementFilter(
        measurement="PERCENT",comparison="BETWEEN",direction="POSITIVE",lower="1",upper="2"),)})
    repository.update_rule(rule["id"],spec)
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee(),context(market,at=datetime.now(timezone.utc)-timedelta(seconds=7)))
    worker.process(quote(2),market,verified_fee(),context(market,"101.5"))
    assert len(repository.positions()) == 1
    assert "1.5" in repository.positions()[0]["entry_provenance_json"]


def test_entry_gate_does_not_block_trustworthy_exit(repository,market,rule):
    worker=engine(repository,market)
    worker.process(quote(),market,verified_fee())
    worker.entry_gate=lambda *_:False
    worker.process(quote(2,bid="0.63",ask="0.73"),market,verified_fee())
    assert repository.positions()[0]["status"] == "CLOSED"


def test_durable_generation_reset_invalidates_pending_actions(repository,market,rule):
    worker=engine(repository,market)
    repository.reset()
    worker.process(quote(),market,verified_fee())
    assert repository.positions() == []


@pytest.mark.parametrize("date,expected", [("2026-01-05T23:30:00",True),("2026-01-06T01:30:00",True),
    ("2026-01-06T02:00:00",False),("2026-01-05T13:00:00",True),("2026-01-07T01:00:00",False)])
def test_schedule_cross_midnight_multiple_ranges(rule,date,expected):
    from demo_v2.domain.trading import WeeklyRange
    spec=rule["spec"].model_copy(update={"weekly_schedule":(
        WeeklyRange(weekday=0,start="23:00",end="02:00"),WeeklyRange(weekday=0,start="12:00",end="14:00"))})
    assert scheduled(spec,datetime.fromisoformat(date).replace(tzinfo=ZoneInfo("Asia/Jerusalem"))) is expected


def test_schedule_dst_and_no_ranges(rule):
    from demo_v2.domain.trading import WeeklyRange
    spec=rule["spec"].model_copy(update={"weekly_schedule":(WeeklyRange(weekday=6,start="01:00",end="02:00"),)})
    for fold in (0,1):
        local=datetime(2026,10,25,1,30,tzinfo=ZoneInfo("Asia/Jerusalem"),fold=fold)
        assert scheduled(spec,local.astimezone(timezone.utc))
    assert scheduled(rule["spec"],datetime.now(timezone.utc))


@pytest.mark.parametrize("key,duration", [("BTC-5m",300),("SOL-15m",900),("ETH-1h",3600)])
def test_generic_market_windows_boundaries(market,key,duration):
    from demo_v2.domain.models import market_definitions
    from demo_v2.domain.trading import EntryWindow
    definition=next(d for d in market_definitions() if d.key==key)
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    instance=replace(market,definition=definition,starts_at=start,ends_at=start+timedelta(seconds=duration))
    spec=RuleSpec(name="windows",markets=(key,),entry_trigger="0.74",entry_windows=(
        EntryWindow(start_seconds=0,end_seconds=60),EntryWindow(start_seconds=duration-60,end_seconds=duration)))
    assert entry_eligible(spec,instance,start)
    assert not entry_eligible(spec,instance,start+timedelta(seconds=60))
    assert entry_eligible(spec,instance,start+timedelta(seconds=duration-60))
    assert not entry_eligible(spec,instance,instance.ends_at)
