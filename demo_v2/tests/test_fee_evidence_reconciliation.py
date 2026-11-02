"""Fee regressions: explicit fake contracts and public metadata, never LIVE rows."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from demo_v2.domain.models import OfficialResolution
from demo_v2.domain.trading import FeeState, LiquidityRole, RuleSpec
from demo_v2.engine import DemoEngine
from demo_v2.providers.fees import FeeEvidence, GammaFeeProvider, ROUNDING_BLOCKER, VIRTUAL_EXECUTION
from conftest import quote


def fake_cash_fee(instance_id="eth-hour", **changes):
    # DOWN is a test-only declared convention, not an asserted operator policy.
    fields = dict(instance_id=instance_id, state=FeeState.VALID,
        observed_at=datetime.now(timezone.utc), source="EXPLICIT_FAKE_CASH_CONTRACT",
        rate=Decimal("0.07"), exponent=1, buy_units="CASH", sell_units="CASH",
        rounding="DOWN", taker_only=True, minimum_fee=Decimal("0.00001"))
    return FeeEvidence(**(fields | changes))


def parameters(market, **changes):
    market = replace(market, up_token_id="11", down_token_id="22")
    raw = dict(id=market.external_id, conditionId=market.condition_id, version="v1",
        outcomes=["Up", "Down"], clobTokenIds=["11", "22"], feesEnabled=True,
        feeSchedule={"rate":"0.07", "exponent":1, "takerOnly":True})
    raw.update(changes)
    class HTTP:
        async def get(self, path):
            assert path == "/markets/" + market.external_id
            return raw
    return asyncio.run(GammaFeeProvider(HTTP()).parameters(market))


def test_taker_buy_and_sell_cash_examples_keep_all_shares():
    fee = fake_cash_fee()
    buy = fee.buy(Decimal(100), Decimal("0.50"), liquidity=LiquidityRole.TAKER)
    assert buy.notional == 50 and buy.buy_fee == Decimal("1.75")
    assert buy.cash_debit == Decimal("51.75")
    assert buy.net_shares == buy.gross_shares == 100 and buy.buy_fee_shares == 0
    sell_fee = fee.fee(buy.net_shares, Decimal("0.50"), liquidity=LiquidityRole.TAKER)
    assert sell_fee == Decimal("1.75")
    assert buy.net_shares * Decimal("0.50") - sell_fee == Decimal("48.25")


def test_maker_zero_requires_explicit_taker_only_and_never_uses_taker_formula():
    fee = fake_cash_fee(rounding="UNKNOWN")
    assert fee.fee(Decimal(100), Decimal("0.5"), liquidity=LiquidityRole.MAKER) == 0
    buy = fee.buy(Decimal(100), Decimal("0.5"), liquidity=LiquidityRole.MAKER)
    assert buy.net_shares == 100 and buy.cash_debit == 50
    with pytest.raises(ValueError, match="unsupported fee contract"):
        fee.fee(Decimal(100), Decimal("0.5"), liquidity=LiquidityRole.TAKER)
    for policy in (None, False):
        with pytest.raises(ValueError, match="maker fee policy"):
            replace(fee, taker_only=policy).fee(Decimal(100), Decimal("0.5"), liquidity=LiquidityRole.MAKER)


@pytest.mark.parametrize("bad_role", ["UNKNOWN", "", "maker"])
def test_unknown_role_cannot_silently_become_taker_or_free_maker(bad_role):
    with pytest.raises(ValueError):
        fake_cash_fee().fee(Decimal(100), Decimal("0.5"), liquidity=bad_role)


@pytest.mark.parametrize("state", [FeeState.UNKNOWN, FeeState.STALE, FeeState.UNSUPPORTED])
def test_unknown_evidence_never_becomes_zero_even_for_maker(state):
    fee = fake_cash_fee(state=state, rate=Decimal(0))
    for role in LiquidityRole:
        with pytest.raises(ValueError, match="not valid"):
            fee.fee(Decimal(100), Decimal("0.5"), liquidity=role)


def test_current_public_configuration_stays_closed_with_precise_rounding_reason(market):
    fee = parameters(market)
    assert fee.state == FeeState.UNSUPPORTED and fee.reason == ROUNDING_BLOCKER
    assert fee.buy_units == fee.sell_units == "CASH" and fee.taker_only is True
    assert fee.rounding == "UNKNOWN" and fee.quantum == fee.minimum_fee == Decimal("0.00001")
    assert fee.execution_model == VIRTUAL_EXECUTION
    assert fee.aggregation == "ONCE_PER_VIRTUAL_EXECUTION"
    with pytest.raises(ValueError):
        fee.buy(Decimal(100), Decimal("0.5"))


@pytest.mark.parametrize("policy", [None, False, "true", 1])
def test_missing_false_or_malformed_maker_policy_is_not_assumed(market, policy):
    fee = parameters(market, feeSchedule={"rate":"0.07", "exponent":1, "takerOnly":policy})
    assert fee.state == FeeState.UNSUPPORTED and fee.reason == "MAKER_TAKER_FEE_POLICY_UNVERIFIED"


@pytest.mark.parametrize("enabled", [None, True])
def test_historical_verified_zero_and_external_fill_fee_are_not_authority(market, enabled):
    fee = parameters(market, feesEnabled=enabled, feeSchedule={},
        fee_rate_bps=0, fee=0, fee_usd=0, fee_verification_status="VERIFIED", trader_side="MAKER")
    assert fee.state == FeeState.UNKNOWN and fee.reason == "FEE_PARAMETERS_MISSING"
    with pytest.raises(ValueError):
        fee.buy(Decimal(100), Decimal("0.5"))


def test_legacy_zero_does_not_override_current_schedule_and_only_explicit_disabled_is_valid_zero(market):
    fee = parameters(market, fee_rate_bps=0, fee=0, fee_verification_status="VERIFIED")
    assert fee.state == FeeState.UNSUPPORTED and fee.rate == Decimal("0.07")
    disabled = parameters(market, feesEnabled=False)
    assert disabled.state == FeeState.VALID and disabled.rate == 0
    assert disabled.method == "EXPLICIT_FEES_DISABLED"
    assert disabled.buy(Decimal(100), Decimal("0.5")).cash_debit == 50


@pytest.mark.parametrize("shares,down,half_up", [
    ("4.99999", "0.00346", "0.00346"),
    ("5", "0.00346", "0.00347"),
    ("5.00001", "0.00346", "0.00347"),
])
def test_tie_and_near_tie_distinguish_candidates_without_choosing_public_policy(shares, down, half_up):
    fee = fake_cash_fee()
    assert fee.fee(Decimal(shares), Decimal("0.01")) == Decimal(down)
    assert replace(fee, rounding="HALF_UP").fee(Decimal(shares), Decimal("0.01")) == Decimal(half_up)


def test_minimum_fee_applies_before_rounding_up_and_invalid_inputs_are_rejected():
    fee = fake_cash_fee(rounding="HALF_UP")
    # Raw fee 0.0000098 is below the documented minimum even though HALF_UP
    # alone would produce 0.00001. Exact threshold: 5 * .000008 * .5 * .5.
    assert replace(fee, rate=Decimal("0.00000784")).fee(Decimal(5), Decimal("0.5")) == 0
    assert replace(fee, rate=Decimal("0.000008")).fee(Decimal(5), Decimal("0.5")) == Decimal("0.00001")
    for shares, price in [("-1", "0.5"), ("NaN", "0.5"), ("1", "Infinity"), ("1", "1.01")]:
        with pytest.raises(ValueError):
            fee.fee(Decimal(shares), Decimal(price))


def test_virtual_execution_is_once_and_real_match_splitting_is_a_fidelity_limit():
    fee = fake_cash_fee(rounding="HALF_UP")
    virtual = fee.fee(Decimal(5), Decimal("0.01"))
    split = 2 * fee.fee(Decimal("2.5"), Decimal("0.01"))
    assert virtual == Decimal("0.00347") and split == Decimal("0.00346")
    assert virtual != split  # No claim of exact multi-match order replication.


@pytest.mark.parametrize("bid,ask,reason,sell_fee,net", [
    ("0.8", "0.81", "TAKE_PROFIT", "1.12", "78.88"),
    ("0.4", "0.41", "STOP_LOSS", "1.68", "38.32"),
])
def test_engine_tp_and_sl_are_taker_and_persist_cash_accounting_and_execution_provenance(
        repository, market, bid, ask, reason, sell_fee, net):
    repository.create_rule(RuleSpec(name="cash fee", markets=(market.definition.key,),
        entry_trigger="0.5", gross_shares="100", take_profit="0.8", stop_loss="0.45"))
    worker = DemoEngine(repository)
    worker.begin_generation(repository.next_generation())
    fee = fake_cash_fee()
    worker.process(quote(ask="0.5", bid="0.49"), market, fee)
    opened = repository.positions()[0]
    assert Decimal(opened["cash_debit_text"]) == Decimal("51.75")
    assert Decimal(opened["net_shares_text"]) == 100 and repository.wallet() == Decimal("-51.75")
    worker.process(quote(2, ask=ask, bid=bid), market, fee)
    closed = repository.positions()[0]
    assert closed["exit_reason"] == reason and Decimal(closed["sell_fee_text"]) == Decimal(sell_fee)
    assert Decimal(closed["net_proceeds_text"]) == Decimal(net)
    assert repository.wallet() == Decimal(net) - Decimal("51.75")
    evidence = json.loads(closed["fee_evidence_json"])
    assert evidence["taker_only"] is True and evidence["aggregation"] == "ONCE_PER_VIRTUAL_EXECUTION"
    for field in ("entry_provenance_json", "exit_provenance_json"):
        execution = json.loads(closed[field])["execution"]
        assert execution["liquidity_role"] == "TAKER" and execution["model"] == VIRTUAL_EXECUTION
    with repository.database.connect() as conn:
        ledger = conn.execute("SELECT amount_text,provenance_json FROM demo_v2_ledger ORDER BY movement_type").fetchall()
        assert all(json.loads(row["provenance_json"])["execution"]["liquidity_role"] == "TAKER" for row in ledger)


@pytest.mark.parametrize("winner,payout", [("eth-up", 100), ("eth-down", 0)])
def test_resolution_uses_all_shares_with_no_fabricated_sell_fee(repository, market, winner, payout):
    repository.create_rule(RuleSpec(name="resolve cash", markets=(market.definition.key,), entry_trigger="0.5", gross_shares="100"))
    worker = DemoEngine(repository)
    worker.begin_generation(repository.next_generation())
    worker.process(quote(ask="0.5", bid="0.49"), market, fake_cash_fee())
    repository.resolve(OfficialResolution(market.instance_id, winner, datetime.now(timezone.utc), "EXPLICIT_FAKE_RESOLUTION"))
    closed = repository.positions()[0]
    assert Decimal(closed["net_proceeds_text"]) == payout and Decimal(closed["sell_fee_text"]) == 0
    assert closed["exit_reason"] == "MARKET_RESOLUTION"
    assert repository.wallet() == Decimal(payout) - Decimal("51.75")


def test_runtime_reports_exact_remaining_blocker_without_enabling_entries(repository, market):
    from test_runtime import runtime
    fee = replace(fake_cash_fee(), state=FeeState.UNSUPPORTED, rounding="UNKNOWN", reason=ROUNDING_BLOCKER)
    class Fees:
        async def parameters(self, instance):
            return fee
    from demo_v2.providers.contracts import Providers
    worker = runtime(repository, providers=Providers(fees=Fees()))
    worker.instances[market.instance_id] = market
    worker.current[market.definition.key] = market.instance_id
    asyncio.run(worker.fees_once())
    snapshot = worker.health.snapshot()
    component = next(c for c in snapshot.components if c.component == "fee_provider")
    assert component.reason == ROUNDING_BLOCKER and not snapshot.entries_allowed
    assert worker.fees[market.instance_id].state == FeeState.UNSUPPORTED


def test_new_public_snapshot_records_taker_only_for_all_21_families(market):
    from demo_v2.providers.discovery import labeled_tokens
    captured = json.loads((Path(__file__).parent / "fixtures/fee_contract_20261102.json").read_text())
    assert len(captured["markets"]) == len({r["family"] for r in captured["markets"]}) == 21
    assert not captured["issues"]
    for row in captured["markets"]:
        assert row["version"] == "v1" and row["feesEnabled"] is True
        assert row["feeSchedule"]["takerOnly"] is True
        assert Decimal(row["feeSchedule"]["rate"]) == Decimal("0.07")
        up, down = labeled_tokens(row)
        instance = replace(market, instance_id=row["conditionId"], external_id=row["id"],
                           condition_id=row["conditionId"], up_token_id=up, down_token_id=down)
        class HTTP:
            async def get(self, path):
                assert path == "/markets/" + row["id"]
                return row
        fee = asyncio.run(GammaFeeProvider(HTTP()).parameters(instance))
        assert fee.state == FeeState.UNSUPPORTED and fee.reason == ROUNDING_BLOCKER
        assert fee.taker_only is True
