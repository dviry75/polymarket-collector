from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from live.adapters.mock import MockTradingAdapter
from live.reconciliation import ReconciliationWorker
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def _case(tmp_path):
    base = LiveRepository(tmp_path / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    base.upsert_market({
        "event_id": "event-targeted",
        "condition_id": "condition-targeted",
        "yes_token_id": "token-targeted",
        "no_token_id": "other-targeted",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "1",
        "min_tick_size": "0.01",
        "taker_base_fee": 0,
    })
    repo.reserve_event_entry(
        event_id="event-targeted",
        condition_id="condition-targeted",
        token_id="token-targeted",
        side="YES",
        simultaneous=False,
        reason_code="ENTRY_PRICE_EXACT",
    )
    position = repo.open_position(
        event_id="event-targeted",
        condition_id="condition-targeted",
        token_id="token-targeted",
        outcome="YES",
        shares=Decimal("8"),
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("5.92"),
        fees=Decimal("0"),
        sellable_shares=Decimal("0"),
        min_sellable=Decimal("1"),
    )
    adapter = MockTradingAdapter()
    worker = ReconciliationWorker(base, adapter, repo)
    return base, repo, adapter, worker, position


def test_targeted_position_updates_sellability_only_and_does_not_publish_ready(
    tmp_path,
) -> None:
    base, repo, adapter, worker, position = _case(tmp_path)
    base.set_state("reconciliation_readiness", "NOT_READY", "test")
    adapter.positions = [{
        "condition_id": position["condition_id"],
        "token_id": position["token_id"],
        "outcome": "YES",
        "size": "8",
        "average_price": "0.74",
        "current_value": "5.92",
    }]

    result = asyncio.run(worker.run_targeted_position(
        token_id=position["token_id"], actor="test"
    ))

    assert result == {"status": "ok", "changed": True}
    current = repo.position_for_token(position["token_id"])
    assert current["remaining_shares_text"] == "8"
    assert current["sellable_shares_text"] == "8"
    assert base.get_state("reconciliation_readiness") == "NOT_READY"
    with base.connect() as connection:
        row = connection.execute(
            "SELECT parameters_json FROM live_audit_timeline "
            "WHERE reason_code='REMOTE_POSITION_CORRECTION' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    payload = json.loads(row["parameters_json"])
    assert payload["change_kind"] == "SELLABLE_VISIBILITY_CHANGED"
    assert payload["before"]["sellable_shares_text"] == "0"
    assert payload["remote"]["shares_text"] == "8"
    assert payload["after"]["sellable_shares_text"] == "8"
    assert payload["sellable_delta_text"] == "8"


def test_targeted_position_refuses_financial_mismatch(tmp_path) -> None:
    _base, repo, adapter, worker, position = _case(tmp_path)
    adapter.positions = [{
        "condition_id": position["condition_id"],
        "token_id": position["token_id"],
        "outcome": "YES",
        "size": "7",
        "average_price": "0.74",
    }]

    result = asyncio.run(worker.run_targeted_position(
        token_id=position["token_id"], actor="test"
    ))

    assert result["status"] == "requires_full"
    assert result["reason"] == "FINANCIAL_MISMATCH"
    current = repo.position_for_token(position["token_id"])
    assert current["remaining_shares_text"] == "8"
    assert current["sellable_shares_text"] == "0"
