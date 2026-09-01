import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile

from live.adapters.mock import MockTradingAdapter
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository, now_iso
from live.strategy_repository import StrategyRepository


class TokenBalanceAdapter(MockTradingAdapter):
    def __init__(self, balances):
        super().__init__()
        self.balances = dict(balances)

    async def get_token_balance(self, token_id):
        value = self.balances.get(token_id)
        if value is None:
            return {"status": "unavailable"}
        return {"status": "mock", "balance_text": str(value)}


def residue_case(name, *, remaining=Decimal("0.005"), minimum=Decimal("5")):
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    event_id = f"event-{name}"
    condition_id = f"condition-{name}"
    token_id = f"token-{name}"
    other_token_id = f"other-{name}"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": other_token_id,
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": str(minimum),
        "min_tick_size": "0.01",
    })
    reservation = repo.reserve_event_entry(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        side="YES",
        simultaneous=False,
        reason_code="ENTRY_PRICE_EXACT",
    )
    position = repo.open_position(
        event_id=event_id,
        condition_id=condition_id,
        token_id=token_id,
        outcome="YES",
        shares=Decimal("10"),
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("7.4"),
        fees=Decimal("0"),
        sellable_shares=Decimal("10"),
        min_sellable=minimum,
        entry_intent_id=reservation["entry_intent_id"],
    )
    sold = Decimal("10") - remaining
    intent = repo.reserve_position_intent(
        position,
        action="EXIT",
        purpose="STOP_066",
        order_type="FAK",
        shares=Decimal("10"),
        price_limit=Decimal("0.60"),
        book_hash=f"book-{name}",
    )
    repo.add_fill(
        intent_id=intent["intent_id"],
        remote_trade_id=f"trade-{name}",
        shares=sold,
        price=Decimal("0.60"),
        fee=Decimal("0"),
        status="MATCHED",
        matched_at=now_iso(),
    )
    repo.apply_exit_fill(
        position_id=position["position_id"],
        intent_id=intent["intent_id"],
        sold_shares=sold,
        average_price=Decimal("0.60"),
        fees=Decimal("0"),
        final_state="PARTIAL_FINAL",
        min_sellable=minimum,
        purpose="STOP_066",
        book_hash=f"book-{name}",
    )
    # Reproduce the legacy lifecycle bug: accounting/fill evidence is final,
    # but the position remained EXITING instead of terminal DUST.
    with base.connect() as conn:
        conn.execute(
            "UPDATE live_strategy_positions SET state='EXITING' "
            "WHERE position_id=?",
            (position["position_id"],),
        )
        conn.commit()
    return (
        temporary,
        base,
        repo,
        repo.position_for_token(token_id),
        other_token_id,
    )


def terminalize(repo, position, *, minimum=Decimal("5")):
    return repo.terminalize_exit_residue(
        position_id=position["position_id"],
        authoritative_balance=Decimal(position["remaining_shares_text"]),
        min_order_size=minimum,
        actor="test",
        evidence_source="conditional_token_balance",
    )


def test_A_partial_exit_below_minimum_terminalizes_without_changing_accounting():
    temporary, base, repo, position, _other = residue_case("A")
    try:
        pnl_before = position["realized_pnl_text"]
        exit_value_before = position["exit_value_text"]
        result = terminalize(repo, position)
        current = repo.position_for_token(position["token_id"])

        assert result["status"] == "repaired"
        assert current["state"] == "DUST"
        assert current["closed_at"]
        assert current["remaining_shares_text"] == "0.005"
        assert current["dust_shares_text"] == "0.005"
        assert current["sellable_shares_text"] == "0"
        assert current["realized_pnl_text"] == pnl_before
        assert current["exit_value_text"] == exit_value_before
        assert (
            current["exit_obligation_reason"]
            == "TERMINAL_EXIT_RESIDUE_BELOW_MIN_ORDER"
        )
        with base.connect() as conn:
            audit = conn.execute(
                "SELECT action,reason FROM live_audit_log "
                "WHERE action='terminalize_exit_residue' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert tuple(audit) == (
            "terminalize_exit_residue",
            "TERMINAL_EXIT_RESIDUE_BELOW_MIN_ORDER",
        )
    finally:
        temporary.cleanup()


def test_B_unresolved_submitted_sell_keeps_residue_fail_closed():
    temporary, _base, repo, position, _other = residue_case("B")
    try:
        unresolved = repo.reserve_position_intent(
            position,
            action="EXIT",
            purpose="STOP_066",
            order_type="FAK",
            shares=Decimal("0.005"),
            price_limit=Decimal("0.01"),
            book_hash="unknown-sell",
        )
        repo.update_intent(
            unresolved["intent_id"],
            state="RECONCILIATION_REQUIRED",
            submitted_at=now_iso(),
            remote_order_id="remote-unknown",
            normalized_error="UNKNOWN_TRANSPORT",
        )

        result = terminalize(
            repo, repo.position_for_token(position["token_id"])
        )

        assert result["status"] == "not_eligible"
        assert "UNRESOLVED_EXIT_INTENT" in result["blockers"]
        assert "UNRESOLVED_SUBMITTED_SELL" in result["blockers"]
        assert repo.position_for_token(position["token_id"])["state"] == "EXITING"
    finally:
        temporary.cleanup()


def test_C_actionable_remainder_does_not_terminalize():
    temporary, _base, repo, position, _other = residue_case(
        "C", remaining=Decimal("5"), minimum=Decimal("5")
    )
    try:
        result = terminalize(repo, position)
        assert result["status"] == "not_eligible"
        assert "RESIDUE_IS_ACTIONABLE" in result["blockers"]
        assert repo.position_for_token(position["token_id"])["state"] == "EXITING"
    finally:
        temporary.cleanup()


def test_D_open_dust_remains_active_and_entry_blocking():
    temporary = tempfile.TemporaryDirectory()
    try:
        base = LiveRepository(Path(temporary.name) / "live.sqlite3")
        base.migrate()
        repo = StrategyRepository(base)
        repo.migrate()
        reservation = repo.reserve_event_entry(
            event_id="open-dust",
            condition_id="open-dust-condition",
            token_id="open-dust-token",
            side="YES",
            simultaneous=False,
            reason_code="ENTRY_PRICE_EXACT",
        )
        position = repo.open_position(
            event_id="open-dust",
            condition_id="open-dust-condition",
            token_id="open-dust-token",
            outcome="YES",
            shares=Decimal("0.005"),
            average_price=Decimal("0.74"),
            cost_all_in=Decimal("0.0037"),
            fees=Decimal("0"),
            sellable_shares=Decimal("0"),
            min_sellable=Decimal("5"),
            entry_intent_id=reservation["entry_intent_id"],
        )
        assert position["state"] == "DUST"
        assert position["closed_at"] is None
        assert [p["position_id"] for p in repo.entry_blocking_positions()] == [
            position["position_id"]
        ]
        assert [p["position_id"] for p in repo.active_positions()] == [
            position["position_id"]
        ]
        assert repo.exposure() > 0
    finally:
        temporary.cleanup()


def test_E_closed_terminal_dust_is_not_active_but_remains_auditable():
    temporary, _base, repo, position, _other = residue_case("E")
    try:
        terminalize(repo, position)
        assert repo.entry_blocking_positions() == []
        assert repo.active_positions() == []
        assert repo.exposure() == 0
        managed = {
            item["position_id"] for item in repo.risk_managed_positions()
        }
        assert position["position_id"] in managed
    finally:
        temporary.cleanup()


def test_F_closed_terminal_dust_does_not_create_reconciliation_gap():
    temporary, base, repo, position, other_token = residue_case("F")
    try:
        base.mark_market_resolved(
            position["condition_id"], other_token, "NO"
        )
        adapter = TokenBalanceAdapter({
            position["token_id"]: position["remaining_shares_text"],
        })
        result = asyncio.run(
            ReconciliationWorker(base, adapter, repo).run_once("test")
        )

        assert result["status"] == "ok", result
        assert result["gaps"] == []
        assert any(
            repair["type"] == "terminal_exit_residue"
            for repair in result["repairs"]
        )
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert current["closed_at"]
        assert repo.entry_blocking_positions() == []
    finally:
        temporary.cleanup()


def test_H_historical_terminal_dust_remains_visible_after_clean_reconciliation():
    temporary, base, repo, position, other_token = residue_case("H")
    try:
        terminalize(repo, position)
        base.mark_market_resolved(
            position["condition_id"], other_token, "NO"
        )
        adapter = TokenBalanceAdapter({
            position["token_id"]: position["remaining_shares_text"],
        })
        result = asyncio.run(
            ReconciliationWorker(base, adapter, repo).run_once("test")
        )
        assert result["status"] == "ok", result
        assert repo.position_for_token(position["token_id"])["state"] == "DUST"
        assert position["position_id"] in {
            item["position_id"] for item in repo.risk_managed_positions()
        }
    finally:
        temporary.cleanup()
