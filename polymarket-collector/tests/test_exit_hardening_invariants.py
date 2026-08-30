"""Exit-hardening invariants that the missing-SELL investigation demanded.

The investigation found eleven BUY fills whose stop-loss condition was crossed
and which never produced a SELL fill. The point of these tests is *not* to
claim every stop can be filled — nobody controls whether a bid exists. It is to
pin down the invariant that actually failed:

    BUY FILLED -> POSITION ALWAYS MANAGED -> EXIT ALWAYS ARMED
    -> SL DURABLY LATCHED -> SELL ATTEMPTED PER POLICY
    -> FAILURES RETRIED OR RECONCILED -> NO SILENT ABANDONMENT
    -> NO NEW ENTRIES WHILE EXIT SAFETY IS UNCERTAIN

The second half of the file replays the eleven failure classes as
**synthetic** scenarios. They are explicitly NOT a historical exact replay:
the original order books, best-bid snapshots and readiness frames were never
recorded, so every market input here is fabricated. What is reused from the
investigation is only what was actually established — fill quantities, the
state each position ended in, and which mechanism dropped it.
"""

import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile

from live.config import LiveConfig
from live.provenance import evaluate_gate
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository

from test_latched_market_exit import (
    RecordingSellAdapter,
    _book,
    _case,
    _exit_intents,
    _manage,
    _ok_reconcile,
)


# ---------------------------------------------------------------------------
# Exit is independent of every entry-side gate
# ---------------------------------------------------------------------------

def test_pause_entries_does_not_block_a_stop_loss():
    """Pausing entries is a risk brake, not a reason to stop protecting money.

    Six of the eleven positions were never even detected as crossing the stop.
    An exit path that consults entry gating is one way that happens, so the
    stop must fire with entries hard-paused.
    """
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "paused-entries", paper=False, adapter=adapter,
        reconciliation=_ok_reconcile,
    )
    try:
        repo.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "paused",
        )
        assert base.get_state("pause_entries") == "true"
        assert len(adapter.create_calls) == 1
        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 1
    finally:
        temp.cleanup()


def test_kill_switch_and_closed_entry_window_do_not_block_a_stop_loss():
    """A long-expired entry window must not strand an open position.

    The entry window is a *starting* condition. Once shares are held the exit
    obligation outlives it, so an event whose entry window closed long ago
    still exits.
    """
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "closed-window",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        # A slug far in the past: its entry window is unambiguously shut.
        event_id="btc-updown-5m-1000000000",
    )
    try:
        base.set_state("kill_switch", "true", "operator")
        repo.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "expired-window",
        )
        assert len(adapter.create_calls) == 1
        assert repo.position_for_token(position["token_id"])["stop_stage"] == 1
    finally:
        temp.cleanup()


# ---------------------------------------------------------------------------
# Every share stays under risk management
# ---------------------------------------------------------------------------

def test_sub_minimum_partial_buy_stays_risk_managed():
    """The 3.68-share partial entry must not vanish into DUST bookkeeping.

    A partial BUY below the exchange minimum still cost real money. It has to
    remain in the risk-managed view and keep its full cost in exposure until
    something authoritative settles it.
    """
    temp, _base, repo, _runtime, position = _case(
        "sub-minimum", shares=Decimal("3.68"), minimum=Decimal("5"),
    )
    try:
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert current["remaining_shares_text"] == "3.68"
        managed = [row["position_id"] for row in repo.risk_managed_positions()]
        assert managed == [position["position_id"]]
        assert repo.exposure() > 0
        # It also still blocks a fresh entry slot: exit safety is not settled.
        assert [
            row["position_id"] for row in repo.entry_blocking_positions()
        ] == [position["position_id"]]
    finally:
        temp.cleanup()


def test_post_exit_dust_remains_visible_to_risk_management():
    """Residual dust after a good exit stays managed, but frees the slot.

    ``closed_at`` is what releases the single-position entry slot; the DUST
    *state* is what keeps the remainder visible. Conflating the two either
    strands the trader (no entries ever again) or hides real remainder.
    """
    temp, _base, repo, runtime, position = _case(
        "post-exit-dust", shares=Decimal("5.066664"), minimum=Decimal("5"),
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5.06")]),
            "partial-then-dust",
        )
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert Decimal(current["remaining_shares_text"]) > 0
        assert current["closed_at"] is not None, "entry slot must be released"
        managed = [row["position_id"] for row in repo.risk_managed_positions()]
        assert position["position_id"] in managed
    finally:
        temp.cleanup()


# ---------------------------------------------------------------------------
# Decimal quantities: never compare shares as floats
# ---------------------------------------------------------------------------

def test_awkward_share_quantities_exit_exactly():
    """The real fill sizes from the investigation, to the last digit.

    ``3.8`` and ``5.205478`` are not representable in binary floating point.
    Any equality or subtraction done in ``float`` leaves a phantom remainder
    that either blocks completion or invents dust, so the whole path stays in
    ``Decimal``.
    """
    for quantity in ("5", "5.066664", "5.205478", "3.8", "3.68"):
        shares = Decimal(quantity)
        temp, base, repo, runtime, position = _case(
            f"decimal-{quantity.replace('.', '-')}",
            shares=shares,
            minimum=Decimal("1"),
        )
        try:
            _manage(
                runtime,
                _book(position["token_id"], "0.66", [("0.66", quantity)]),
                f"exit-{quantity}",
            )
            current = repo.position_for_token(position["token_id"])
            intents = _exit_intents(base, position["position_id"])
            assert intents, f"{quantity}: no exit intent was created"
            assert Decimal(intents[0]["requested_shares_text"]) == shares
            assert current["state"] == "CLOSED", f"{quantity}: {current['state']}"
            assert Decimal(current["remaining_shares_text"]) == 0
        finally:
            temp.cleanup()


# ---------------------------------------------------------------------------
# Resolution may not bury an unresolved exit
# ---------------------------------------------------------------------------

def _resolution_race_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate(pause_entries_default=False)
    base.upsert_market({
        "event_id": "race-event",
        "condition_id": "race-condition",
        "yes_token_id": "race-token",
        "no_token_id": "race-loser",
        "accepting_orders": False,
        "market_resolved": True,
        "winning_asset_id": "race-token",
        "winning_outcome": "Up",
    })
    event = repo.reserve_event_entry(
        event_id="race-event", condition_id="race-condition",
        token_id="race-token", side="YES", simultaneous=False,
        reason_code="ENTRY_074",
    )
    position = repo.open_position(
        event_id="race-event", condition_id="race-condition",
        token_id="race-token", outcome="YES", shares=Decimal("5"),
        average_price=Decimal("0.74"), cost_all_in=Decimal("3.70"),
        fees=Decimal("0"), sellable_shares=Decimal("5"),
        entry_intent_id=str(event["entry_intent_id"]),
    )
    return temporary, base, repo, position


def test_resolution_cannot_overwrite_a_submitted_unresolved_exit():
    """A SELL that may exist remotely outranks a market-resolution frame.

    This is how a lost POST response became a "settled" position: resolution
    arrived, stamped a terminal state, and the unresolved SELL stopped being
    anybody's problem.
    """
    temporary, _base, repo, position = _resolution_race_repo()
    try:
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5"), price_limit=Decimal("0.64"),
            book_hash="stop-frame",
        )
        repo.update_intent(
            str(intent["intent_id"]),
            state="RECONCILIATION_REQUIRED",
            submitted_at=now_iso(),
            normalized_error="TransportError: Request failed",
        )
        result = repo.mark_position_resolved(
            str(position["position_id"]), winner=True, redeem_pending=True,
        )
        assert result["state"] != "REDEEM_PENDING"
        assert repo.position_for_token("race-token")["state"] != "REDEEM_PENDING"
    finally:
        temporary.cleanup()


def test_resolution_proceeds_once_the_exit_is_terminal():
    """The protection is a hold, not a deadlock.

    Once the exit reaches a final state the position must be free to resolve,
    otherwise the guard would strand every position it protected.
    """
    temporary, _base, repo, position = _resolution_race_repo()
    try:
        intent = repo.reserve_position_intent(
            position, action="EXIT", purpose="STOP_066", order_type="FAK",
            shares=Decimal("5"), price_limit=Decimal("0.64"),
            book_hash="stop-frame",
        )
        repo.update_intent(
            str(intent["intent_id"]),
            state="RECONCILIATION_REQUIRED",
            submitted_at=now_iso(),
        )
        repo.update_intent(str(intent["intent_id"]), state="ZERO_FILL")
        result = repo.mark_position_resolved(
            str(position["position_id"]), winner=True, redeem_pending=True,
        )
        assert result["state"] == "REDEEM_PENDING"
    finally:
        temporary.cleanup()


def test_purely_local_intent_does_not_wedge_resolution():
    """A TAKE_PROFIT parked in WAITING_SELLABLE was never sent anywhere.

    It carries no remote effect to protect, so it must not hold the position
    out of resolution forever.
    """
    temporary, _base, repo, position = _resolution_race_repo()
    try:
        tp = repo.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
            shares=Decimal("5"), price_limit=Decimal("0.96"), book_hash="entry",
        )
        repo.mark_waiting_sellable(
            str(tp["intent_id"]),
            reason="TAKE_PROFIT_WAITING_FOR_FULL_SELLABLE_BALANCE",
        )
        result = repo.mark_position_resolved(
            str(position["position_id"]), winner=True, redeem_pending=True,
        )
        assert result["state"] == "REDEEM_PENDING"
    finally:
        temporary.cleanup()


# ---------------------------------------------------------------------------
# Deployment attestation gates real money
# ---------------------------------------------------------------------------

def test_dirty_working_tree_blocks_real_trading():
    ok, reasons = evaluate_gate(
        git_available=True, git_dirty=True, git_sha="abc",
        source_hash="hash", approved_git_sha="", approved_runtime_hash="",
        require_clean_runtime=True,
    )
    assert ok is False
    assert "WORKING_TREE_DIRTY" in reasons


def test_unavailable_git_provenance_fails_closed():
    ok, reasons = evaluate_gate(
        git_available=False, git_dirty=True, git_sha="",
        source_hash="hash", approved_git_sha="", approved_runtime_hash="",
        require_clean_runtime=True,
    )
    assert ok is False
    assert "GIT_PROVENANCE_UNAVAILABLE" in reasons


def test_runtime_hash_must_match_the_approved_deployment():
    ok, reasons = evaluate_gate(
        git_available=True, git_dirty=False, git_sha="abc",
        source_hash="actual", approved_git_sha="abc",
        approved_runtime_hash="expected", require_clean_runtime=True,
    )
    assert ok is False
    assert "RUNTIME_HASH_NOT_APPROVED" in reasons


def test_unapproved_commit_is_refused():
    ok, reasons = evaluate_gate(
        git_available=True, git_dirty=False, git_sha="actual",
        source_hash="hash", approved_git_sha="approved",
        approved_runtime_hash="", require_clean_runtime=True,
    )
    assert ok is False
    assert "GIT_SHA_NOT_APPROVED" in reasons


def test_clean_attested_runtime_passes_the_gate():
    ok, reasons = evaluate_gate(
        git_available=True, git_dirty=False, git_sha="abc",
        source_hash="hash", approved_git_sha="abc",
        approved_runtime_hash="hash", require_clean_runtime=True,
    )
    assert ok is True
    assert reasons == ()


def test_provenance_carries_no_secrets():
    from live import provenance

    config = LiveConfig(
        live_module_enabled=True,
        operator_token="super-secret-operator-token",
        session_secret="super-secret-session-secret",
    )
    payload = provenance.collect(config).as_dict()
    blob = repr(payload)
    assert "super-secret-operator-token" not in blob
    assert "super-secret-session-secret" not in blob
    assert payload["config_hash"]
    assert payload["run_id"]


# ===========================================================================
# FORENSIC SYNTHETIC SCENARIO SUITE
#
# SYNTHETIC — NOT A HISTORICAL EXACT REPLAY.
#
# One scenario per market from the investigation. The event ids and fill
# quantities are real; the order books, best-bid sequences, readiness frames
# and adapter responses are fabricated to reproduce the *failure class* that
# was proven for that market. The assertion in every case is the same
# invariant, never "this position would have been filled":
#
#   * the position stays under risk management,
#   * the exit obligation never silently disappears,
#   * a failure produces retry / reconciliation / quarantine, and
#   * entries stay blocked while exit safety is uncertain.
# ===========================================================================

def _assert_still_managed(repo, position, *, note):
    """The invariant: shares outstanding are always somebody's problem."""
    current = repo.position_for_token(position["token_id"])
    remaining = Decimal(current["remaining_shares_text"])
    if remaining <= 0:
        return current
    managed = {row["position_id"] for row in repo.risk_managed_positions()}
    assert position["position_id"] in managed, (
        f"{note}: position with {remaining} shares left risk management"
    )
    assert current["state"] not in {
        "CLOSED", "RESOLVED_LOSER", "REDEEMED",
    }, f"{note}: position was silently terminalised holding {remaining} shares"
    return current


def test_forensic_01_to_06_stop_fires_without_entry_readiness():
    """Six markets where the stop was never detected at all.

    Failure class: exit detection coupled to entry-side readiness. Synthetic
    input: a book that crosses 0.66 while the event is not entry-ready and
    entries are paused.
    """
    for event_id, quantity in (
        ("btc-updown-5m-1786275600", "5.428571"),
        ("btc-updown-5m-1786912500", "5.135134"),
        ("btc-updown-5m-1787258100", "5"),
        ("btc-updown-5m-1787960100", "5.066664"),
        ("btc-updown-5m-1788006300", "5.135134"),
        ("btc-updown-5m-1788033900", "5.205478"),
    ):
        adapter = RecordingSellAdapter()
        temp, base, repo, runtime, position = _case(
            f"forensic-{event_id}",
            shares=Decimal(quantity),
            paper=False,
            adapter=adapter,
            reconciliation=_ok_reconcile,
            event_id=event_id,
        )
        try:
            repo.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
            # event_ready=False is the condition that used to silence the stop.
            asyncio.run(runtime._manage_position(
                market={},
                update=dict(
                    _book(position["token_id"], "0.66", [("0.66", quantity)]),
                    _critical_stop_latched=True,
                ),
                event_ready=False,
                frame_hash=f"forensic-{event_id}",
            ))
            current = repo.position_for_token(position["token_id"])
            assert current["stop_stage"] == 1, f"{event_id}: stop never latched"
            assert not adapter.create_calls, (
                f"{event_id}: the not-ready frame path must hand off, not sell"
            )

            # The frame path deliberately stops at the latch when the event is
            # not entry-ready and wakes the supervisor instead. The supervisor
            # is the component that owes the SELL, and it sources its own book
            # (WS, else REST) rather than waiting for a readiness frame.
            runtime.set_exit_book_provider(
                lambda token, _q=quantity: _book(token, "0.66", [("0.66", _q)])
            )
            asyncio.run(runtime._refresh_hot_state_once())
            asyncio.run(runtime._drive_latched_exits_once())

            assert adapter.create_calls, (
                f"{event_id}: supervisor never attempted the SELL"
            )
            _assert_still_managed(repo, position, note=event_id)
        finally:
            temp.cleanup()


def test_forensic_07_and_08_waiting_sellable_is_a_durable_obligation():
    """Two markets that latched the stop and then stalled in WAITING_SELLABLE.

    Failure class: the exit parked waiting for a sellable balance and nothing
    ever woke it. Synthetic input: the exchange rejects for INSUFFICIENT
    BALANCE, then reconciliation reports the balance.
    """
    for event_id, quantity in (
        ("btc-updown-5m-1788036000", "5.588234"),
        ("btc-updown-5m-1788042600", "5.066664"),
    ):
        adapter = RecordingSellAdapter(response={
            "success": False,
            "status": "blocked",
            "submission_state": "NOT_SUBMITTED",
            "failure_reason": "INSUFFICIENT_BALANCE",
            "message": "INSUFFICIENT_BALANCE",
        })
        temp, base, repo, runtime, position = _case(
            f"forensic-{event_id}",
            shares=Decimal(quantity),
            sellable=Decimal("0"),
            paper=False,
            adapter=adapter,
            reconciliation=_ok_reconcile,
            event_id=event_id,
        )
        try:
            _manage(
                runtime,
                _book(position["token_id"], "0.66", [("0.66", quantity)]),
                "waiting",
            )
            stalled = repo.position_for_token(position["token_id"])
            waiting = repo.intent(stalled["active_exit_intent_id"])
            assert waiting["state"] == "WAITING_SELLABLE"
            assert stalled["stop_stage"] == 1, "the latch must be durable"
            _assert_still_managed(repo, position, note=f"{event_id} waiting")

            # Reconciliation reports the balance: the obligation resumes with
            # no new market frame required to re-derive it.
            repo.reconcile_remote_position(
                event_id=position["event_id"],
                condition_id=position["condition_id"],
                token_id=position["token_id"],
                outcome="YES",
                remote_shares=Decimal(quantity),
                average_price=Decimal("0.74"),
            )
            asyncio.run(runtime._refresh_hot_state_once())
            resumed = repo.position_for_token(position["token_id"])
            assert Decimal(resumed["sellable_shares_text"]) > 0
            assert resumed["stop_stage"] == 1
        finally:
            temp.cleanup()


def test_forensic_09_fak_rejection_keeps_the_exit_obligation():
    """A FAK rejection that was not retried hard enough.

    Failure class: a rejected exit treated as the end of the obligation.
    Synthetic input: the exchange rejects the order outright.
    """
    event_id = "btc-updown-5m-1788046200"
    adapter = RecordingSellAdapter(response={
        "success": False,
        "status": "rejected",
        "submission_state": "SUBMITTED",
        "failure_reason": "REJECTED",
        "message": "order rejected",
    })
    temp, base, repo, runtime, position = _case(
        f"forensic-{event_id}",
        shares=Decimal("5.066664"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        event_id=event_id,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5.066664")]),
            "rejected",
        )
        current = _assert_still_managed(repo, position, note=event_id)
        assert current["stop_stage"] == 1
        # The rejection is recorded, and the position is still exiting rather
        # than quietly closed.
        assert current["state"] in {"EXITING", "EXIT_RECONCILIATION_REQUIRED"}
    finally:
        temp.cleanup()


def test_forensic_10_sub_minimum_partial_buy_never_leaves_risk_management():
    """The 3.68-share partial BUY that became DUST and left the exit path.

    Failure class: a remainder below the exchange minimum dropping out of risk
    management. No order may be sent below the minimum, and the position must
    still be managed and still block entries.
    """
    event_id = "btc-updown-5m-1788062100"
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        f"forensic-{event_id}",
        shares=Decimal("3.68"),
        minimum=Decimal("5"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        event_id=event_id,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "10")]),
            "dust-stop",
        )
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert Decimal(current["remaining_shares_text"]) == Decimal("3.68")
        assert not adapter.create_calls, (
            "an order below the exchange minimum must never be submitted"
        )
        _assert_still_managed(repo, position, note=event_id)
        assert [
            row["position_id"] for row in repo.entry_blocking_positions()
        ] == [position["position_id"]], "entries must stay blocked"
    finally:
        temp.cleanup()


def test_forensic_11_no_liquidity_above_floor_is_not_a_terminal_state():
    """Three FAK attempts with no liquidity above the protected floor.

    Failure class: an exit that cannot fill being treated as finished. Nobody
    can conjure a bid, so the requirement is only that the position stays
    managed and the failure stays visible.
    """
    event_id = "btc-updown-5m-1788082200"
    temp, base, repo, runtime, position = _case(
        f"forensic-{event_id}",
        shares=Decimal("5.205478"),
        event_id=event_id,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "1")]),
            "thin",
        )
        asyncio.run(runtime._refresh_hot_state_once())
        _manage(
            runtime,
            _book(position["token_id"], None, []),
            "empty-book",
        )
        current = _assert_still_managed(repo, position, note=event_id)
        assert current["state"] == "EXITING"
        assert current["stop_stage"] == 1
        with base.connect() as conn:
            documented = conn.execute(
                "SELECT COUNT(*) FROM live_audit_timeline "
                "WHERE reason_code='EXIT_NO_LIQUIDITY_ABOVE_FLOOR'"
            ).fetchone()[0]
        assert documented >= 1, "the failure must be recorded, not swallowed"
    finally:
        temp.cleanup()


def test_forensic_transport_unknown_requires_proof_before_retry():
    """The SELL whose HTTP response was lost.

    Failure class: a TransportError leaving the remote effect unknown. A blind
    retry risks selling twice, so the position parks in a reconciliation state
    and the intent stays non-terminal until authoritative evidence arrives.
    """
    event_id = "btc-updown-5m-1786057500"
    adapter = RecordingSellAdapter(response={
        "success": False,
        "status": "unknown",
        "submission_state": "UNKNOWN",
        "failure_reason": "TRANSPORT_ERROR",
        "message": "TransportError: Request failed",
    })
    temp, base, repo, runtime, position = _case(
        f"forensic-{event_id}",
        shares=Decimal("5"),
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        event_id=event_id,
    )
    try:
        _manage(
            runtime,
            _book(position["token_id"], "0.66", [("0.66", "5")]),
            "transport-error",
        )
        current = _assert_still_managed(repo, position, note=event_id)
        intents = _exit_intents(base, position["position_id"])
        assert intents, "the exit intent must survive the transport failure"
        assert len(adapter.create_calls) == 1, (
            "an UNKNOWN submission must not be blindly retried"
        )
        assert current["stop_stage"] == 1
    finally:
        temp.cleanup()
