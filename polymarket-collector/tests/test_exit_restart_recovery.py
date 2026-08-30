"""Restart and crash recovery for exit obligations.

A process restart is the one event that can erase in-memory risk state
entirely. Every durable fact an exit depends on — the stop latch, the intent,
the remaining shares — therefore has to survive a cold start and be picked up
again *before* the trader is allowed to open anything new.

These tests simulate the crash by throwing the ``StrategyRepository`` away and
rebuilding it from the same database file, which is exactly what the process
does on restart.
"""

import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile

from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


def _crashed_trader(state_setup):
    """Build a DB, run ``state_setup``, then reopen it as a fresh process would."""
    temporary = tempfile.TemporaryDirectory()
    db_path = Path(temporary.name) / "live.sqlite3"
    base = LiveRepository(db_path)
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate(pause_entries_default=False)

    base.upsert_market({
        "event_id": "restart-event",
        "condition_id": "restart-condition",
        "yes_token_id": "restart-token",
        "no_token_id": "restart-loser",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "5",
        "min_tick_size": "0.01",
    })
    event = repo.reserve_event_entry(
        event_id="restart-event", condition_id="restart-condition",
        token_id="restart-token", side="YES", simultaneous=False,
        reason_code="ENTRY_074",
    )
    position = repo.open_position(
        event_id="restart-event", condition_id="restart-condition",
        token_id="restart-token", outcome="YES", shares=Decimal("5.205478"),
        average_price=Decimal("0.74"), cost_all_in=Decimal("3.80"),
        fees=Decimal("0"), sellable_shares=Decimal("5.205478"),
        min_sellable=Decimal("5"),
        entry_intent_id=str(event["entry_intent_id"]),
    )
    state_setup(repo, position)

    # The crash: nothing in memory survives, only the file.
    restarted_base = LiveRepository(db_path)
    restarted = StrategyRepository(restarted_base)
    return temporary, restarted, position


def _assert_recovered(repo, position, *, note):
    """After a restart the position must be visible to risk management."""
    managed = {row["position_id"] for row in repo.risk_managed_positions()}
    assert position["position_id"] in managed, f"{note}: not risk-managed"
    blocking = {row["position_id"] for row in repo.entry_blocking_positions()}
    assert position["position_id"] in blocking, f"{note}: does not block entries"
    current = repo.position_for_token(position["token_id"])
    assert Decimal(current["remaining_shares_text"]) > 0
    return current


def test_restart_while_open_recovers_the_position():
    temporary, repo, position = _crashed_trader(lambda _repo, _pos: None)
    try:
        current = _assert_recovered(repo, position, note="OPEN")
        assert current["state"] == "OPEN"
    finally:
        temporary.cleanup()


def test_restart_while_exiting_preserves_the_stop_latch():
    """The latch is the whole point of being durable.

    If a restart cleared ``stop_stage`` the position would need the price to
    cross 0.66 all over again, and a market that gapped straight through would
    never re-arm it.
    """
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))

    temporary, repo, position = _crashed_trader(setup)
    try:
        current = _assert_recovered(repo, position, note="EXITING")
        assert current["state"] == "EXITING"
        assert current["stop_stage"] >= 1, "the stop latch did not survive"
    finally:
        temporary.cleanup()


def test_restart_while_waiting_sellable_keeps_the_intent_open():
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.205478"), price_limit=Decimal("0.64"),
            book_hash="stop",
        )
        repo.mark_waiting_sellable(
            str(intent["intent_id"]), reason="WAITING_FOR_SELLABLE_BALANCE",
        )

    temporary, repo, position = _crashed_trader(setup)
    try:
        _assert_recovered(repo, position, note="WAITING_SELLABLE")
        unresolved = repo.unresolved_intents()
        assert [i["state"] for i in unresolved] == ["WAITING_SELLABLE"]
        assert repo.entry_blocking_intents(), "entries must stay blocked"
    finally:
        temporary.cleanup()


def test_restart_while_submitting_keeps_the_intent_unresolved():
    """A SUBMITTING intent may or may not exist remotely.

    It must come back unresolved so reconciliation resolves it against the
    exchange rather than the trader assuming either outcome.
    """
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.205478"), price_limit=Decimal("0.64"),
            book_hash="stop",
        )
        repo.update_intent(str(intent["intent_id"]), state="SUBMITTING")

    temporary, repo, position = _crashed_trader(setup)
    try:
        _assert_recovered(repo, position, note="SUBMITTING")
        assert [i["state"] for i in repo.unresolved_intents()] == ["SUBMITTING"]
    finally:
        temporary.cleanup()


def test_restart_during_unknown_transport_keeps_reconciliation_required():
    """The dangerous one: the SELL's fate is unknown across a crash."""
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.205478"), price_limit=Decimal("0.64"),
            book_hash="stop",
        )
        repo.update_intent(
            str(intent["intent_id"]), state="RECONCILIATION_REQUIRED",
            submitted_at=now_iso(),
            normalized_error="TransportError: Request failed",
        )

    temporary, repo, position = _crashed_trader(setup)
    try:
        current = _assert_recovered(repo, position, note="UNKNOWN")
        unresolved = repo.unresolved_intents()
        assert [i["state"] for i in unresolved] == ["RECONCILIATION_REQUIRED"]
        assert unresolved[0]["submitted_at"], (
            "submission evidence must survive so resolution cannot bury it"
        )
        # And resolution must still refuse to terminalise it after the restart.
        result = repo.mark_position_resolved(
            str(position["position_id"]), winner=True, redeem_pending=True,
        )
        assert result["state"] != "REDEEM_PENDING"
        assert current["stop_stage"] >= 1
    finally:
        temporary.cleanup()


def test_restart_after_partial_exit_keeps_the_remainder_managed():
    """Half a fill is still an open obligation for the other half."""
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.205478"), price_limit=Decimal("0.64"),
            book_hash="stop",
        )
        repo.apply_exit_fill(
            position_id=str(position["position_id"]),
            intent_id=str(intent["intent_id"]),
            sold_shares=Decimal("2.105478"),
            average_price=Decimal("0.65"),
            fees=Decimal("0"),
            final_state="PARTIAL_FINAL",
            min_sellable=Decimal("1"),
            purpose="STOP_066",
            book_hash="partial",
            cumulative_filled_shares=Decimal("2.105478"),
            cumulative_notional=Decimal("1.368560"),
            cumulative_fees=Decimal("0"),
        )

    temporary, repo, position = _crashed_trader(setup)
    try:
        current = _assert_recovered(repo, position, note="PARTIAL")
        # Decimal arithmetic: 5.205478 - 2.105478 is exactly 3.1, not 3.0999…
        assert Decimal(current["remaining_shares_text"]) == Decimal("3.1")
        assert current["stop_stage"] >= 1
    finally:
        temporary.cleanup()


def test_crash_between_buy_fill_and_management_recovers_the_shares():
    """The atomicity requirement: a BUY fill is never orphaned.

    If the process dies after the fill is durable but before anything puts the
    position under management, the restart scan has to find it from the
    database alone.
    """
    temporary = tempfile.TemporaryDirectory()
    try:
        db_path = Path(temporary.name) / "live.sqlite3"
        base = LiveRepository(db_path)
        base.migrate()
        repo = StrategyRepository(base)
        repo.migrate(pause_entries_default=False)
        base.upsert_market({
            "event_id": "orphan-event",
            "condition_id": "orphan-condition",
            "yes_token_id": "orphan-token",
            "no_token_id": "orphan-loser",
            "accepting_orders": True,
            "min_order_size": "5",
        })
        event = repo.reserve_event_entry(
            event_id="orphan-event", condition_id="orphan-condition",
            token_id="orphan-token", side="YES", simultaneous=False,
            reason_code="ENTRY_074",
        )
        position = repo.open_position(
            event_id="orphan-event", condition_id="orphan-condition",
            token_id="orphan-token", outcome="YES", shares=Decimal("3.68"),
            average_price=Decimal("0.76"), cost_all_in=Decimal("2.80"),
            fees=Decimal("0"), sellable_shares=Decimal("0"),
            min_sellable=Decimal("5"),
            entry_intent_id=str(event["entry_intent_id"]),
        )
        # Crash here: nothing else ran.
        restarted = StrategyRepository(LiveRepository(db_path))
        recovered = restarted.risk_managed_positions()
        assert [row["position_id"] for row in recovered] == [
            position["position_id"]
        ]
        assert restarted.exposure() > 0
        assert restarted.entry_blocking_positions(), (
            "an orphaned sub-minimum fill must still block new entries"
        )
    finally:
        temporary.cleanup()


def test_recovery_is_idempotent_across_repeated_restarts():
    """Restart loops must not multiply intents or lose the latch."""
    def setup(repo, position):
        repo.latch_stop_exit(str(position["position_id"]))
        repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5.205478"), price_limit=Decimal("0.64"),
            book_hash="stop",
        )

    temporary, repo, position = _crashed_trader(setup)
    try:
        first = repo.position_for_token(position["token_id"])
        for _ in range(3):
            repo = StrategyRepository(LiveRepository(repo.base.db_path))
        again = repo.position_for_token(position["token_id"])
        assert again["state"] == first["state"]
        assert again["stop_stage"] == first["stop_stage"]
        assert again["remaining_shares_text"] == first["remaining_shares_text"]
        assert len(repo.unresolved_intents()) == 1
    finally:
        temporary.cleanup()
