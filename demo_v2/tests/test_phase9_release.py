"""Offline regressions using the public Phase 9 evidence captured 2026-10-30."""

import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from demo_v2.domain.models import Asset,Timeframe,MarketDefinition,MarketInstance
from demo_v2.domain.trading import RuleSpec,FeeState
from demo_v2.providers.discovery import GammaDiscovery,candidate_family,labeled_tokens
from demo_v2.providers.fees import FeeEvidence,GammaFeeProvider
from demo_v2.providers.subscriptions import subscription_demand
from demo_v2.providers.underlying import source_mapping
from test_runtime import runtime
import demo_v2.runtime as runtime_module


EVIDENCE=json.loads((Path(__file__).parent/"fixtures/phase9_public_evidence.json").read_text(),parse_float=Decimal)
MARKETS=EVIDENCE["matrix"]["markets"]


def model(raw):
    fields=dict(raw)
    definition=fields.pop("definition")
    definition={**definition,"asset":Asset(definition["asset"]),"timeframe":Timeframe(definition["timeframe"])}
    for key in ("starts_at","ends_at","discovered_at"):
        fields[key]=datetime.fromisoformat(fields[key])
    return MarketInstance(definition=MarketDefinition(**definition),**fields)


@pytest.mark.parametrize("row",MARKETS,ids=lambda row:row["family"])
def test_captured_public_family_identity_labels_duration_and_next(row):
    assert row["status"]=="VERIFIED"
    current=model(row["current"][0]);following=model(row["next"][0])
    assert labeled_tokens(row["market_metadata"])==(current.up_token_id,current.down_token_id)
    assert current.starts_at<current.ends_at==following.starts_at<following.ends_at
    assert (current.ends_at-current.starts_at).total_seconds()==current.definition.expected_duration
    assert current.condition_id!=following.condition_id
    assert not {current.up_token_id,current.down_token_id}&{following.up_token_id,following.down_token_id}
    assert any(item.get("resolution") and item["closed"] and item["uma_status"]=="resolved" for item in row["historical_resolution"])
    assert source_mapping(current,row["market_metadata"])==tuple(row["source_mapping"])


@pytest.mark.parametrize("row",MARKETS,ids=lambda row:row["family"])
def test_rollover_demand_from_captured_public_times_retains_old_held_token(row):
    current=model(row["current"][0]);following=model(row["next"][0])
    spec=RuleSpec(name="rollover",markets=(current.definition.key,),entry_trigger="0.74")
    rule={"id":"fake-rule","spec":spec}
    position={"status":"OPEN","instance_id":current.instance_id,"token_id":current.up_token_id}
    wanted=subscription_demand([current,following],[rule],[position],following.starts_at,lambda *args:True)
    assert {instance.instance_id for instance in wanted}=={current.instance_id,following.instance_id}
    assert subscription_demand([current,following],[],[],following.starts_at,lambda *args:True)==()


def test_sol_hourly_uses_verified_solana_series_in_discovery():
    row=next(item for item in MARKETS if item["family"]=="SOL-1h")
    current=model(row["current"][0]);following=model(row["next"][0])
    assert candidate_family(current.definition)=="solana-up-or-down-hourly"
    assert current.definition.family_id=="10122"
    class HTTP:
        async def get(self,path,params=None):
            if path=="/series":
                return [{"id":"10122","slug":"solana-up-or-down-hourly","recurrence":"hourly"}]
            assert params["series_id"]=="10122"
            events=[]
            for instance in (current,following):
                market={**row["market_metadata"],"question":"Solana Up or Down",
                    "id":instance.external_id,"conditionId":instance.condition_id,
                    "clobTokenIds":json.dumps([instance.up_token_id,instance.down_token_id]),"outcomes":'["Up","Down"]',
                    "eventStartTime":instance.starts_at.isoformat(),"endDate":instance.ends_at.isoformat()}
                events.append({"series":[{"id":"10122","slug":"solana-up-or-down-hourly"}],"markets":[market]})
            return events
    provider=GammaDiscovery(HTTP(),clock=lambda:datetime.fromisoformat(EVIDENCE["matrix"]["observed_at"]))
    result=asyncio.run(provider.discover([current.definition]))
    assert len(result)==2 and not provider.issues


@pytest.mark.parametrize("version",["v2","v3","unknown"])
def test_unsupported_protocol_cannot_select_legacy_ids_just_because_present(version):
    raw={**MARKETS[0]["market_metadata"],"version":version,"positionIds":["11","22"]}
    with pytest.raises(ValueError,match="protocol"):
        labeled_tokens(raw)


@pytest.mark.parametrize("example",EVIDENCE["fee_contract"]["non_boundary_examples"])
def test_current_official_fee_examples_without_inventing_tie_policy(example):
    # These exact products need no tie rounding. They are examples, not authority
    # for activating either HALF_UP or DOWN in the real public provider.
    shares,price,rate=map(Decimal,(example["shares"],example["price"],example["rate"]))
    assert shares*rate*price*(1-price)==Decimal(example["fee"])
    for convention in ("HALF_UP","DOWN"):
        fee=FeeEvidence("fake-example",FeeState.VALID,datetime.now(timezone.utc),"OFFICIAL_NON_BOUNDARY_EXAMPLE",
            rate,1,"CASH","CASH",convention)
        assert fee.fee(shares,price)==Decimal(example["fee"])


def test_verified_cash_units_do_not_promote_unverified_operator_rounding():
    row=MARKETS[0]
    instance=model(row["current"][0])
    class HTTP:
        async def get(self,path): return row["market_metadata"]
    evidence=asyncio.run(GammaFeeProvider(HTTP()).parameters(instance))
    assert evidence.state==FeeState.UNSUPPORTED
    assert evidence.buy_units==evidence.sell_units=="CASH"
    assert evidence.rate==Decimal(row["market_metadata"]["feeSchedule"]["rate"])
    assert evidence.rounding=="UNKNOWN"
    assert evidence.reason=="CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED"
    with pytest.raises(ValueError): evidence.buy(Decimal(100),Decimal("0.5"))


def test_cash_debit_example_matches_current_contract_without_share_deduction():
    fee=FeeEvidence("fake-example",FeeState.VALID,datetime.now(timezone.utc),"OFFICIAL_NON_BOUNDARY_EXAMPLE",
        Decimal("0.07"),1,"CASH","CASH","DOWN")
    buy=fee.buy(Decimal(100),Decimal("0.5"))
    assert buy.cash_debit==Decimal("51.75") and buy.net_shares==100 and buy.buy_fee_shares==0
    assert buy.net_shares*Decimal("0.5")-fee.fee(buy.net_shares,Decimal("0.5"))==Decimal("48.25")


def test_current_rate_has_a_real_unresolved_tie_and_fill_aggregation_case():
    from decimal import ROUND_DOWN,ROUND_HALF_UP
    rate=Decimal(MARKETS[0]["market_metadata"]["feeSchedule"]["rate"])
    raw=Decimal(5)*rate*Decimal("0.01")*Decimal("0.99")
    assert raw==Decimal("0.003465")
    quantum=Decimal("0.00001")
    assert raw.quantize(quantum,rounding=ROUND_DOWN)==Decimal("0.00346")
    assert raw.quantize(quantum,rounding=ROUND_HALF_UP)==Decimal("0.00347")
    assert (raw/2).quantize(quantum,rounding=ROUND_HALF_UP)*2==Decimal("0.00346")


def test_hype_hourly_futures_authority_stays_distinct_from_spot_research():
    row=next(item for item in MARKETS if item["family"]=="HYPE-1h")
    assert "/futures/" in row["market_metadata"]["resolutionSource"]
    assert source_mapping(model(row["current"][0]),row["market_metadata"])[1]=="BINANCE_USDM_1H_CANDLE"
    sample=next(c for a in EVIDENCE["matrix"]["underlying"] for c in a["contexts"] if c["family"]=="HYPE-1h")["evidence"]
    assert sample["is_research_proxy"] and sample["current"]["reference_type"]=="SPOT_USDT_RESEARCH"


def test_legacy_fee_rate_is_not_the_current_curve_rate():
    clob=EVIDENCE["matrix"]["clob"]
    legacy=next(row for row in clob if row["path"]=="/fee-rate")
    current=next(row for row in clob if row["path"].startswith("/clob-markets/"))
    assert json.loads(legacy["body_excerpt"])["base_fee"]==1000
    assert Decimal(current["fee_parameters"]["r"])==Decimal(MARKETS[0]["market_metadata"]["feeSchedule"]["rate"])


def test_runtime_does_not_construct_or_call_a_clob_http_client(repository,monkeypatch):
    origins=[]
    class HTTP:
        async def get(self,path,params=None):
            if path=="/series": return []
            raise ValueError("no unexpected public calls in isolated probe")
        async def close(self): pass
    def public_http(origin):
        assert origin!="https://clob.polymarket.com"
        origins.append(origin)
        return HTTP()
    class Data:
        status="WAITING_FOR_DEMAND"
        def __init__(self,*args,**kwargs): self.stop=asyncio.Event()
        async def run(self): await self.stop.wait()
        async def set_subscriptions(self,instances): assert not instances
        async def close(self): self.stop.set()
    monkeypatch.setattr(runtime_module,"PublicHTTP",public_http)
    monkeypatch.setattr(runtime_module,"PublicMarketWS",Data)
    worker=runtime(repository,public_data_enabled=True)
    async def run():
        await worker.start()
        try:
            await asyncio.sleep(0.01)
            assert worker.database_ok and worker.engine_ok
        finally:
            await worker.close()
    asyncio.run(run())
    assert origins==["https://gamma-api.polymarket.com","https://api.binance.com"]


def test_captured_public_observation_cannot_be_replayed_after_aging():
    from demo_v2.domain.models import MarketObservation
    from demo_v2.streams import DecisionStream,ObservationRejected
    from datetime import timedelta
    raw=dict(EVIDENCE["public_e2e"]["observations"][0])
    for field in ("observed_at","received_at"):
        raw[field]=datetime.fromisoformat(raw[field])
    for field in ("best_bid","best_ask"):
        raw[field]=Decimal(raw[field]) if raw[field] is not None else None
    observation=MarketObservation(**raw)
    stream=DecisionStream(clock=lambda:observation.received_at+timedelta(seconds=6))
    stream.begin_generation(observation.generation)
    with pytest.raises(ObservationRejected,match="NOT_FRESH"):
        stream.publish(observation)
