"""Regression: historical DUST must never enter A1's latched-exit tier 0.

Focused corrective-patch coverage for the post-A1 regression where a
``state == 'DUST'`` position that still carried ``stop_stage >= 1`` was
classified as tier 0 (precedence bug in ``_exit_priority_tier``) and then
fanned out into ``_submit_latched_exit`` -- the fast urgent-exit path.

A1 itself is unchanged; genuine latched STOP exits must keep the same
fast tier-0 behaviour.
"""

import asyncio
from decimal import Decimal

from live.strategy_runtime import LiveStrategyRuntime

from test_latched_market_exit import _book, _case


def _force_state(base, position_id, *, state, stop_stage=None, closed_at=None):
    sets = ["state=?"]
    params = [state]
    if stop_stage is not None:
        sets.append("stop_stage=?")
        params.append(int(stop_stage))
    if closed_at is not None:
        sets.append("closed_at=?")
        params.append(closed_at)
    params.append(position_id)
    with base.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE live_strategy_positions SET {','.join(sets)} "
            "WHERE position_id=?",
            params,
        )
        conn.commit()


def _add_position(base, repo, name, *, state, stop_stage, closed_at=None):
    """Insert one extra position (own event/condition/token) into an
    existing test repo and coerce it to the requested terminal shape."""
    event_id = f"backlog-{name}"
    condition_id = f"condition-{name}"
    token_id = f"token-{name}"
    base.upsert_market({
        "event_id": event_id,
        "condition_id": condition_id,
        "yes_token_id": token_id,
        "no_token_id": f"no-{name}",
        "token_mapping_status": "verified",
        "accepting_orders": True,
        "min_order_size": "1",
        "min_tick_size": "0.01",
        "taker_base_fee": 0,
    })
    repo.reserve_event_entry(
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
        shares=Decimal("5"),
        average_price=Decimal("0.74"),
        cost_all_in=Decimal("3.70"),
        fees=Decimal("0"),
        sellable_shares=Decimal("5"),
        min_sellable=Decimal("1"),
    )
    if stop_stage >= 1:
        repo.latch_stop_exit(str(position["position_id"]))
    _force_state(
        base,
        str(position["position_id"]),
        state=state,
        stop_stage=stop_stage,
        closed_at=closed_at,
    )
    return position


def _install_spies(runtime):
    calls = {
        "submit": [],
        "book": [],
        "stop_plan": [],
        "fak": [],
    }
    original_submit = runtime._submit_latched_exit
    original_book = runtime._exit_book_for_position
    original_plan = runtime._stop_attempt_plan
    original_fak = runtime._market_exit_fak

    async def submit(position_id):
        calls["submit"].append(str(position_id))
        return await original_submit(position_id)

    async def book(position, *args, **kwargs):
        calls["book"].append(str(position.get("token_id") or ""))
        return await original_book(position, *args, **kwargs)

    def stop_plan(position, *args, **kwargs):
        calls["stop_plan"].append(str(position.get("token_id") or ""))
        return original_plan(position, *args, **kwargs)

    async def fak(position, *args, **kwargs):
        calls["fak"].append(str(position.get("token_id") or ""))
        return await original_fak(position, *args, **kwargs)

    runtime._submit_latched_exit = submit
    runtime._exit_book_for_position = book
    runtime._stop_attempt_plan = stop_plan
    runtime._market_exit_fak = fak
    return calls


# --------------------------------------------------------------------------
# Test 1 -- DUST + stop_stage>=1 is never tier 0
# --------------------------------------------------------------------------
def test_dust_with_stop_stage_is_not_tier_zero():
    tier = LiveStrategyRuntime._exit_priority_tier

    # Historical (terminal) DUST: closed_at set -> existing tier 4.
    assert tier({
        "state": "DUST",
        "stop_stage": 1,
        "closed_at": "2026-09-01T20:23:00+00:00",
    }) == 4

    # DUST without closed_at keeps its existing tier 3 classification.
    assert tier({"state": "DUST", "stop_stage": 1}) == 3
    assert tier({"state": "DUST", "stop_stage": 2}) == 3

    # And identical to a DUST row that never latched a stop.
    assert tier({"state": "DUST", "stop_stage": 1, "closed_at": "x"}) == tier(
        {"state": "DUST", "stop_stage": 0, "closed_at": "x"}
    )
    assert tier({"state": "DUST", "stop_stage": 1}) == tier(
        {"state": "DUST", "stop_stage": 0}
    )


# --------------------------------------------------------------------------
# Test 2 -- genuine latched STOP stays tier 0
# --------------------------------------------------------------------------
def test_genuine_latched_stop_remains_tier_zero():
    tier = LiveStrategyRuntime._exit_priority_tier

    assert tier({"state": "EXITING", "stop_stage": 1}) == 0
    assert tier({"state": "OPEN", "stop_stage": 1}) == 0
    assert tier({"state": "TP_OPEN", "stop_stage": 1}) == 0
    assert tier({"state": "EXIT_RECONCILIATION_REQUIRED", "stop_stage": 0}) == 0
    assert tier({"state": "EXITING", "stop_stage": 0}) == 0


# --------------------------------------------------------------------------
# Test 3 -- _submit_latched_exit rejects DUST defensively
# --------------------------------------------------------------------------
def test_submit_latched_exit_rejects_dust_defensively():
    temp, base, repo, runtime, position = _case("dust-defensive")
    try:
        repo.latch_stop_exit(str(position["position_id"]))
        _force_state(
            base,
            str(position["position_id"]),
            state="DUST",
            stop_stage=1,
            closed_at="2026-09-01T20:23:00+00:00",
        )
        asyncio.run(runtime._refresh_hot_state_once())

        calls = _install_spies(runtime)
        # Force a usable book to exist -- the guard must fire before any of
        # this is consulted.
        runtime.set_exit_book_provider(
            lambda _token_id: _book(position["token_id"], "0.66", [("0.66", "5")])
        )

        asyncio.run(
            runtime._submit_latched_exit(str(position["position_id"]))
        )

        assert calls["book"] == []
        assert calls["stop_plan"] == []
        assert calls["fak"] == []
        with base.connect() as conn:
            intent_count = conn.execute(
                "SELECT COUNT(*) FROM live_strategy_intents "
                "WHERE position_id=? AND action='EXIT'",
                (str(position["position_id"]),),
            ).fetchone()[0]
        assert intent_count == 0
        assert runtime.exit_rest_fallbacks == 0
        # Position was left untouched as terminal DUST.
        current = repo.position_for_token(position["token_id"])
        assert current["state"] == "DUST"
        assert current["stop_stage"] == 1
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# Test 4 -- large historical DUST backlog never fans out; the one real
# latched STOP is still selected immediately for tier 0.
# --------------------------------------------------------------------------
def test_large_dust_backlog_does_not_reach_latched_exit():
    temp, base, repo, runtime, position = _case("dust-backlog-real")
    try:
        # The one genuine latched STOP position.
        repo.latch_stop_exit(str(position["position_id"]))
        real_token = str(position["token_id"])
        real_id = str(position["position_id"])

        dust_tokens = set()
        dust_ids = set()
        for i in range(130):
            dust = _add_position(
                base,
                repo,
                f"dust-{i}",
                state="DUST",
                stop_stage=1,
                closed_at="2026-09-01T20:23:00+00:00",
            )
            dust_tokens.add(str(dust["token_id"]))
            dust_ids.add(str(dust["position_id"]))

        asyncio.run(runtime._refresh_hot_state_once())

        # Classification proof on the live snapshot.
        snapshot = runtime._hot_state.get("positions_by_token") or {}
        flat = [
            dict(p)
            for token_positions in snapshot.values()
            for p in token_positions
        ]
        assert len(flat) == 131
        for p in flat:
            tier = LiveStrategyRuntime._exit_priority_tier(p)
            if str(p["position_id"]) == real_id:
                assert tier == 0
            else:
                assert tier != 0

        calls = _install_spies(runtime)
        runtime.set_exit_book_provider(
            lambda token_id: (
                _book(real_token, "0.66", [("0.66", "5")])
                if str(token_id) == real_token
                else None
            )
        )

        asyncio.run(runtime._drive_latched_exits_once())

        # Only the genuine STOP was submitted to the latched fast path.
        assert calls["submit"] == [real_id]
        # No DUST token ever triggered a stop plan, a FAK, or a REST book.
        assert dust_tokens.isdisjoint(calls["stop_plan"])
        assert dust_tokens.isdisjoint(calls["fak"])
        assert calls["fak"] == [real_token]
        assert runtime.exit_rest_fallbacks == 0
        # No EXIT intent for any DUST position.
        with base.connect() as conn:
            dust_intents = conn.execute(
                "SELECT COUNT(*) FROM live_strategy_intents WHERE action='EXIT' "
                f"AND position_id IN ({','.join('?' * len(dust_ids))})",
                tuple(dust_ids),
            ).fetchone()[0]
        assert dust_intents == 0
    finally:
        temp.cleanup()


# --------------------------------------------------------------------------
# Test 5 -- mixed backlog: only genuinely eligible positions reach tier 0
# --------------------------------------------------------------------------
def test_mixed_backlog_only_real_exits_reach_tier_zero():
    tier = LiveStrategyRuntime._exit_priority_tier

    cases = {
        "dust_ss0": ({"state": "DUST", "stop_stage": 0, "closed_at": "x"}, 4),
        "dust_ss1": ({"state": "DUST", "stop_stage": 1, "closed_at": "x"}, 4),
        "dust_ss2": ({"state": "DUST", "stop_stage": 2, "closed_at": "x"}, 4),
        "dust_open_slot": ({"state": "DUST", "stop_stage": 1}, 3),
        "open_ss0": ({"state": "OPEN", "stop_stage": 0}, 1),
        "open_ss1": ({"state": "OPEN", "stop_stage": 1}, 0),
        "exiting": ({"state": "EXITING", "stop_stage": 1}, 0),
        "recon": (
            {"state": "EXIT_RECONCILIATION_REQUIRED", "stop_stage": 0},
            0,
        ),
    }
    for name, (position, expected) in cases.items():
        assert tier(position) == expected, name

    tier_zero = {
        name for name, (position, _) in cases.items() if tier(position) == 0
    }
    assert tier_zero == {"open_ss1", "exiting", "recon"}
    # No DUST variant is eligible for the urgent latched-exit path.
    assert not any(name.startswith("dust") for name in tier_zero)
