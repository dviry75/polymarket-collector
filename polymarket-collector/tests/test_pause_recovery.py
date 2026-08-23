from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.pause_recovery import (
    PauseRecoveryCoordinator,
    request_manual_resume,
)
from live.reconciliation import ReconciliationWorker
from live.recovery_policy import (
    PauseState,
    ReleasePolicy,
    recovery_policy,
)
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


class FakeHealth:
    def __init__(self, value):
        self.value = value

    def health(self):
        return self.value


def build_clean(config: LiveConfig | None = None):
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate(pause_entries_default=False)
    now = now_iso()
    base.set_states({
        "kill_switch": "false",
        "strategy_readiness": "READY",
        "strategy_block_reason": "",
        "reconciliation_readiness": "READY",
        "reconciliation_block_reason": "",
        "live_blocked_by_reconciliation": "false",
        "last_successful_reconciliation_at": now,
        "geographic_availability": "ALLOWED",
        "geographic_checked_at": now,
        "order_heartbeat_status": "OK",
        "last_successful_heartbeat_at": now,
        "recovery_engine_status": "HEALTHY",
    }, "operator")
    market = FakeHealth({
        "status": "CONNECTED", "stale": False,
        "subscribed_asset_ids": ["yes"],
        "books": {
            "yes": {
                "ready": True,
                "reason": "READY",
                "exchange_age_ms": 100,
            }
        },
    })
    user = FakeHealth({
        "status": "CONNECTED",
        "stale": False,
        "last_message_at": now,
    })
    config = config or LiveConfig(
        pause_entries_default=False,
        live_kill_switch_default=False,
        recovery_stability_seconds=0.01,
        recovery_detection_debounce_seconds=0,
    )
    recovery = PauseRecoveryCoordinator(
        base, strategy, market, user, config=config
    )
    return temporary, base, strategy, market, user, recovery


def complete_stability(base: LiveRepository, recovery: PauseRecoveryCoordinator):
    first = recovery.attempt_auto_resume()
    assert not first.resumed
    assert "STABILITY_WINDOW" in first.blockers
    base.set_state(
        "pause_eligible_since",
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "test",
    )
    return recovery.attempt_auto_resume()


def clean_reconciliation(
    strategy: StrategyRepository, run_id: int
) -> str:
    finished_at = now_iso()
    strategy.set_reconciliation_state(
        ready=True,
        reason="",
        actor="reconciliation",
        run_id=run_id,
        finished_at=finished_at,
    )
    return finished_at


def test_healthy_system_stays_enabled():
    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        result = recovery.tick()
        assert not result.resumed
        assert not strategy.pause_entries()
        assert result.state == PauseState.TRADING
    finally:
        temp.cleanup()


def test_market_ws_disconnect_pause_then_stable_auto_resume():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["status"] = "DISCONNECTED"
        market.value["stale"] = True
        recovery.tick()
        record = strategy.pause_record()
        assert record["pause_entries"]
        assert record["pause_cause"] == "MARKET_WS_DOWN"

        market.value["status"] = "CONNECTED"
        market.value["stale"] = False
        assert complete_stability(base, recovery).resumed
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_user_ws_connected_but_stale_blocks_until_fresh_and_reconciled():
    temp, base, strategy, _market, user, recovery = build_clean()
    try:
        user.value["stale"] = True
        recovery.tick()
        assert strategy.pause_record()["pause_cause"] == "USER_WS_STALE"

        user.value["stale"] = False
        user.value["last_message_at"] = now_iso()
        blocked = recovery.attempt_auto_resume()
        assert "RECONCILIATION_CLEAN_EVIDENCE_NOT_NEWER_THAN_PAUSE" in blocked.blockers

        clean_reconciliation(strategy, 101)
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_one_book_not_ready_blocks_all_books_until_ready():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["subscribed_asset_ids"] = ["yes", "no"]
        market.value["books"]["no"] = {
            "ready": False, "reason": "DEPTH_PENDING", "exchange_age_ms": 20
        }
        recovery.tick()
        assert strategy.pause_record()["pause_cause"] == "BOOK_NOT_READY"
        assert "BOOK_NOT_READY" in recovery.blockers()

        market.value["books"]["no"] = {
            "ready": True, "reason": "READY", "exchange_age_ms": 20
        }
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_freshness_flap_resets_stability_and_prevents_premature_resume():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="market_ws", reason="MARKET_DATA_STALE", owner="MACHINE"
        )
        recovery.attempt_auto_resume()
        assert strategy.pause_record()["pause_state"] == PauseState.PAUSED_WAITING_STABILITY

        market.value["books"]["yes"]["exchange_age_ms"] = 1001
        blocked = recovery.attempt_auto_resume()
        assert "MARKET_DATA_STALE" in blocked.blockers
        assert strategy.pause_record()["pause_eligible_since"] == ""

        market.value["books"]["yes"]["exchange_age_ms"] = 10
        assert not recovery.attempt_auto_resume().resumed
        assert strategy.pause_entries()
        base.set_state(
            "pause_eligible_since",
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "test",
        )
        assert recovery.attempt_auto_resume().resumed
    finally:
        temp.cleanup()


def test_operator_pause_never_auto_releases_but_manual_uses_same_evaluator():
    temp, _base, strategy, market, user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="operator", reason="OPERATOR_PAUSE", owner="OPERATOR"
        )
        result = recovery.attempt_auto_resume()
        assert "MANUAL_ONLY_PAUSE" in result.blockers
        assert strategy.pause_entries()

        manual = request_manual_resume(
            recovery.config, recovery.repo, strategy, market, user
        )
        assert manual["ok"]
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_kill_switch_never_auto_releases():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="market_ws", reason="MARKET_WS_DOWN", owner="MACHINE"
        )
        base.set_state("kill_switch", "true", "operator")
        result = recovery.attempt_auto_resume()
        assert "KILL_SWITCH_ACTIVE" in result.blockers
        assert strategy.pause_entries()
        assert base.kill_switch_active()
    finally:
        temp.cleanup()


def test_unknown_reason_defaults_manual_only():
    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="risk_manager",
            reason="UNCLASSIFIED_INTERNAL_CORRUPTION",
            owner="MACHINE",
        )
        record = strategy.pause_record()
        assert record["release_policy"] == ReleasePolicy.MANUAL_ONLY
        assert record["pause_state"] == PauseState.PAUSED_MANUAL_ONLY
        assert "MANUAL_ONLY_PAUSE" in recovery.attempt_auto_resume().blockers
    finally:
        temp.cleanup()


def test_geographic_not_allowed_and_stale_evidence_block():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        base.set_state("geographic_availability", "BLOCKED", "test")
        recovery.tick()
        assert strategy.pause_record()["release_policy"] == ReleasePolicy.MANUAL_ONLY
        assert "GEOGRAPHIC_NOT_ALLOWED" in recovery.blockers()
    finally:
        temp.cleanup()


def test_invalid_config_is_manual_only():
    config = LiveConfig(
        pause_entries_default=False,
        live_kill_switch_default=False,
        recovery_stability_seconds=6,
    )
    temp, _base, strategy, _market, _user, recovery = build_clean(config)
    try:
        recovery.tick()
        record = strategy.pause_record()
        assert record["pause_cause"] == "CONFIG_INVALID"
        assert record["release_policy"] == ReleasePolicy.MANUAL_ONLY
    finally:
        temp.cleanup()


def test_generation_cas_rejects_stale_release():
    temp, _base, strategy, _market, _user, _recovery = build_clean()
    try:
        first, _ = strategy.acquire_pause(
            actor="market_ws", reason="MARKET_WS_DOWN", owner="MACHINE"
        )
        strategy.acquire_pause(
            actor="risk_manager", reason="CONFIG_INVALID", owner="MACHINE"
        )
        assert not strategy.release_pause_cas(
            expected_generation=first["pause_generation"],
            expected_owner="MACHINE",
            actor="pause_recovery",
            reason="STALE",
        )
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_owner_change_during_evaluation_rejects_release():
    temp, _base, strategy, _market, _user, _recovery = build_clean()
    try:
        first, _ = strategy.acquire_pause(
            actor="market_ws", reason="MARKET_WS_DOWN", owner="MACHINE"
        )
        strategy.acquire_pause(
            actor="operator", reason="OPERATOR_PAUSE", owner="OPERATOR"
        )
        assert not strategy.release_pause_cas(
            expected_generation=first["pause_generation"],
            expected_owner="MACHINE",
            actor="pause_recovery",
            reason="STALE",
        )
        assert strategy.pause_record()["pause_owner"] == "OPERATOR"
    finally:
        temp.cleanup()


def test_restart_preserves_manual_only_generation_and_evidence():
    temp, base, strategy, _market, _user, _recovery = build_clean()
    try:
        before, _ = strategy.acquire_pause(
            actor="operator", reason="OPERATOR_PAUSE", owner="OPERATOR"
        )
        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        after = restarted.pause_record()
        assert after["pause_generation"] == before["pause_generation"]
        assert after["pause_acquired_at"] == before["pause_acquired_at"]
        assert after["release_policy"] == ReleasePolicy.MANUAL_ONLY
    finally:
        temp.cleanup()


def test_duplicate_pause_and_blocker_ticks_do_not_create_audit_storm():
    temp, base, strategy, market, _user, recovery = build_clean()
    try:
        market.value["status"] = "DISCONNECTED"
        recovery.tick()
        with base.connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM live_audit_timeline"
            ).fetchone()[0]
        for _ in range(5):
            recovery.tick()
        with base.connect() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM live_audit_timeline"
            ).fetchone()[0]
        assert after == before
        assert strategy.pause_record()["pause_generation"] == 1
    finally:
        temp.cleanup()


def test_multiple_simultaneous_blockers_are_all_reported():
    temp, base, _strategy, market, user, recovery = build_clean()
    try:
        market.value["status"] = "DISCONNECTED"
        market.value["stale"] = True
        market.value["books"]["yes"]["ready"] = False
        user.value["status"] = "DISCONNECTED"
        user.value["stale"] = True
        base.set_state("reconciliation_readiness", "NOT_READY", "test")
        codes = set(recovery.blockers())
        assert {
            "MARKET_WS_NOT_CONNECTED",
            "MARKET_DATA_STALE",
            "BOOK_NOT_READY",
            "USER_WS_NOT_CONNECTED",
            "USER_WS_STALE",
            "RECONCILIATION_NOT_READY",
        } <= codes
    finally:
        temp.cleanup()


def test_recovery_engine_exception_is_visible_and_fail_closed():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        recovery.mark_degraded(RuntimeError("synthetic failure"))
        assert base.get_state("recovery_engine_status") == "DEGRADED"
        assert strategy.pause_entries()
        assert "AUTO_RECOVERY_DEGRADED" in recovery.blockers()
    finally:
        temp.cleanup()


def test_rate_limit_then_new_clean_run_recovers():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False,
            reason="RECONCILIATION_RATE_LIMITED",
            actor="reconciliation",
            run_id=201,
            finished_at=now_iso(),
        )
        assert strategy.pause_record()["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN
        clean_reconciliation(strategy, 202)
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_137741_gap_repair_then_137742_clean_without_repairs_promotes():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False,
            reason="RECONCILIATION_GAP",
            actor="reconciliation",
            run_id=137741,
            finished_at=now_iso(),
        )
        record = strategy.pause_record()
        assert record["release_policy"] == ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION
        assert base.get_state("recovery_financial_verified_generation") == ""

        clean = asyncio.run(
            ReconciliationWorker(
                base, MockTradingAdapter(), strategy
            ).run_once("reconciliation")
        )
        assert clean["status"] == "ok"
        assert clean["repairs"] == []
        assert base.get_state("reconciliation_readiness") == "READY"
        assert base.get_state("live_blocked_by_reconciliation") == "false"
        assert base.get_state(
            "recovery_financial_verified_generation"
        ) == str(record["pause_generation"])
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_repairable_gap_cannot_recover_without_new_clean_evidence():
    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False,
            reason="RECONCILIATION_GAP",
            actor="reconciliation",
            run_id=301,
            finished_at=now_iso(),
        )
        result = recovery.attempt_auto_resume()
        assert "RECONCILIATION_NOT_READY" in result.blockers
        assert "FINANCIAL_REPAIR_NOT_VERIFIED" in result.blockers
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_temporary_network_error_retries_then_clean_recovers():
    class TimeoutAdapter(MockTradingAdapter):
        async def get_balance(self):
            raise TimeoutError("temporary API network timeout")

    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        failed = asyncio.run(
            ReconciliationWorker(
                base, TimeoutAdapter(), strategy
            ).run_once("reconciliation")
        )
        assert failed["status"] == "failed"
        assert strategy.pause_record()["pause_cause"] == "RECONCILIATION_TEMPORARY_ERROR"

        clean = asyncio.run(
            ReconciliationWorker(
                base, MockTradingAdapter(), strategy
            ).run_once("reconciliation")
        )
        assert clean["status"] == "ok"
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_policy_table_is_fail_closed_for_unknown_reason():
    policy = recovery_policy("SOMETHING_NEW_AND_UNCLASSIFIED")
    assert policy.release_policy == ReleasePolicy.MANUAL_ONLY


# --- RECONNECT_RECONCILIATION_PENDING regression coverage -----------------
#
# market_websocket.py sets strategy_block_reason="RECONNECT_RECONCILIATION_PENDING"
# while a reconnect-triggered reconciliation is in flight. Before this fix that
# reason was absent from RECOVERY_POLICIES, so it fell through to UNKNOWN_POLICY
# (MANUAL_ONLY) the moment pause_recovery acquired a pause for it, permanently
# requiring an operator even after every real safety gate went clean.

def test_reconnect_reconciliation_pending_policy_is_transient_not_manual(  # Test A
) -> None:
    policy = recovery_policy("RECONNECT_RECONCILIATION_PENDING")
    assert policy.release_policy == ReleasePolicy.AUTO_WHEN_CLEAN
    assert policy.release_policy != ReleasePolicy.MANUAL_ONLY


def test_reconnect_reconciliation_pending_blocks_while_pending():  # Test B
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        # market_websocket.py sets exactly these two keys while the
        # reconnect-triggered reconciliation run is still in flight; it does
        # not touch reconciliation_readiness itself (that belongs to the
        # reconciliation worker's own lifecycle).
        base.set_states({
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": "RECONNECT_RECONCILIATION_PENDING",
        }, "test")
        recovery.tick()
        record = strategy.pause_record()
        assert record["pause_entries"]
        assert record["pause_cause"] == "RECONNECT_RECONCILIATION_PENDING"
        assert record["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN
        assert record["pause_state"] != PauseState.PAUSED_MANUAL_ONLY

        result = recovery.attempt_auto_resume()
        assert not result.resumed
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_reconnect_reconciliation_pending_auto_releases_once_clean():  # Test C
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        base.set_states({
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": "RECONNECT_RECONCILIATION_PENDING",
        }, "test")
        recovery.tick()
        assert strategy.pause_record()["pause_cause"] == "RECONNECT_RECONCILIATION_PENDING"

        # Reconciliation finishes; every other gate is already clean via build_clean().
        base.set_states({
            "strategy_readiness": "READY",
            "strategy_block_reason": "",
        }, "test")
        result = complete_stability(base, recovery)
        assert result.resumed
        assert not strategy.pause_entries()
    finally:
        temp.cleanup()


def test_reconnect_reconciliation_pending_stays_blocked_with_other_blocker():  # Test D
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        base.set_states({
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": "RECONNECT_RECONCILIATION_PENDING",
        }, "test")
        recovery.tick()
        assert strategy.pause_record()["pause_cause"] == "RECONNECT_RECONCILIATION_PENDING"

        # Reconnect reconciliation itself finishes clean, but an unrelated
        # safety gate (kill switch) is independently active.
        base.set_states({
            "strategy_readiness": "READY",
            "strategy_block_reason": "",
        }, "test")
        base.set_state("kill_switch", "true", "operator")
        result = recovery.attempt_auto_resume()
        assert not result.resumed
        assert "KILL_SWITCH_ACTIVE" in result.blockers
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_true_manual_only_reasons_unaffected_by_reconnect_fix():  # Test E
    assert recovery_policy("OPERATOR_PAUSE").release_policy == ReleasePolicy.MANUAL_ONLY
    assert recovery_policy("KILL_SWITCH_ACTIVE").release_policy == ReleasePolicy.MANUAL_ONLY
    assert (
        recovery_policy("RECONCILIATION_CONTRADICTION").release_policy
        == ReleasePolicy.MANUAL_ONLY
    )

    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="operator", reason="OPERATOR_PAUSE", owner="OPERATOR"
        )
        result = recovery.attempt_auto_resume()
        assert "MANUAL_ONLY_PAUSE" in result.blockers
        assert strategy.pause_entries()
    finally:
        temp.cleanup()


def test_unrelated_unknown_reason_still_fail_closed_after_reconnect_fix():  # Test F
    policy = recovery_policy("SOME_OTHER_UNMAPPED_REASON_XYZ")
    assert policy.release_policy == ReleasePolicy.MANUAL_ONLY

    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="risk_manager",
            reason="SOME_OTHER_UNMAPPED_REASON_XYZ",
            owner="MACHINE",
        )
        record = strategy.pause_record()
        assert record["release_policy"] == ReleasePolicy.MANUAL_ONLY
        assert record["pause_state"] == PauseState.PAUSED_MANUAL_ONLY
        assert "MANUAL_ONLY_PAUSE" in recovery.attempt_auto_resume().blockers
    finally:
        temp.cleanup()


def test_heartbeat_requires_success_newer_than_pause():
    config = LiveConfig(
        trading_mode="LIVE",
        execution_mode="REAL_TRADING",
        live_module_enabled=True,
        live_trading_enabled=True,
        live_order_submission_enabled=True,
        live_adapter="polymarket",
        live_kill_switch_default=False,
        pause_entries_default=False,
        continuous_trading_enabled=True,
        market_ws_enabled=True,
        private_signing_readiness_enabled=True,
        signer_address="0x" + "1" * 40,
        funder_address="0x" + "2" * 40,
        profile_address="0x" + "2" * 40,
        recovery_stability_seconds=0.01,
    )
    temp, base, strategy, _market, _user, recovery = build_clean(config)
    try:
        old_success = base.get_state("last_successful_heartbeat_at")
        strategy.acquire_pause(
            actor="heartbeat", reason="HEARTBEAT_FAILURE", owner="MACHINE"
        )
        base.set_state("order_heartbeat_status", "OK", "heartbeat")
        base.set_state(
            "last_successful_heartbeat_at", old_success, "heartbeat"
        )
        result = recovery.attempt_auto_resume()
        assert "HEARTBEAT_SUCCESS_NOT_NEWER_THAN_PAUSE" in result.blockers

        base.set_state(
            "last_successful_heartbeat_at", now_iso(), "heartbeat"
        )
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_unclassified_reconciliation_contradiction_stays_manual_only():
    temp, _base, strategy, _market, _user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False,
            reason="RECONCILIATION_CONTRADICTION",
            actor="reconciliation",
            run_id=401,
            finished_at=now_iso(),
        )
        clean_reconciliation(strategy, 402)
        record = strategy.pause_record()
        assert record["release_policy"] == ReleasePolicy.MANUAL_ONLY
        assert "MANUAL_ONLY_PAUSE" in recovery.attempt_auto_resume().blockers
    finally:
        temp.cleanup()


def test_restart_while_trading_preserves_enabled_state():
    temp, base, strategy, _market, _user, _recovery = build_clean()
    try:
        before = strategy.pause_record()
        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=True)
        after = restarted.pause_record()
        assert not after["pause_entries"]
        assert after["pause_state"] == PauseState.TRADING
        assert after["pause_generation"] == before["pause_generation"]
    finally:
        temp.cleanup()


def test_restart_during_recoverable_pause_preserves_acquisition_evidence():
    temp, base, strategy, market, user, recovery = build_clean()
    try:
        before, _ = strategy.acquire_pause(
            actor="market_ws", reason="MARKET_WS_DOWN", owner="MACHINE"
        )
        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        after = restarted.pause_record()
        assert after["pause_state"] == PauseState.PAUSED_RECOVERING
        assert after["pause_generation"] == before["pause_generation"]
        assert after["pause_acquired_at"] == before["pause_acquired_at"]

        restarted_recovery = PauseRecoveryCoordinator(
            base, restarted, market, user, config=recovery.config
        )
        result = restarted_recovery.attempt_auto_resume()
        assert not result.resumed
        assert result.blockers == ("STABILITY_WINDOW",)
    finally:
        temp.cleanup()


def test_restart_during_stability_window_preserves_window_and_cas():
    config = LiveConfig(
        pause_entries_default=False,
        live_kill_switch_default=False,
        recovery_stability_seconds=5,
    )
    temp, base, strategy, market, user, recovery = build_clean(config)
    try:
        strategy.acquire_pause(
            actor="market_ws", reason="MARKET_WS_DOWN", owner="MACHINE"
        )
        recovery.attempt_auto_resume()
        before = strategy.pause_record()
        assert before["pause_state"] == PauseState.PAUSED_WAITING_STABILITY

        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        restarted_recovery = PauseRecoveryCoordinator(
            base, restarted, market, user, config=recovery.config
        )
        after = restarted.pause_record()
        assert after["pause_generation"] == before["pause_generation"]
        assert after["pause_eligible_since"] == before["pause_eligible_since"]
        assert not restarted_recovery.attempt_auto_resume().resumed
        base.set_state(
            "pause_eligible_since",
            (datetime.now(timezone.utc) - timedelta(seconds= 6)).isoformat(),
            "test",
        )
        assert restarted_recovery.attempt_auto_resume().resumed
    finally:
        temp.cleanup()


def test_restart_after_clean_reconciliation_preserves_financial_proof():
    temp, base, strategy, market, user, recovery = build_clean()
    try:
        strategy.set_reconciliation_state(
            ready=False,
            reason="RECONCILIATION_GAP",
            actor="reconciliation",
            run_id=501,
            finished_at=now_iso(),
        )
        clean_reconciliation(strategy, 502)
        before = strategy.pause_record()
        assert base.get_state(
            "recovery_financial_verified_generation"
        ) == str(before["pause_generation"])

        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        restarted_recovery = PauseRecoveryCoordinator(
            base, restarted, market, user, config=recovery.config
        )
        after = restarted.pause_record()
        assert after["pause_generation"] == before["pause_generation"]
        assert base.get_state(
            "recovery_financial_verified_generation"
        ) == str(after["pause_generation"])
        assert complete_stability(base, restarted_recovery).resumed
    finally:
        temp.cleanup()


def test_empty_strategy_readiness_is_transient_and_recovers():
    temp, base, strategy, _market, _user, recovery = build_clean()
    try:
        base.set_states({
            "strategy_readiness": "NOT_READY",
            "strategy_block_reason": "",
        }, "test")
        recovery.tick()
        record = strategy.pause_record()
        assert record["pause_cause"] == "MARKET_DATA_NOT_READY"
        assert record["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN

        base.set_state("strategy_readiness", "READY", "test")
        assert complete_stability(base, recovery).resumed
    finally:
        temp.cleanup()


def test_migration_repairs_only_legacy_strategy_not_ready_manual_pause():
    temp, base, strategy, _market, _user, _recovery = build_clean()
    try:
        strategy.acquire_pause(
            actor="strategy", reason="STRATEGY_NOT_READY", owner="MACHINE"
        )
        before = strategy.pause_record()
        base.set_states({
            "pause_cause": "STRATEGY_NOT_READY",
            "pause_reason": "STRATEGY_NOT_READY",
            "release_policy": ReleasePolicy.MANUAL_ONLY,
            "pause_state": PauseState.PAUSED_MANUAL_ONLY,
        }, "test")

        restarted = StrategyRepository(base)
        restarted.migrate(pause_entries_default=False)
        after = restarted.pause_record()
        assert after["pause_cause"] == "MARKET_DATA_NOT_READY"
        assert after["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN
        assert after["pause_generation"] == before["pause_generation"] + 1
    finally:
        temp.cleanup()


def test_short_market_freshness_blip_gates_without_durable_pause():
    config = LiveConfig(
        pause_entries_default=False,
        live_kill_switch_default=False,
        recovery_stability_seconds=0.01,
        recovery_detection_debounce_seconds=2,
    )
    temp, base, strategy, market, _user, recovery = build_clean(config)
    try:
        market.value["books"]["yes"]["ready"] = False
        market.value["books"]["yes"]["reason"] = "STALE_EXCHANGE_TIMESTAMP"
        market.value["books"]["yes"]["exchange_age_ms"] = 1500
        result = recovery.tick()
        assert not strategy.pause_entries()
        assert "BOOK_NOT_READY" in result.blockers
        assert base.get_state("recovery_status") == "DETECTING"
        assert recovery.status()["trading_status"] == "GATED"

        for code in tuple(recovery._blocker_first_seen):
            recovery._blocker_first_seen[code] -= 3
        recovery.tick()
        record = strategy.pause_record()
        assert record["pause_entries"]
        assert record["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN
    finally:
        temp.cleanup()
