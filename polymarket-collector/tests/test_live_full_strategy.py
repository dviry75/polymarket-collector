import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from live.adapters.mock import MockTradingAdapter
from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.config import LiveConfig
from live.order_book import OrderBookSet
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository
from live.strategy import AllInBudget, StrategyPolicy, choose_entry, exact_trigger, simulate_buy_fak
from live.strategy_runtime import LiveStrategyRuntime
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
    assert out.out_of_order and books.books["yes"].ready
    assert books.books["yes"].best_ask == Decimal("0.75")
    books.mark_not_ready("RECONNECT_AWAITING_SNAPSHOT")
    assert not books.books["yes"].bids and not books.books["no"].asks
    assert books.event_ready(["yes", "no"])[0] is False


def test_five_dollar_all_in_rounding_fees_and_minimum():
    budget = AllInBudget(Decimal("5"))
    assert budget.sdk_buy_parameters() == {"amount": "3.8", "max_spend": "5"}
    result = simulate_buy_fak(
        [{"price": "0.74", "size": "100"}], max_price=Decimal("0.76"),
        max_spend=Decimal("5"), fee_rate=Decimal("0.07"),
        max_shares=Decimal("5"),
    )
    assert result.all_in <= Decimal("5")
    assert result.filled_shares <= Decimal("5")
    assert budget.minimum_viable(
        min_order_shares=Decimal("5"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    ) == (True, "VIABLE")
    assert budget.minimum_viable(
        min_order_shares=Decimal("100"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    )[0] is False

    assert budget.minimum_viable(
        min_order_shares=Decimal("5.000001"), maximum_price=Decimal("0.76"),
        maximum_fee_fraction=Decimal("0.07"),
    ) == (False, "MINIMUM_ORDER_EXCEEDS_5_TOKEN_CAP")

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
def test_canary_reservation_consumes_and_disarms_atomically_across_restart():
    temp, base, strategy = build_repo()
    try:
        base.set_state("canary_armed", "true", "test")
        base.set_state("canary_consumed", "false", "test")
        base.set_state("kill_switch", "false", "test")
        base.set_state("pause_entries", "false", "test")
        first = strategy.reserve_event_entry(
            event_id="canary-event", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT", consume_canary=True,
        )
        assert not first.get("_blocked")
        restarted = StrategyRepository(LiveRepository(base.db_path))
        assert base.get_state("canary_armed") == "false"
        assert base.get_state("canary_consumed") == "true"
        assert base.get_state("pause_entries") == "true"
        blocked = restarted.reserve_event_entry(
            event_id="second-event", condition_id="c2", token_id="no", side="NO",
            simultaneous=False, reason_code="ENTRY_PRICE_EXACT", consume_canary=True,
        )
        assert blocked == {"_blocked": True, "reason": "CANARY_NOT_AVAILABLE"}
        assert restarted.event_state("second-event") is None
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
        repeated = strategy.mark_position_resolved(
            loser["position_id"], winner=False, redeem_pending=False
        )
        assert repeated["state"] == "RESOLVED_LOSER"
        with strategy.base.connect() as conn:
            daily = conn.execute(
                "SELECT realized_pnl_usd,consecutive_losing_deals FROM live_daily_limits"
            ).fetchone()
            deal = conn.execute(
                "SELECT state,final_reason FROM live_strategy_deals WHERE event_id='loser'"
            ).fetchone()
        assert Decimal(str(daily["realized_pnl_usd"])) == Decimal("-5")
        assert daily["consecutive_losing_deals"] == 1
        assert deal["state"] == "RESOLVED_LOSER"
        assert deal["final_reason"] == "MARKET_RESOLUTION"
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
        live_kill_switch_default=False, funder_address=FakeSecureClient.wallet, signature_type=1,
    )


def test_adapter_buy_fak_max_spend_max_price_and_no_auto_approval():
    fake = FakeSecureClient()
    adapter = RealPolymarketTradingAdapter(armed_config(), secure_client=fake)
    result = asyncio.run(adapter.create_order({
        "durable_intent_reserved": True, "token_id": "token", "side": "BUY",
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
    }))
    assert result["status"] == "matched"
    assert fake.limit_calls == [{
        "token_id": "token", "price": "0.76", "size": "5", "side": "BUY",
    }]
    assert fake.posted[0]["order_type"] == "FAK"
    assert fake.market_calls == []
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
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
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
        "order_type": "FAK", "requested_amount_usd": "3.8", "max_spend": "5",
        "max_price": "0.76", "max_tokens": "5",
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


def test_paper_stop_gtc_partial_then_cancel_and_emergency_fak_closes_position():
    temp, base, strategy = build_repo()
    try:
        base.upsert_market({
            "event_id": "event-stop",
            "condition_id": "condition-event-stop",
            "yes_token_id": "token-event-stop",
            "no_token_id": "token-no",
            "token_mapping_status": "verified",
            "accepting_orders": True,
            "min_order_size": 1,
            "min_tick_size": 0.01,
            "taker_base_fee": 0,
        })
        position = reserve_and_open(
            strategy, event="event-stop", shares=Decimal("5"), minimum=Decimal("1")
        )
        tp = strategy.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.96"),
            book_hash="entry",
        )
        strategy.update_intent(tp["intent_id"], state="LIVE")
        position = strategy.position_for_token("token-event-stop")
        runtime = LiveStrategyRuntime(
            LiveConfig(
                live_module_enabled=True,
                execution_mode="PAPER_TRADING",
                paper_trading_enabled=True,
            ),
            base,
            strategy,
            MockTradingAdapter(),
        )
        asyncio.run(runtime._place_stop_loss(
            position,
            {
                "asset_id": "token-event-stop",
                "best_bid": "0.66",
                "bids": [{"price": "0.66", "size": "2"}],
            },
            frame_hash="stop-book",
        ))
        partial = strategy.position_for_token("token-event-stop")
        assert partial["remaining_shares_text"] == "3"
        assert partial["active_exit_intent_id"]
        stop_intent = strategy.intent(partial["active_exit_intent_id"])
        assert stop_intent["order_type"] == "GTC"
        assert stop_intent["price_limit_text"] == "0.55"
        assert stop_intent["state"] == "PARTIAL"

        restarted_strategy = StrategyRepository(LiveRepository(base.db_path))
        restarted_strategy.migrate()
        runtime = LiveStrategyRuntime(
            LiveConfig(
                live_module_enabled=True,
                execution_mode="PAPER_TRADING",
                paper_trading_enabled=True,
            ),
            base,
            restarted_strategy,
            MockTradingAdapter(),
        )
        partial = restarted_strategy.position_for_token("token-event-stop")
        asyncio.run(runtime._emergency_exit(
            partial,
            {
                "asset_id": "token-event-stop",
                "best_bid": "0.60",
                "bids": [{"price": "0.60", "size": "3"}],
            },
            purpose="EMERGENCY_060",
            min_price=Decimal("0.55"),
            frame_hash="emergency-book",
        ))
        closed = strategy.position_for_token("token-event-stop")
        assert closed["state"] == "CLOSED"
        assert closed["remaining_shares_text"] == "0"
        assert strategy.intent(stop_intent["intent_id"])["state"] == "CANCELED"
    finally:
        temp.cleanup()


def test_emergency_cancel_failure_never_submits_parallel_sell():
    class CancelFailureAdapter(MockTradingAdapter):
        def __init__(self):
            super().__init__()
            self.create_calls = []

        async def cancel_order(self, order_id):
            return {"success": False, "status": "unknown"}

        async def create_order(self, order):
            self.create_calls.append(order)
            return await super().create_order(order)

    temp, base, strategy = build_repo()
    try:
        position = reserve_and_open(
            strategy, event="cancel-failure", shares=Decimal("5"), minimum=Decimal("1")
        )
        active = strategy.reserve_position_intent(
            position,
            action="EXIT",
            purpose="STOP_066",
            order_type="GTC",
            shares=Decimal("5"),
            price_limit=Decimal("0.55"),
            book_hash="stop",
        )
        strategy.update_intent(
            active["intent_id"], state="LIVE", remote_order_id="remote-stop"
        )
        position = strategy.position_for_token("token-cancel-failure")
        adapter = CancelFailureAdapter()
        runtime = LiveStrategyRuntime(
            LiveConfig(execution_mode="READ_ONLY"),
            base,
            strategy,
            adapter,
        )
        asyncio.run(runtime._emergency_exit(
            position,
            {
                "asset_id": "token-cancel-failure",
                "best_bid": "0.60",
                "bids": [{"price": "0.60", "size": "5"}],
            },
            purpose="EMERGENCY_060",
            min_price=Decimal("0.55"),
            frame_hash="emergency",
        ))
        assert adapter.create_calls == []
        assert strategy.intent(active["intent_id"])["state"] == "CANCEL_UNCERTAIN"
        assert strategy.position_for_token("token-cancel-failure")[
            "state"
        ] == "EXIT_RECONCILIATION_REQUIRED"
    finally:
        temp.cleanup()


def test_reconciliation_settles_entry_fill_and_position_across_restart():
    temp, base, strategy = build_repo()
    try:
        base.upsert_market({
            "event_id": "recovered-entry",
            "condition_id": "recovered-condition",
            "yes_token_id": "recovered-token",
            "no_token_id": "recovered-no",
            "token_mapping_status": "verified",
            "accepting_orders": True,
            "min_order_size": 5,
        })
        reservation = strategy.reserve_event_entry(
            event_id="recovered-entry", condition_id="recovered-condition",
            token_id="recovered-token", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        intent_id = reservation["entry_intent_id"]
        strategy.update_intent(intent_id, state="RECONCILIATION_REQUIRED", remote_order_id="r1")
        adapter = MockTradingAdapter()
        adapter.orders["r1"] = {
            "status": "filled",
            "fills": [{
                "polymarket_order_id": "r1", "polymarket_trade_id": "trade-r1",
                "price": "0.74", "size": "5", "fee": "0.02", "status": "matched",
            }],
        }
        adapter.positions = [{
            "condition_id": "recovered-condition", "token_id": "recovered-token",
            "outcome": "YES", "size": "5", "average_price": "0.74",
            "current_value": "3.7",
        }]
        restarted = StrategyRepository(LiveRepository(base.db_path))
        result = asyncio.run(
            ReconciliationWorker(base, adapter, restarted).run_once("restart-test")
        )
        assert result["status"] == "ok"
        position = restarted.position_for_token("recovered-token")
        assert position["remaining_shares_text"] == "5"
        assert position["cost_all_in_text"] == "3.72"
        assert restarted.intent(intent_id)["state"] == "FILLED"
    finally:
        temp.cleanup()


def test_reconciliation_failure_forces_kill_pause_and_disarms_canary():
    class FailingAdapter(MockTradingAdapter):
        async def get_balance(self):
            raise RuntimeError("Secret Manager unavailable")

    temp, base, strategy = build_repo()
    try:
        base.set_state("kill_switch", "false", "test")
        base.set_state("canary_armed", "true", "test")
        strategy.set_pause_entries(False, "test", "test")
        result = asyncio.run(
            ReconciliationWorker(base, FailingAdapter(), strategy).run_once("test")
        )
        assert result["status"] == "failed"
        assert base.get_state("kill_switch") == "true"
        assert base.get_state("canary_armed") == "false"
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_strategy_daily_loss_limit_locks_real_entry_path():
    temp, base, strategy = build_repo()
    try:
        config = LiveConfig(max_daily_realized_loss_usd=Decimal("10"))
        runtime = LiveStrategyRuntime(config, base, strategy, MockTradingAdapter())
        day_key = datetime.now(ZoneInfo("Asia/Jerusalem")).date().isoformat()
        base.current_daily_limit(day_key, "Asia/Jerusalem")
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_daily_limits SET realized_pnl_usd='-10' WHERE day_key=?",
                (day_key,),
            )
            conn.commit()
        base.set_state("kill_switch", "false", "test")
        base.set_state("canary_armed", "true", "test")
        strategy.set_pause_entries(False, "test", "test")
        assert runtime._daily_loss_blocked()
        assert base.get_state("kill_switch") == "true"
        assert base.get_state("canary_armed") == "false"
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_configured_signer_mismatch_fails_before_network_client_creation():
    class MemorySecrets:
        values = {
            "POLYMARKET_PRIVATE_KEY": "0x" + "11" * 32,
            "POLYMARKET_API_KEY": "fake-api-key",
            "POLYMARKET_API_SECRET": "fake-api-secret",
            "POLYMARKET_API_PASSPHRASE": "fake-passphrase",
        }

        def get_secret(self, name):
            return self.values.get(name)

    config = LiveConfig(
        private_signing_readiness_enabled=True,
        signer_address="0x2222222222222222222222222222222222222222",
        funder_address="0x3333333333333333333333333333333333333333",
        signature_type=3,
    )
    result = asyncio.run(
        RealPolymarketTradingAdapter(
            config, secret_provider=MemorySecrets()
        ).identity_preflight()
    )
    assert result["status"] == "SIGNER_MISMATCH"
    assert result["signer"] is None


def test_entry_schedule_blocks_weekdays_14_to_23_jerusalem():
    jerusalem = ZoneInfo("Asia/Jerusalem")
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 13, 59, 59, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 14, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert not LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 22, 59, 59, tzinfo=jerusalem)
    )["allowed"]
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 3, 23, 0, 0, tzinfo=jerusalem)
    )["allowed"]
    assert LiveStrategyRuntime.entry_schedule_status(
        datetime(2026, 8, 8, 16, 0, 0, tzinfo=jerusalem)
    )["allowed"]


def test_continuous_entry_slot_is_atomic_for_intents_and_positions():
    temporary, base, strategy = build_repo()
    try:
        first = strategy.reserve_event_entry(
            event_id="event-slot-1", condition_id="condition-slot-1",
            token_id="token-slot-1", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert not first.get("_blocked")
        blocked_by_intent = strategy.reserve_event_entry(
            event_id="event-slot-2", condition_id="condition-slot-2",
            token_id="token-slot-2", side="NO", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert blocked_by_intent["reason"] == "ACTIVE_ENTRY_SLOT_OCCUPIED"
        strategy.open_position(
            event_id="event-slot-1", condition_id="condition-slot-1",
            token_id="token-slot-1", outcome="YES", shares=Decimal("5"),
            average_price=Decimal("0.74"), cost_all_in=Decimal("3.8"),
            fees=Decimal("0"), min_sellable=Decimal("1"),
        )
        blocked_by_position = strategy.reserve_event_entry(
            event_id="event-slot-3", condition_id="condition-slot-3",
            token_id="token-slot-3", side="YES", simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT", require_empty_slot=True,
        )
        assert blocked_by_position["reason"] == "ACTIVE_ENTRY_SLOT_OCCUPIED"
    finally:
        temporary.cleanup()


def test_critical_074_frame_survives_regular_frame_conflation():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32
        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0
        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0
        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(ask, number):
            return {
                "event_type": "price_change",
                "message_hash": f"frame-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": ask,
                    "best_bid": "0.73",
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        # No await between these calls: this recreates a burst in which the
        # normal queue would otherwise collapse 0.73 -> 0.74 -> 0.75.
        runtime.schedule_frame(frame("0.73", 1))
        runtime.schedule_frame(frame("0.74", 2))
        runtime.schedule_frame(frame("0.75", 3))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        asks = [
            context["updates"][0]["best_ask"]
            for context in processed
        ]

        # Critical 0.74 is evaluated first and survives; ordinary state
        # collapses to the latest 0.75 frame.
        assert asks == ["0.74", "0.75"]
        assert processed[0]["_critical_trigger"] is True
        assert (
            processed[0]["updates"][0]["_critical_trigger_latched"]
            is True
        )

        assert runtime.critical_triggers_queued == 1
        assert runtime.critical_triggers_processed == 1
        assert runtime.critical_triggers_dropped == 0
        assert runtime.frames_coalesced == 1

    asyncio.run(scenario())


def test_latched_critical_trigger_is_not_rejected_as_frame_superseded():
    runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

    runtime.config = type(
        "CriticalTriggerConfig",
        (),
        {"max_market_data_age_seconds": 2},
    )()

    runtime._market_freshness = lambda _condition_id: {
        "ready": True,
        "reason": "READY",
        "book_versions": {
            "yes-token": {
                "generation": 1,
                "update_number": 3,
            }
        },
    }

    runtime.paper_mode = lambda: False

    trigger_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    latched = runtime._freshness(
        "condition-1",
        {
            "asset_id": "yes-token",
            "generation": 1,
            "update_number": 2,
            "exchange_timestamp_ms": trigger_ms,
            "_critical_trigger_latched": True,
        },
    )

    assert latched["ready"] is True
    assert latched["critical_trigger_latched"] is True
    assert latched["critical_trigger_age_ms"] <= 2000

    regular = runtime._freshness(
        "condition-1",
        {
            "asset_id": "yes-token",
            "generation": 1,
            "update_number": 2,
            "exchange_timestamp_ms": trigger_ms,
        },
    )

    assert regular["ready"] is False
    assert regular["reason"] == "FRAME_SUPERSEDED"



def test_stop_and_emergency_exact_prices_survive_conflation():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32

        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0

        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0

        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(bid, number):
            return {
                "event_type": "price_change",
                "message_hash": f"frame-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": "0.80",
                    "best_bid": bid,
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        # No awaits: recreate an ingress burst.
        runtime.schedule_frame(frame("0.67", 1))
        runtime.schedule_frame(frame("0.66", 2))

        # Still 0.66: must NOT create another critical edge.
        runtime.schedule_frame(frame("0.66", 3))

        runtime.schedule_frame(frame("0.65", 4))
        runtime.schedule_frame(frame("0.60", 5))
        runtime.schedule_frame(frame("0.59", 6))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        critical = [
            context for context in processed
            if context.get("_critical_trigger")
        ]

        assert len(critical) == 2

        assert critical[0]["_critical_trigger_types"] == [
            "STOP_066"
        ]
        assert (
            critical[0]["updates"][0]["_critical_stop_latched"]
            is True
        )

        assert critical[1]["_critical_trigger_types"] == [
            "EMERGENCY_060"
        ]
        assert (
            critical[1]["updates"][0][
                "_critical_emergency_latched"
            ]
            is True
        )

        # Exact 0.66 appearing on two consecutive frames is one edge,
        # not two critical queue entries.
        assert runtime.critical_triggers_queued == 2
        assert runtime.critical_triggers_processed == 2
        assert runtime.critical_triggers_dropped == 0

        # Latest ordinary state still survives independently.
        assert processed[-1]["updates"][0]["best_bid"] == "0.59"

    asyncio.run(scenario())


def test_entry_exact_price_is_edge_triggered_not_level_triggered():
    async def scenario():
        runtime = LiveStrategyRuntime.__new__(LiveStrategyRuntime)

        runtime.policy = StrategyPolicy()
        runtime._pending_frames = __import__("collections").OrderedDict()
        runtime._critical_frames = __import__("collections").deque()
        runtime._critical_price_state = {}
        runtime._frame_queue_capacity = 32

        runtime.frames_coalesced = 0
        runtime.frames_dropped = 0

        runtime.critical_triggers_queued = 0
        runtime.critical_triggers_processed = 0
        runtime.critical_triggers_dropped = 0
        runtime.max_critical_queue_depth = 0

        runtime._frame_event = asyncio.Event()
        runtime._frame_task = None
        runtime._stop = asyncio.Event()

        runtime._observe_entry_trigger = lambda _context: None
        runtime.enabled = lambda: True

        processed = []

        async def fake_process(context):
            processed.append(context)

        runtime.process_atomic_frame = fake_process

        def frame(ask, number):
            return {
                "event_type": "price_change",
                "message_hash": f"entry-{number}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "updates": [{
                    "condition_id": "condition-1",
                    "asset_id": "yes-token",
                    "outcome": "YES",
                    "best_ask": ask,
                    "best_bid": "0.73",
                    "generation": 1,
                    "update_number": number,
                    "exchange_timestamp_ms": int(
                        datetime.now(timezone.utc).timestamp() * 1000
                    ),
                }],
                "event_readiness": {
                    "condition-1": {
                        "ready": True,
                        "reason": "READY",
                    }
                },
            }

        runtime.schedule_frame(frame("0.73", 1))
        runtime.schedule_frame(frame("0.74", 2))
        runtime.schedule_frame(frame("0.74", 3))
        runtime.schedule_frame(frame("0.74", 4))

        runtime._stop.set()
        runtime._frame_event.set()

        await asyncio.wait_for(runtime._frame_task, timeout=1)

        critical = [
            context for context in processed
            if context.get("_critical_trigger")
        ]

        assert len(critical) == 1
        assert critical[0]["_critical_trigger_types"] == [
            "ENTRY_074"
        ]
        assert runtime.critical_triggers_queued == 1
        assert runtime.critical_triggers_dropped == 0

    asyncio.run(scenario())


def test_hot_state_snapshot_combines_safety_lock_and_exposure():
    temp, base, strategy = build_repo()

    try:
        base.set_state("pause_entries", "false", "test")
        base.set_state("kill_switch", "false", "test")
        base.set_state("canary_armed", "true", "test")
        base.set_state(
            "reconciliation_readiness",
            "READY",
            "test",
        )

        reserve_and_open(
            strategy,
            event="hot-state-event",
            shares=Decimal("10"),
        )

        snapshot = strategy.hot_state_snapshot()

        assert snapshot["pause_entries"] is False
        assert snapshot["kill_switch"] is False
        assert snapshot["canary_armed"] is True
        assert (
            snapshot["reconciliation_readiness"]
            == "READY"
        )
        assert (
            "hot-state-event"
            in snapshot["locked_event_ids"]
        )
        assert snapshot["active_exposure"] == Decimal("5")

    finally:
        temp.cleanup()


def test_stale_ram_entry_state_fails_closed():
    runtime = LiveStrategyRuntime.__new__(
        LiveStrategyRuntime
    )

    runtime._hot_state = {
        "pause_entries": False,
        "kill_switch": False,
        "canary_armed": True,
        "canary_consumed": False,
        "reconciliation_readiness": "READY",
        "locked_event_ids": set(),
        "active_exposure": Decimal("0"),
    }

    runtime._hot_state_max_age_seconds = 1.0
    runtime._hot_state_refreshed_monotonic = (
        __import__("time").monotonic() - 2.0
    )

    state = runtime._entry_state_from_ram("event-1")

    assert state["stale"] is True
    assert state["paused"] is True
    assert state["event_locked"] is False
    assert state["active_exposure"] == Decimal("0")


def test_entry_decision_hot_path_does_not_query_durable_state():
    inspect = __import__("inspect")

    source = (
        inspect.getsource(
            LiveStrategyRuntime._observe_entry_trigger
        )
        + inspect.getsource(
            LiveStrategyRuntime._process_event
        )
    )

    forbidden = (
        "self.repo.pause_entries()",
        "self.base.kill_switch_active()",
        'self.base.get_state("canary_armed"',
        "self.repo.event_state(",
        "self.repo.exposure()",
    )

    for item in forbidden:
        assert item not in source

    # Daily-loss SQLite work is now trigger-only.
    process_source = inspect.getsource(
        LiveStrategyRuntime._process_event
    )

    assert "if has_trigger" in process_source
    assert "self._daily_loss_blocked()" in process_source


def test_hot_state_snapshot_contains_active_position_and_tp_state():
    temp, base, strategy = build_repo()

    try:
        position = reserve_and_open(
            strategy,
            event="ram-position-event",
            shares=Decimal("10"),
        )

        intent = strategy.reserve_position_intent(
            position,
            action="TP",
            purpose="TAKE_PROFIT",
            order_type="GTC",
            shares=Decimal("10"),
            price_limit=Decimal("0.96"),
            book_hash="ram-test",
        )

        strategy.update_intent(
            str(intent["intent_id"]),
            state="LIVE",
        )

        snapshot = strategy.hot_state_snapshot()

        positions = snapshot[
            "positions_by_token"
        ]["token-ram-position-event"]

        assert len(positions) == 1

        cached = positions[0]

        assert (
            cached["position_id"]
            == position["position_id"]
        )
        assert (
            cached["tp_intent_id"]
            == intent["intent_id"]
        )
        assert cached["tp_intent_state"] == "LIVE"

    finally:
        temp.cleanup()


def test_manage_position_neutral_frame_uses_ram_only():
    runtime = LiveStrategyRuntime.__new__(
        LiveStrategyRuntime
    )

    runtime.policy = StrategyPolicy()

    runtime._hot_state = {
        "reconciliation_readiness": "READY",
        "positions_by_token": {
            "token-1": [{
                "position_id": "position-1",
                "event_id": "event-1",
                "condition_id": "condition-1",
                "token_id": "token-1",
                "outcome": "YES",
                "state": "TP_OPEN",
                "remaining_shares_text": "5",
                "sellable_shares_text": "5",
                "stop_stage": 0,
                "tp_intent_id": "tp-1",
                "tp_intent_state": "LIVE",
                "active_exit_intent_id": None,
            }]
        },
    }

    runtime.paper_mode = lambda: False

    class ExplodingRepo:
        def __getattr__(self, name):
            raise AssertionError(
                f"Unexpected DB repository read: {name}"
            )

    runtime.repo = ExplodingRepo()

    asyncio.run(
        runtime._manage_position(
            market={},
            update={
                "asset_id": "token-1",
                "best_bid": "0.70",
            },
            event_ready=True,
            frame_hash="neutral-frame",
        )
    )


def test_manage_position_has_no_unconditional_position_db_reads():
    inspect = __import__("inspect")

    source = inspect.getsource(
        LiveStrategyRuntime._manage_position
    )

    forbidden = (
        "self.repo.active_positions(",
        "self.repo.position_for_token(",
        "self.repo.intent(",
        'self.base.get_state("reconciliation_readiness"',
    )

    for item in forbidden:
        assert item not in source
