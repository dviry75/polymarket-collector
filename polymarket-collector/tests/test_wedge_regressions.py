"""Regressions for the three defects that each independently wedged LIVE.

D11 a transient closed-only probe failure was classified as an authoritative
     financial contradiction and escalated to a permanent MANUAL_ONLY pause.
D12 the MANUAL_ONLY release policy was stamped once and read back forever, so
     the pause outlived the condition that caused it.
D13 a user-websocket auth rejection returned out of the reconnect loop, so a
     transient rejection killed the private feed until the next restart.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from live.reconciliation import (
    TransientAccountModeError,
    _is_temporary_network_error,
)
from live.recovery_policy import ReleasePolicy, recovery_policy


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------- D11

def test_failed_closed_only_probe_is_temporary_not_contradiction():
    exc = TransientAccountModeError("ReadTimeout: closed-only probe timed out")
    assert _is_temporary_network_error(exc) is True
    policy = recovery_policy("RECONCILIATION_TEMPORARY_ERROR")
    assert policy.release_policy == ReleasePolicy.AUTO_WHEN_CLEAN


def test_real_restriction_still_reaches_manual_review():
    # CLOSED_ONLY is a genuine account restriction and must NOT be softened.
    assert (
        recovery_policy("RECONCILIATION_CONTRADICTION").release_policy
        == ReleasePolicy.MANUAL_ONLY
    )


# --------------------------------------------------------------------- D12

class _Evaluator:
    """Minimal harness exposing only the de-escalation predicate."""

    from live.pause_recovery import EntryReleaseEvaluator as _E

    NEVER_AUTO_DEESCALATE = _E.NEVER_AUTO_DEESCALATE
    MANUAL_DEESCALATION_CLEAN_SECONDS = _E.MANUAL_DEESCALATION_CLEAN_SECONDS
    _manual_cause_resolved = _E._manual_cause_resolved


def _state(**over):
    base = {
        "reconciliation_readiness": "READY",
        "reconciliation_block_reason": "",
        "live_blocked_by_reconciliation": "false",
        "last_successful_reconciliation_at": "",
    }
    base.update(over)
    return base


def test_manual_only_deescalates_once_reconciliation_is_durably_clean():
    now = datetime.now(timezone.utc)
    acquired = now - timedelta(minutes=30)
    clean = now - timedelta(seconds=30)
    pause = {
        "pause_entries": True,
        "pause_cause": "RECONCILIATION_CONTRADICTION",
        "pause_acquired_at": _iso(acquired),
    }
    state = _state(last_successful_reconciliation_at=_iso(clean))
    assert _Evaluator._manual_cause_resolved(_Evaluator, pause, state, now) is True


def test_manual_only_holds_while_reconciliation_still_blocked():
    now = datetime.now(timezone.utc)
    pause = {
        "pause_entries": True,
        "pause_cause": "RECONCILIATION_CONTRADICTION",
        "pause_acquired_at": _iso(now - timedelta(minutes=30)),
    }
    state = _state(
        reconciliation_readiness="NOT_READY",
        last_successful_reconciliation_at=_iso(now - timedelta(seconds=30)),
    )
    assert _Evaluator._manual_cause_resolved(_Evaluator, pause, state, now) is False


def test_manual_only_holds_when_clean_evidence_predates_the_pause():
    now = datetime.now(timezone.utc)
    pause = {
        "pause_entries": True,
        "pause_cause": "RECONCILIATION_CONTRADICTION",
        "pause_acquired_at": _iso(now - timedelta(minutes=5)),
    }
    # "clean" reconciliation older than the pause proves nothing about it.
    state = _state(last_successful_reconciliation_at=_iso(now - timedelta(hours=2)))
    assert _Evaluator._manual_cause_resolved(_Evaluator, pause, state, now) is False


@pytest.mark.parametrize(
    "cause",
    ["OPERATOR_PAUSE", "KILL_SWITCH_ACTIVE", "DAILY_LOSS_LIMIT",
     "GEOGRAPHIC_AVAILABILITY_FAILED", "EMERGENCY_CLOSE_REQUESTED"],
)
def test_operator_and_risk_pauses_never_deescalate(cause):
    now = datetime.now(timezone.utc)
    pause = {
        "pause_entries": True,
        "pause_cause": cause,
        "pause_acquired_at": _iso(now - timedelta(hours=1)),
    }
    state = _state(last_successful_reconciliation_at=_iso(now - timedelta(seconds=10)))
    assert _Evaluator._manual_cause_resolved(_Evaluator, pause, state, now) is False


# ------------------------------------------------------- D12, second half
# Removing the MANUAL_ONLY_PAUSE blocker is not enough on its own:
# release_pause_cas refuses machine releases while the stored policy is still
# MANUAL_ONLY, so the pause sat with zero blockers and never released. The
# stale stamp itself has to be cleared.

def _repo(tmp_path):
    from live.repository import LiveRepository
    from live.strategy_repository import StrategyRepository

    base = LiveRepository(tmp_path / "t.sqlite3")
    base.migrate(True)
    repo = StrategyRepository(base)
    # Without this the migration installs a CONFIGURED_STARTUP_PAUSE, which is
    # itself MANUAL_ONLY and would mask the pause under test.
    repo.migrate(pause_entries_default=False)
    return repo


def test_release_is_refused_while_manual_only_stamp_remains(tmp_path):
    repo = _repo(tmp_path)
    record, _ = repo.acquire_pause(
        actor="pause_recovery", reason="RECONCILIATION_CONTRADICTION",
        owner="RECONCILIATION",
    )
    gen = int(repo.pause_record()["pause_generation"])
    assert repo.release_pause_cas(
        expected_generation=gen, expected_owner="RECONCILIATION",
        actor="pause_recovery", reason="AUTO_RECOVERY_GATES_STABLE",
    ) is False, "machine release must not bypass the MANUAL_ONLY guard"


def test_deescalation_clears_the_stamp_and_then_release_succeeds(tmp_path):
    repo = _repo(tmp_path)
    repo.acquire_pause(
        actor="pause_recovery", reason="RECONCILIATION_CONTRADICTION",
        owner="RECONCILIATION",
    )
    gen = int(repo.pause_record()["pause_generation"])

    assert repo.deescalate_pause_policy_cas(
        expected_generation=gen,
        expected_cause="RECONCILIATION_CONTRADICTION",
        actor="pause_recovery",
    ) is True
    assert repo.pause_record()["release_policy"] == ReleasePolicy.AUTO_WHEN_CLEAN
    assert repo.release_pause_cas(
        expected_generation=gen, expected_owner="RECONCILIATION",
        actor="pause_recovery", reason="AUTO_RECOVERY_GATES_STABLE",
    ) is True
    assert repo.pause_record()["pause_entries"] is False


def test_deescalation_refuses_non_reconciliation_causes(tmp_path):
    repo = _repo(tmp_path)
    repo.acquire_pause(
        actor="operator", reason="DAILY_LOSS_LIMIT", owner="OPERATOR",
    )
    gen = int(repo.pause_record()["pause_generation"])
    assert repo.deescalate_pause_policy_cas(
        expected_generation=gen, expected_cause="DAILY_LOSS_LIMIT",
        actor="pause_recovery",
    ) is False


def test_deescalation_refuses_on_generation_or_cause_mismatch(tmp_path):
    repo = _repo(tmp_path)
    repo.acquire_pause(
        actor="pause_recovery", reason="RECONCILIATION_CONTRADICTION",
        owner="RECONCILIATION",
    )
    gen = int(repo.pause_record()["pause_generation"])
    assert repo.deescalate_pause_policy_cas(
        expected_generation=gen + 5,
        expected_cause="RECONCILIATION_CONTRADICTION",
        actor="pause_recovery",
    ) is False
    assert repo.deescalate_pause_policy_cas(
        expected_generation=gen, expected_cause="RECONCILIATION_FAILED",
        actor="pause_recovery",
    ) is False


def test_deescalation_is_not_available_to_arbitrary_actors(tmp_path):
    repo = _repo(tmp_path)
    repo.acquire_pause(
        actor="pause_recovery", reason="RECONCILIATION_CONTRADICTION",
        owner="RECONCILIATION",
    )
    gen = int(repo.pause_record()["pause_generation"])
    assert repo.deescalate_pause_policy_cas(
        expected_generation=gen,
        expected_cause="RECONCILIATION_CONTRADICTION",
        actor="dashboard",
    ) is False


# --------------------------------------------------------------------- D13

def _user_ws():
    from live.market_websocket import UserWebSocketManager
    return UserWebSocketManager(repo=None)


def test_auth_failure_threshold_allows_retries_before_going_terminal():
    ws = _user_ws()
    assert ws.auth_failure_threshold >= 2, (
        "a single auth rejection must not be terminal"
    )
    assert ws._auth_failures == 0


def test_auth_reconnect_loop_has_no_terminal_return():
    """The loop must not `return` on an auth error -- that killed the feed."""
    import inspect
    from live.market_websocket import UserWebSocketManager

    source = inspect.getsource(UserWebSocketManager.run)
    marker = "if self._is_auth_error(error):"
    assert marker in source
    tail = source.split(marker, 1)[1].split("self.status.reconnect_attempts", 1)[0]
    assert "return" not in tail, (
        "auth errors must fall through to backoff, not exit the reconnect loop"
    )


def test_successful_auth_resets_the_failure_counter():
    import inspect
    from live.market_websocket import UserWebSocketManager

    source = inspect.getsource(UserWebSocketManager)
    assert "self._auth_failures = 0" in source
    assert source.count("self._auth_failures = 0") >= 2, (
        "counter must reset on successful authentication, not only at init"
    )
