import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.config import LiveConfig
from live.order_book import OrderBookSet
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository
from live.strategy import AllInBudget, StrategyPolicy, choose_entry, exact_trigger, simulate_buy_fak
from live.strategy_repository import StrategyRepository, sanitize


def build_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate()
    return temporary, base, strategy


def reserve_and_open(strategy, event="event-1", shares=Decimal("10"), minimum=Decimal("5")):
    strategy.reserve_event_entry(
        event_id=event, condition_id=f"condition-{event}", token_id=f"token-{event}",
        side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    return strategy.open_position(
        event_id=event, condition_id=f"condition-{event}", token_id=f"token-{event}",
        outcome="YES", shares=shares, average_price=Decimal("0.74"),
        cost_all_in=Decimal("5"), fees=Decimal("0.05"), min_sellable=minimum,
    )


def test_exact_trigger_never_crosses_and_window_is_exact():
    assert exact_trigger("0.74", Decimal("0.74"))
    assert not exact_trigger("0.740001", Decimal("0.74"))
    assert not exact_trigger("0.75", Decimal("0.74"))
    policy = StrategyPolicy()
    observed = datetime.fromtimestamp(1180, timezone.utc)
    allowed = choose_entry(
        updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=observed,
        event_id="btc-updown-5m-1000", policy=policy,
    )
    assert allowed.allowed and allowed.side == "YES"
    ended = choose_entry(
        updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=datetime.fromtimestamp(1300, timezone.utc),
        event_id="btc-updown-5m-1000", policy=policy,
    )
    assert ended.reason == "EVENT_ENDED"


def test_simultaneous_trigger_skips_and_first_separate_update_wins():
    simultaneous = choose_entry(
        updates=[
            {"asset_id": "yes", "best_ask": "0.74"},
            {"asset_id": "no", "best_ask": "0.74"},
        ], yes_token_id="yes", no_token_id="no", event_ready=True, paused=False,
        event_locked=False, active_exposure=Decimal("0"),
        observed_at=datetime.fromtimestamp(1180, timezone.utc),
        event_id="btc-updown-5m-1000",
    )
    assert not simultaneous.allowed and simultaneous.simultaneous
    assert simultaneous.reason == "SKIPPED_SIMULTANEOUS_TRIGGER"
    first = choose_entry(
        updates=[{"asset_id": "no", "best_ask": "0.74"}], yes_token_id="yes",
        no_token_id="no", event_ready=True, paused=False, event_locked=False,
        active_exposure=Decimal("0"), observed_at=datetime.fromtimestamp(1180, timezone.utc),
        event_id="btc-updown-5m-1000",
    )
    assert first.allowed and first.side == "NO"


def test_order_book_snapshot_delta_delete_duplicate_out_of_order_reconnect_two_tokens():
    books = OrderBookSet(["yes", "no"])
    first = books.apply({
        "event_type": "book", "asset_id": "yes", "timestamp": "100",
        "bids": [{"price": "0.70", "size": "8"}, {"price": "0.69", "size": "9"}],
        "asks": [{"price": "0.74", "size": "7"}, {"price": "0.75", "size": "6"}],
    })
    books.apply({
        "event_type": "book", "asset_id": "no", "timestamp": "100",
        "bids": [{"price": "0.24", "size": "8"}],
        "asks": [{"price": "0.26", "size": "7"}],
    })
    assert first.updates[0]["asks"][1] == {"price": "0.75", "size": "6"}
    assert books.event_ready(["yes", "no"]) == (True, "READY")
    delta = {
        "event_type": "price_change", "timestamp": "101",
        "price_changes": [{
            "asset_id": "yes", "side": "SELL", "price": "0.75", "size": "10",
            "best_bid": "0.70", "best_ask": "0.74",
        }],
    }
    applied = books.apply(delta)
    assert applied.updates[0]["asks"] == [
        {"price": "0.74", "size": "7"}, {"price": "0.75", "size": "10"}
    ]
    assert books.apply(delta).duplicate
    deleted = books.apply({
        "event_type": "price_change", "timestamp": "102",
        "price_changes": [{
            "asset_id": "yes", "side": "SELL", "price": "0.74", "size": "0",
            "best_bid": "0.70", "best_ask": "0.75",
        }],
    })
    assert deleted.updates[0]["best_ask"] == "0.75"
    out = books.apply({
        "event_type": "price_change", "timestamp": "99",
        "price_changes": [{"asset_id": "yes", "side": "BUY", "price": "0.71", "size": "1"}],
    })
    assert out.out_of_order and not books.books["yes"].ready
    books.mark_not_ready("RECONNECT_AWAITING_SNAPSHOT")
    assert not books.books["yes"].bids and not books.books["no"].asks
    assert books.event_ready(["yes", "no"])[0] is False


def test_five_dollar_all_in_rounding_fees_and_minimum():
    budget = AllInBudget(Decimal("5"))
    assert budget.sdk_buy_parameters() == {"amount": "5", "max_spend": "5"}
    result = simulate_buy_fak(
        [{"price": "0.74", "size": "100"}], max_price=Decimal("0.76"),
        max_spend=Decimal("5"), fee_rate=Decimal("0.07"),
    )
    assert result.all_in <= Decimal("5")
    assert result.filled_shares > Decimal("5")
    assert budget.minimum_viable(
        min_order_shares=Decimal("5"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    ) == (True, "VIABLE")
    assert budget.minimum_viable(
        min_order_shares=Decimal("100"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    )[0] is False


def test_event_lock_zero_fill_survives_restart_and_is_unique():
    temp, base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="e", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
        )
        assert not first.get("_duplicate")
        strategy.mark_zero_fill("e", "FAK_ZERO_FILL")
        restarted = StrategyRepository(LiveRepository(base.db_path))
        restarted.migrate()
        duplicate = restarted.reserve_event_entry(
            event_id="e", condition_id="c", token_id="no", side="NO",
            simultaneous=False, reason_code="SECOND_SIDE",
        )
        assert duplicate["_duplicate"] and duplicate["locked_side"] == "YES"
        assert restarted.intent(first["entry_intent_id"])["state"] == "ZERO_FILL"
    finally:
        temp.cleanup()


def test_atomic_simultaneous_lock_has_no_order_intent():
    temp, base, strategy = build_repo()
    try:
        state = strategy.reserve_event_entry(
            event_id="e", condition_id="c", token_id=None, side=None,
            simultaneous=True, reason_code="SKIPPED_SIMULTANEOUS_TRIGGER",
        )
        assert state["status"] == "SKIPPED_SIMULTANEOUS_TRIGGER"
        assert state["entry_intent_id"] is None
        assert base.list_table("live_strategy_intents", 10) == []
    finally:
        temp.cleanup()


def test_partial_tp_cancel_race_no_oversell_and_dust():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("10"))
        tp = strategy.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
            shares=Decimal("10"), price_limit=Decimal("0.96"), book_hash="entry",
        )
        partial = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=tp["intent_id"],
            sold_shares=Decimal("3"), average_price=Decimal("0.96"), fees=Decimal("0.01"),
            final_state="PARTIAL", min_sellable=Decimal("5"), purpose="TAKE_PROFIT",
            book_hash="book-1",
        )
        assert partial["state"] == "TP_OPEN" and partial["remaining_shares_text"] == "7"
        cancel = strategy.cancel_tp(position["position_id"], "STOP_066")
        assert cancel and cancel["state"] == "CANCEL_REQUESTED"
        strategy.finalize_cancel(tp["intent_id"], True, "CANCEL_ACK")
        refreshed = strategy.position_for_token(position["token_id"])
        exit_intent = strategy.reserve_position_intent(
            refreshed, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("7"), price_limit=Decimal("0.55"), book_hash="book-2",
        )
        closed = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=exit_intent["intent_id"],
            sold_shares=Decimal("100"), average_price=Decimal("0.60"), fees=Decimal("0"),
            final_state="FILLED", min_sellable=Decimal("5"), purpose="STOP_066",
            book_hash="book-2",
        )
        assert closed["remaining_shares_text"] == "0" and closed["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_partial_emergency_allows_new_book_only_and_never_parallel():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("12"))
        intent = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("12"), price_limit=Decimal("0.55"), book_hash="book-a",
        )
        duplicate = strategy.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("12"), price_limit=Decimal("0.55"), book_hash="book-a",
        )
        assert duplicate["_duplicate"]
        partial = strategy.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=Decimal("6"), average_price=Decimal("0.60"), fees=Decimal("0"),
            final_state="PARTIAL_FINAL", min_sellable=Decimal("5"), purpose="STOP_066",
            book_hash="book-a",
        )
        second = strategy.reserve_position_intent(
            partial, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("6"), price_limit=Decimal("0.55"), book_hash="book-b",
        )
        assert not second.get("_duplicate") and second["intent_id"] != intent["intent_id"]
    finally:
        temp.cleanup()


def test_partial_entry_under_minimum_is_dust_and_does_not_count_exposure():
    temp, _base, strategy = build_repo()
    try:
        position = reserve_and_open(strategy, shares=Decimal("3"), minimum=Decimal("5"))
        assert position["state"] == "DUST"
        assert position["sellable_shares_text"] == "0"
        assert position["dust_shares_text"] == "3"
        assert strategy.exposure() == 0
        assert strategy.active_positions() == []
        assert strategy.unresolved_positions()[0]["position_id"] == position["position_id"]
    finally:
        temp.cleanup()


def test_resolution_winner_redeem_loser_zero_and_noop_closed():
    temp, _base, strategy = build_repo()
    try:
        winner = reserve_and_open(strategy, event="winner", shares=Decimal("6"))
        resolved = strategy.mark_position_resolved(winner["position_id"], winner=True, redeem_pending=True)
        assert resolved["state"] == "REDEEM_PENDING"
        redeemed = strategy.mark_position_redeemed(winner["position_id"], "tx-public")
        assert redeemed["state"] == "REDEEMED" and redeemed["remaining_shares_text"] == "0"
        loser = reserve_and_open(strategy, event="loser", shares=Decimal("6"))
        zero = strategy.mark_position_resolved(loser["position_id"], winner=False, redeem_pending=False)
        assert zero["state"] == "RESOLVED_LOSER"
        assert Decimal(zero["realized_pnl_text"]) == Decimal("-5")
        assert strategy.unresolved_positions() == []
    finally:
        temp.cleanup()


def test_exposure_cap_across_events_and_dust_exception():
    temp, _base, strategy = build_repo()
    try:
        reserve_and_open(strategy, event="one", shares=Decimal("6"))
        decision = choose_entry(
            updates=[{"asset_id": "yes", "best_ask": "0.74"}], yes_token_id="yes",
            no_token_id="no", event_ready=True, paused=False, event_locked=False,
            active_exposure=strategy.exposure(), observed_at=datetime.fromtimestamp(1180, timezone.utc),
            event_id="btc-updown-5m-1000",
        )
        assert decision.reason == "EXPOSURE_CAP"
    finally:
        temp.cleanup()


def test_reconciliation_clean_then_missing_remote_position_fails_closed():
    temp, base, strategy = build_repo()
    try:
        adapter = MockTradingAdapter()
        worker = ReconciliationWorker(base, adapter, strategy)
        clean = asyncio.run(worker.run_once("test"))
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
        strategy.set_pause_entries(False, "test", "READY")
        reserve_and_open(strategy, shares=Decimal("6"))
        gap = asyncio.run(worker.run_once("test"))
        assert gap["status"] == "gaps"
        assert any(item["type"] == "local_position_missing_remote" for item in gap["gaps"])
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_reconciliation_recovers_remote_position_then_requires_clean_pass():
    temp, base, strategy = build_repo()
    try:
        base.upsert_market({
            "event_id": "remote-event", "condition_id": "remote-condition",
            "yes_token_id": "remote-token", "no_token_id": "remote-no",
            "token_mapping_status": "verified", "accepting_orders": True,
        })
        adapter = MockTradingAdapter()
        adapter.positions = [{
            "condition_id": "remote-condition", "token_id": "remote-token", "outcome": "YES",
            "size": "7", "average_price": "0.72", "current_value": "4.9",
        }]
        worker = ReconciliationWorker(base, adapter, strategy)
        corrected = asyncio.run(worker.run_once("test"))
        assert corrected["status"] == "gaps"
        assert strategy.position_for_token("remote-token")["remaining_shares_text"] == "7"
        clean = asyncio.run(worker.run_once("test"))
        assert clean["status"] == "ok"
        assert base.get_state("reconciliation_readiness") == "READY"
    finally:
        temp.cleanup()


class FakeSecureClient:
    wallet = "0x2222222222222222222222222222222222222222"
    signer = "0x1111111111111111111111111111111111111111"
    wallet_type = "POLY_PROXY"

    def __init__(self, allowance=10_000_000):
        self.allowance = allowance
        self.market_calls = []
        self.limit_calls = []
        self.posted = []
        self._ctx = type("Context", (), {"secure_clob": type("Transport", (), {
            "post_json": staticmethod(lambda *args, **kwargs: asyncio.sleep(0, result={"status": "ok"}))
        })()})()

    async def get_balance_allowance(self, **kwargs):
        return {"balance": 10_000_000, "allowances": {"exchange": self.allowance}}

    async def create_market_order(self, **kwargs):
        self.market_calls.append(kwargs)
        return {"signed": True, "kind": "market"}

    async def create_limit_order(self, **kwargs):
        self.limit_calls.append(kwargs)
        return {"signed": True, "kind": "limit"}

    async def post_order(self, signed):
        self.posted.append(signed)
        return {"ok": True, "status": "matched", "order_id": "remote-1", "trade_ids": ["trade-1"]}

    async def cancel_order(self, **kwargs):
        return {"canceled": [kwargs["order_id"]], "not_canceled": {}}

    async def cancel_orders(self, **kwargs):
        return {"canceled": list(kwargs["order_ids"]), "not_canceled": {}}

    async def cancel_market_orders(self, **kwargs):
        return {"canceled": ["relevant"], "not_canceled": {}}


def armed_config():
    return LiveConfig(
        trading_mode="LIVE", execution_mode="REAL_TRADING", live_module_enabled=True,
        live_trading_enabled=True, live_order_submission_enabled=True,
        live_adapter="polymarket", pause_entries_default=False, canary_armed=True,
        funder_address=FakeSecureClient.wallet, signature_type=1,
    )


def test_adapter_buy_fak_max_spend_max_price_and_no_auto_approval():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    result = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "5", "max_spend": "5",
        "max_price": "0.76",
    }))
    assert result["status"] == "matched"
    assert fake.market_calls == [{
        "token_id": "token", "side": "BUY", "amount": "5", "max_spend": "5",
        "max_price": "0.76", "order_type": "FAK",
    }]
    assert len(fake.posted) == 1


def test_adapter_sell_fak_gtc_targeted_cancel_and_heartbeat():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    sold = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "SELL",
        "order_type": "FAK", "requested_size": "6", "min_price": "0.55",
    }))
    assert sold["success"]
    assert fake.market_calls[0]["shares"] == "6" and fake.market_calls[0]["min_price"] == "0.55"
    asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "SELL",
        "order_type": "GTC", "requested_size": "6", "requested_price": "0.96",
    }))
    assert fake.limit_calls[0]["price"] == "0.96"
    assert asyncio.run(adapter.cancel_order("remote-1"))["success"]
    assert asyncio.run(adapter.cancel_market_orders("condition", "token"))["success"]
    assert asyncio.run(adapter.cancel_all_orders())["failure_reason"] == "GLOBAL_CANCEL_ALL_PROHIBITED"
    assert asyncio.run(adapter.heartbeat())["success"]


def test_adapter_insufficient_allowance_blocks_before_sign_and_post():
    fake = FakeSecureClient(allowance=0)
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    result = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "5", "max_spend": "5",
        "max_price": "0.76",
    }))
    assert result["status"] == "blocked"
    assert "APPROVAL_REQUIRED" in result["failure_reason"]
    assert fake.market_calls == [] and fake.posted == []


class FakeResponseClient(FakeSecureClient):
    def __init__(self, response):
        super().__init__()
        self.response = response

    async def post_order(self, signed):
        self.posted.append(signed)
        await asyncio.sleep(0)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _adapter_entry_order():
    return {
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "5", "max_spend": "5",
        "max_price": "0.76",
    }


def test_adapter_delayed_pending_partial_rejected_and_unknown_responses_never_retry():
    cases = [
        ({"ok": True, "status": "delayed", "order_id": "delayed-1"}, True, "delayed"),
        ({"ok": True, "status": "pending", "order_id": "pending-1"}, True, "pending"),
        ({"ok": True, "status": "matched", "order_id": "partial-1",
          "making_amount": "3", "taking_amount": "2.22"}, True, "matched"),
        ({"ok": False, "code": "order_rejected", "message": "rejected"}, False, "rejected"),
        ({"ok": True}, True, "unknown"),
    ]
    for response, expected_success, expected_status in cases:
        fake = FakeResponseClient(response)
        result = asyncio.run(
            RealPolymarketTradingAdapter(armed_config(), secure_client=fake).create_order(
                _adapter_entry_order()
            )
        )
        assert result["success"] is expected_success
        assert result["status"] == expected_status
        assert len(fake.posted) == 1


def test_adapter_transport_timeout_is_uncertain_and_never_retried():
    fake = FakeResponseClient(asyncio.TimeoutError("SDK read timeout"))
    result = asyncio.run(
        RealPolymarketTradingAdapter(armed_config(), secure_client=fake).create_order(
            _adapter_entry_order()
        )
    )
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert "TimeoutError" in result["failure_reason"]
    assert len(fake.posted) == 1


def test_adapter_fail_closed_when_not_armed_or_without_durable_intent():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(LiveConfig(), secure_client=fake)
    blocked = asyncio.run(adapter.create_order({"token_id": "token"}))
    assert blocked["failure_reason"] == "REAL_SUBMISSION_NOT_ARMED"
    armed = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    durable = asyncio.run(armed.create_order({"token_id": "token"}))
    assert durable["failure_reason"] == "DURABLE_INTENT_REQUIRED"


def test_recursive_masking_covers_payloads_and_error_strings():
    masked = sanitize({
        "private_key": "never-show", "nested": {"passphrase": "never-show-2"},
        "error": "Authorization: BearerNever API_SECRET=hidden safe-text",
        "token_id": "public-token-id",
    })
    rendered = str(masked)
    assert "never-show" not in rendered and "BearerNever" not in rendered and "hidden" not in rendered
    assert masked["token_id"] == "public-token-id"
