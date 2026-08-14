from pathlib import Path
import asyncio
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.pause_recovery import PauseRecoveryCoordinator
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


class FakeHealth:
    def __init__(self, value):
        self.value = value

    def health(self):
        return self.value


def build_clean():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate(pause_entries_default=False)
    base.set_states({
        "kill_switch": "false",
        "strategy_readiness": "READY",
        "strategy_block_reason": "",
        "reconciliation_readiness": "READY",
        "reconciliation_block_reason": "",
        "live_blocked_by_reconciliation": "false",
    }, "operator")
    market = FakeHealth({
        "status": "CONNECTED", "stale": False,
        "subscribed_asset_ids": ["yes"],
        "books": {"yes": {"ready": True, "exchange_age_ms": 100}},
    })
    user = FakeHealth({"status": "CONNECTED", "stale": False})
    return temporary, base, strategy, market, user, PauseRecoveryCoordinator(
        base, strategy, market, user, freshness_limit_ms=1000
    )


def test_machine_pause_clean_recovery_auto_resumes():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_pause_entries(True, "ws", "MARKET_WS_DOWN", owner="MACHINE", auto_recoverable=True)
        result = recovery.attempt_auto_resume()
        assert result.resumed
        assert not strategy.pause_entries()
        assert base.get_state("pause_owner") == "NONE"
    finally:
        temp.cleanup()


def test_operator_pause_clean_recovery_stays_paused():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
        assert not recovery.attempt_auto_resume().resumed
        assert strategy.pause_entries()
        assert base.get_state("pause_owner") == "OPERATOR"
    finally:
        temp.cleanup()


def test_kill_switch_clean_recovery_stays_killed_and_paused():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_pause_entries(True, "ws", "MARKET_WS_DOWN", owner="MACHINE", auto_recoverable=True)
        base.set_state("kill_switch", "true", "operator")
        result = recovery.attempt_auto_resume()
        assert "KILL_SWITCH_ACTIVE" in result.blockers
        assert strategy.pause_entries()
        assert base.kill_switch_active()
    finally:
        temp.cleanup()


def test_unknown_financial_state_never_auto_resumes():
    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_pause_entries(True, "ws", "USER_WS_DOWN", owner="MACHINE", auto_recoverable=True)
        strategy.reserve_event_entry(
            event_id="e", condition_id="c", token_id="yes", side="YES",
            simultaneous=False, reason_code="TEST_UNKNOWN_INTENT",
        )
        result = recovery.attempt_auto_resume()
        assert "UNRESOLVED_INTENT" in result.blockers
        assert "UNKNOWN_ORDER_FILL_OR_CANCELLATION" in result.blockers
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_reconciliation_429_hold_clean_reconciliation_auto_resumes():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False, reason="RECONCILIATION_RATE_LIMITED", actor="reconciliation"
        )
        assert base.get_state("pause_owner") == "RECONCILIATION"
        assert base.get_state("pause_auto_recoverable") == "true"
        strategy.set_reconciliation_state(ready=True, reason="", actor="reconciliation")
        assert recovery.attempt_auto_resume().resumed
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_market_ws_disconnect_reconnect_ready_fresh_auto_resumes():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["status"] = "DISCONNECTED"
        market.value["stale"] = True
        base.set_state("strategy_readiness", "NOT_READY", "market_ws")
        recovery.tick()
        assert strategy.pause_entries()
        assert base.get_state("pause_reason") == "MARKET_WS_DOWN"
        market.value["status"] = "CONNECTED"
        market.value["stale"] = False
        market.value["books"]["yes"] = {"ready": True, "exchange_age_ms": 999}
        base.set_state("strategy_readiness", "READY", "market_ws")
        assert recovery.tick().resumed
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_user_ws_disconnect_reconnect_auto_resumes():
    temp, base, strategy, _market, user, recovery = build_clean()
    try:
        user.value["status"] = "DISCONNECTED"
        recovery.tick()
        assert strategy.pause_entries()
        assert base.get_state("pause_reason") == "USER_WS_DOWN"
        user.value["status"] = "CONNECTED"
        assert recovery.tick().resumed
    finally:
        temp.cleanup()


def test_connected_stale_market_recovers_only_below_one_second():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["stale"] = True
        market.value["books"]["yes"]["exchange_age_ms"] = 1500
        base.set_state("strategy_readiness", "NOT_READY", "market_ws")
        recovery.tick()
        assert base.get_state("pause_reason") == "MARKET_DATA_STALE"
        market.value["stale"] = False
        market.value["books"]["yes"]["exchange_age_ms"] = 1001
        base.set_state("strategy_readiness", "READY", "market_ws")
        assert not recovery.tick().resumed
        market.value["books"]["yes"]["exchange_age_ms"] = 1000
        assert recovery.tick().resumed
    finally:
        temp.cleanup()


def test_book_not_ready_resync_then_ready_auto_resumes():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["books"]["yes"] = {"ready": False, "exchange_age_ms": 50}
        base.set_state("strategy_readiness", "NOT_READY", "market_ws")
        recovery.tick()
        assert base.get_state("pause_reason") == "BOOK_NOT_READY"
        market.value["books"]["yes"] = {"ready": True, "exchange_age_ms": 50}
        base.set_state("strategy_readiness", "READY", "market_ws")
        assert recovery.tick().resumed
    finally:
        temp.cleanup()


def test_non_operator_cannot_mutate_kill_switch():
    import pytest

    temp, base, _strategy, _market, _user, _recovery = build_clean()
    try:
        with pytest.raises(PermissionError, match="operator-owned"):
            base.set_state("kill_switch", "true", "machine")
        assert not base.kill_switch_active()
    finally:
        temp.cleanup()


def test_temporary_network_api_error_clean_pass_auto_resumes():
    class TimeoutAdapter(MockTradingAdapter):
        async def get_balance(self):
            raise TimeoutError("temporary API network timeout")

    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        failed = asyncio.run(
            ReconciliationWorker(base, TimeoutAdapter(), strategy).run_once("reconciliation")
        )
        assert failed["status"] == "failed"
        assert base.get_state("pause_reason") == "RECONCILIATION_TEMPORARY_ERROR"
        assert base.get_state("pause_auto_recoverable") == "true"
        clean = asyncio.run(
            ReconciliationWorker(base, MockTradingAdapter(), strategy).run_once("reconciliation")
        )
        assert clean["status"] == "ok"
        assert recovery.attempt_auto_resume().resumed
    finally:
        temp.cleanup()
