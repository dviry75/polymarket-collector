import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from demo_v2.domain.trading import FeeState, MovementFilter
from demo_v2.providers.fees import FeeEvidence, GammaFeeProvider
from demo_v2.providers.underlying import ReferenceContext, ReferencePoint, movement_matches, BinanceResearchProvider


def verified_fee(instance_id="eth-hour", **changes):
    fields = dict(instance_id=instance_id, state=FeeState.VALID, observed_at=datetime.now(timezone.utc),
        source="EXPLICIT_FAKE_CONTRACT", rate=Decimal("0.02"), exponent=1,
        buy_units="SHARES", sell_units="CASH", rounding="HALF_UP", method="FAKE_V1")
    fields.update(changes)
    return FeeEvidence(**fields)


def context(market, price="101", at=None):
    return ReferenceContext(market.instance_id, market.definition.asset.value,
        ReferencePoint(Decimal(100),market.starts_at,"FAKE","ETHUSDT","RESEARCH"),
        ReferencePoint(Decimal(price),at or datetime.now(timezone.utc),"FAKE","ETHUSDT","RESEARCH"),"FAKE_ORACLE")


def test_fee_decimal_share_buy_and_net_share_sell():
    fee = verified_fee()
    amounts = fee.buy(Decimal(5),Decimal("0.5"))
    assert amounts.buy_fee == Decimal("0.02500")
    assert amounts.buy_fee_shares == Decimal("0.05")
    assert amounts.net_shares == Decimal("4.95")
    assert amounts.cash_debit == Decimal("2.5")
    assert fee.fee(amounts.net_shares,Decimal("0.8")) == Decimal("0.01584")


def test_cash_fee_does_not_reduce_shares():
    amounts = verified_fee(buy_units="CASH").buy(Decimal(5),Decimal("0.5"))
    assert amounts.net_shares == 5 and amounts.cash_debit == Decimal("2.525")


@pytest.mark.parametrize("state", [FeeState.UNKNOWN,FeeState.STALE,FeeState.UNSUPPORTED])
def test_numeric_zero_without_valid_evidence_is_not_valid(state):
    with pytest.raises(ValueError):
        verified_fee(state=state,rate=Decimal(0)).buy(Decimal(5),Decimal("0.5"))


def test_fee_freshness_rounding_and_unsupported_exponent():
    fee = verified_fee()
    assert fee.current(fee.observed_at+timedelta(seconds=301)).state == FeeState.STALE
    assert fee.current(fee.observed_at-timedelta(seconds=1)).state == FeeState.STALE
    assert replace(fee,rate=Decimal("0.00002")).fee(Decimal(5),Decimal("0.5")) == Decimal("0.00003")
    assert replace(fee,rate=Decimal("0.00002"),rounding="DOWN").fee(Decimal(5),Decimal("0.5")) == Decimal("0.00002")
    with pytest.raises(ValueError):
        replace(fee,exponent=2).fee(Decimal(5),Decimal("0.5"))


def test_current_gamma_parameters_do_not_invent_units(market):
    class HTTP:
        async def get(self, path):
            return dict(id=market.external_id,conditionId=market.condition_id,
                outcomes=["Down","Up"],clobTokenIds=[market.down_token_id,market.up_token_id],
                feesEnabled=True,feeSchedule={"rate":"0.02","exponent":1})
    # Fake market fixture uses readable token IDs; public metadata parser requires numeric IDs.
    market = replace(market,up_token_id="11",down_token_id="22")
    result = asyncio.run(GammaFeeProvider(HTTP()).parameters(market))
    assert result.state == FeeState.UNSUPPORTED and result.rate == Decimal("0.02")
    assert "UNVERIFIED" in result.reason


@pytest.mark.parametrize("measurement,direction,comparison,price,lower,upper,expected", [
    ("PERCENT","POSITIVE","GREATER_THAN","102","1",None,True),
    ("ABSOLUTE","NEGATIVE","GREATER_THAN","98","1",None,True),
    ("ABSOLUTE","POSITIVE","GREATER_THAN","98","1",None,False),
    ("PERCENT","ABSOLUTE","BETWEEN","98","1","2",True),
    ("PERCENT","ABSOLUTE","LESS_THAN","101","1",None,False),
    ("PERCENT","ABSOLUTE","GREATER_THAN","101","1",None,False),
])
def test_movement_contract(market, measurement,direction,comparison,price,lower,upper,expected):
    condition = MovementFilter(measurement=measurement,direction=direction,comparison=comparison,lower=lower,upper=upper)
    assert movement_matches(condition,context(market,price)) is expected


def test_reference_staleness_and_context_mismatch(market):
    now = datetime.now(timezone.utc)
    sample = context(market,at=now)
    assert sample.valid(market,now)
    assert not sample.valid(market,now+timedelta(seconds=6))
    assert not replace(sample,current=replace(sample.current,reference_id="OTHER")).valid(market,now)
    assert not replace(sample,baseline=replace(sample.baseline,observed_at=now)).valid(market,now)


def test_public_research_provider_validates_pair_candle_time_and_mapping(market):
    now = datetime.now(timezone.utc)
    class HTTP:
        async def get(self,path,params):
            if path.endswith("exchangeInfo"):
                return {"symbols":[dict(symbol="ETHUSDT",baseAsset="ETH",quoteAsset="USDT",status="TRADING")]}
            if path.endswith("klines"):
                return [[int(market.starts_at.timestamp()*1000),"100"]]
            return [{"price":"101","time":int(now.timestamp()*1000)}]
    async def run():
        provider=BinanceResearchProvider(HTTP(),clock=lambda:now)
        assert await provider.context(market,{"description":"unverified source"}) is None
        sample=await provider.context(market,{"description":"Binance 1 hour candle"})
        assert sample.valid(market,now)
        assert sample.is_research_proxy and sample.settlement_reference == "BINANCE_1H_CANDLE"
    asyncio.run(run())


def test_three_timeframe_consumers_share_timestamped_symbol_fetch(market):
    now=[datetime.now(timezone.utc)]
    calls=[]
    class HTTP:
        async def get(self,path,params):
            calls.append(path)
            await asyncio.sleep(0)
            if path.endswith("exchangeInfo"):
                return {"symbols":[dict(symbol="ETHUSDT",baseAsset="ETH",quoteAsset="USDT",status="TRADING")]}
            if path.endswith("klines"):
                return [[int(market.starts_at.timestamp()*1000),"100"]]
            return [{"price":"101","time":int(now[0].timestamp()*1000)}]
    async def run():
        provider=BinanceResearchProvider(HTTP(),clock=lambda:now[0])
        instances=[replace(market,instance_id=str(index)) for index in range(3)]
        metadata={"description":"Binance 1 hour candle"}
        samples=await asyncio.gather(*(provider.context(instance,metadata) for instance in instances))
        assert calls.count("/api/v3/trades")==1
        assert calls.count("/api/v3/exchangeInfo")==1
        assert all(sample.current.reference_id=="BINANCE_SPOT:ETHUSDT" for sample in samples)
        now[0]+=timedelta(seconds=2)
        await asyncio.gather(*(provider.context(instance,metadata) for instance in instances))
        assert calls.count("/api/v3/trades")==2
    asyncio.run(run())
