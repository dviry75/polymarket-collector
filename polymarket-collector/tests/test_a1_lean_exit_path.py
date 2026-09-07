import asyncio
from dataclasses import replace
from decimal import Decimal

from test_latched_market_exit import (
    RecordingSellAdapter,
    _book,
    _case,
    _exit_intents,
    _ok_reconcile,
)


def _latch_and_prepare(runtime, repo, position, update):
    repo.latch_stop_exit(str(position["position_id"]))

    async def prepare():
        await runtime._refresh_hot_state_once()
        runtime.set_exit_book_provider(
            lambda token_id: (
                dict(update)
                if str(token_id) == str(position["token_id"])
                else None
            )
        )

    asyncio.run(prepare())


def test_frame_storm_event_lock_does_not_delay_latched_sell():
    temp, _base, repo, runtime, position = _case("a1-event-independent")
    update = _book(position["token_id"], "0.66", [("0.66", "5")])
    try:
        _latch_and_prepare(runtime, repo, position, update)

        async def scenario():
            event_lock = runtime._event_locks.setdefault(
                str(position["event_id"]), asyncio.Lock()
            )
            await event_lock.acquire()
            sell_started = asyncio.Event()
            original = runtime._market_exit_fak

            async def observed(*args, **kwargs):
                sell_started.set()
                await original(*args, **kwargs)

            runtime._market_exit_fak = observed
            task = asyncio.create_task(
                runtime._submit_latched_exit(str(position["position_id"]))
            )
            try:
                await asyncio.wait_for(sell_started.wait(), 1.0)
                assert event_lock.locked()
                await asyncio.wait_for(task, 1.0)
            finally:
                if event_lock.locked():
                    event_lock.release()
                if not task.done():
                    task.cancel()

        asyncio.run(scenario())
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_two_concurrent_latched_submissions_send_one_sell():
    temp, base, repo, runtime, position = _case("a1-double-submit")
    update = _book(position["token_id"], "0.66", [("0.66", "5")])
    try:
        _latch_and_prepare(runtime, repo, position, update)

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            original = runtime._market_exit_fak
            active = 0
            maximum = 0

            async def blocked(*args, **kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                entered.set()
                await release.wait()
                try:
                    await original(*args, **kwargs)
                finally:
                    active -= 1

            runtime._market_exit_fak = blocked
            first = asyncio.create_task(
                runtime._submit_latched_exit(str(position["position_id"]))
            )
            second = asyncio.create_task(
                runtime._submit_latched_exit(str(position["position_id"]))
            )
            await asyncio.wait_for(entered.wait(), 1.0)
            await asyncio.sleep(0)
            assert maximum == 1
            assert not second.done() or not first.done()
            release.set()
            await asyncio.gather(first, second)
            return maximum

        assert asyncio.run(scenario()) == 1
        assert len(_exit_intents(base, position["position_id"])) == 1
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_frame_latch_only_then_supervisor_submits():
    temp, _base, repo, runtime, position = _case("a1-handoff")
    update = _book(position["token_id"], "0.66", [("0.66", "5")])
    try:
        calls = []
        original = runtime._market_exit_fak

        async def observed(*args, **kwargs):
            calls.append(kwargs.get("purpose"))
            await original(*args, **kwargs)

        runtime._market_exit_fak = observed

        async def scenario():
            runtime.set_exit_book_provider(lambda _token_id: dict(update))
            await runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="frame",
            )
            current = repo.position_for_token(position["token_id"])
            assert current["stop_stage"] == 1
            assert current["state"] == "EXITING"
            assert calls == []
            assert runtime._exit_wakeup.is_set()
            await runtime._drive_latched_exits_once()

        asyncio.run(scenario())
        assert calls == ["STOP_066"]
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_submit_latched_exit_never_acquires_event_lock():
    temp, _base, repo, runtime, position = _case("a1-lock-order")
    update = _book(position["token_id"], "0.66", [("0.66", "5")])
    try:
        _latch_and_prepare(runtime, repo, position, update)

        class ExplodingEventLock:
            async def __aenter__(self):
                raise AssertionError("tier-0 exit attempted to acquire event lock")

            async def __aexit__(self, *_args):
                return False

        runtime._event_locks[str(position["event_id"])] = ExplodingEventLock()
        asyncio.run(runtime._submit_latched_exit(str(position["position_id"])))
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_take_profit_creation_is_serialized_behind_stop_exit():
    temp, base, repo, runtime, position = _case("a1-tp-race")
    stale_open_position = dict(position)
    update = _book(position["token_id"], "0.66", [("0.66", "5")])
    try:
        _latch_and_prepare(runtime, repo, position, update)

        async def scenario():
            sell_started = asyncio.Event()
            release_sell = asyncio.Event()
            original = runtime._market_exit_fak

            async def blocked(*args, **kwargs):
                sell_started.set()
                await release_sell.wait()
                await original(*args, **kwargs)

            runtime._market_exit_fak = blocked
            exit_task = asyncio.create_task(
                runtime._submit_latched_exit(str(position["position_id"]))
            )
            await asyncio.wait_for(sell_started.wait(), 1.0)
            tp_task = asyncio.create_task(
                runtime._ensure_take_profit(stale_open_position)
            )
            await asyncio.sleep(0)
            assert not tp_task.done()
            release_sell.set()
            await asyncio.gather(exit_task, tp_task)

        asyncio.run(scenario())
        with base.connect() as conn:
            tp_count = conn.execute(
                "SELECT COUNT(*) FROM live_strategy_intents "
                "WHERE position_id=? AND purpose='TAKE_PROFIT'",
                (str(position["position_id"]),),
            ).fetchone()[0]
        assert tp_count == 0
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_waiting_sellable_exit_resumes_without_new_duplicate_intent():
    adapter = RecordingSellAdapter()
    temp, base, repo, runtime, position = _case(
        "a1-waiting-resume",
        paper=False,
        adapter=adapter,
        reconciliation=_ok_reconcile,
        config_overrides={"stop_optimistic_submit_enabled": False},
    )
    update = _book(position["token_id"], "0.60", [("0.60", "5")])
    try:
        latched = repo.latch_stop_exit(str(position["position_id"]))
        waiting = repo.reserve_position_intent(
            latched,
            action="EXIT",
            purpose="STOP_066",
            order_type="FAK",
            shares=Decimal("5"),
            price_limit=Decimal("0.01"),
            book_hash="waiting",
        )
        repo.mark_waiting_sellable(
            str(waiting["intent_id"]), reason="TEST_WAITING"
        )
        repo.reconcile_remote_position(
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            outcome="YES",
            remote_shares=Decimal("5"),
            average_price=Decimal("0.74"),
        )
        asyncio.run(runtime._refresh_hot_state_once())
        runtime.set_exit_book_provider(lambda _token_id: dict(update))

        asyncio.run(runtime._submit_latched_exit(str(position["position_id"])))

        intents = _exit_intents(base, position["position_id"])
        assert len(adapter.create_calls) == 1
        assert len(intents) == 2
        assert repo.intent(str(waiting["intent_id"]))["state"] == "CANCELED"
        assert sum(
            item["state"] not in {
                "FILLED", "PARTIAL_FINAL", "ZERO_FILL", "CANCELED",
                "REJECTED", "FAILED", "SETTLED",
            }
            for item in intents
        ) == 1
    finally:
        temp.cleanup()


def test_initial_exit_evaluation_promotes_and_submits_same_tick():
    temp, _base, repo, runtime, position = _case("a1-initial-eval")
    update = _book(position["token_id"], "0.65", [("0.65", "5")])
    try:
        runtime._pending_initial_eval.add(str(position["position_id"]))
        runtime.set_exit_book_provider(lambda _token_id: dict(update))
        selected = []
        original = runtime._submit_latched_exit

        async def observed(position_id):
            selected.append(position_id)
            await original(position_id)

        runtime._submit_latched_exit = observed
        asyncio.run(runtime._drive_latched_exits_once())

        assert selected == [str(position["position_id"])]
        current = repo.position_for_token(position["token_id"])
        assert current["stop_stage"] == 1
        assert current["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_emergency_close_and_supervisor_share_position_lock():
    temp, _base, repo, runtime, position = _case(
        "a1-emergency-race", reconciliation=_ok_reconcile
    )
    update = _book(position["token_id"], "0.60", [("0.60", "5")])
    try:
        _latch_and_prepare(runtime, repo, position, update)
        entered = None
        release = None
        call_count = 0
        original = runtime._market_exit_fak

        class Book:
            ready = True
            generation = 1
            update_number = 1
            last_message_hash = "emergency"

            def view(self, **_kwargs):
                return dict(update)

        class Books:
            books = {str(position["token_id"]): Book()}

        async def scenario():
            nonlocal entered, release, call_count
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocked(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                entered.set()
                await release.wait()
                await original(*args, **kwargs)

            runtime._market_exit_fak = blocked
            supervisor = asyncio.create_task(
                runtime._submit_latched_exit(str(position["position_id"]))
            )
            await asyncio.wait_for(entered.wait(), 1.0)
            emergency = asyncio.create_task(
                runtime.emergency_close_all(Books(), actor="test")
            )
            await asyncio.sleep(0)
            assert not emergency.done()
            release.set()
            result, emergency_result = await asyncio.gather(
                supervisor, emergency
            )
            assert result is None
            assert emergency_result["status"] == "executed"

        asyncio.run(scenario())
        assert call_count == 1
        assert repo.position_for_token(position["token_id"])["state"] == "CLOSED"
    finally:
        temp.cleanup()


def test_stop_retry_throttle_is_preserved_in_new_path():
    temp, _base, repo, runtime, position = _case("a1-retry-throttle")
    books = iter((
        _book(position["token_id"], "0.66", [] , generation=1),
        _book(position["token_id"], "0.65", [] , generation=2),
    ))
    try:
        runtime.config = replace(
            runtime.config,
            stop_loss_retry_delay_ms=60_000,
        )
        repo.latch_stop_exit(str(position["position_id"]))
        asyncio.run(runtime._refresh_hot_state_once())
        runtime.set_exit_book_provider(
            lambda _token_id: dict(next(books))
        )
        calls = []
        original = runtime._market_exit_fak

        async def observed(*args, **kwargs):
            calls.append(kwargs.get("frame_hash"))
            await original(*args, **kwargs)

        runtime._market_exit_fak = observed
        asyncio.run(runtime._submit_latched_exit(str(position["position_id"])))
        asyncio.run(runtime._submit_latched_exit(str(position["position_id"])))

        assert len(calls) == 1
        assert repo.stop_attempt_state(
            str(position["position_id"])
        )["attempt_count"] == 1
    finally:
        temp.cleanup()


def test_latched_path_preserves_sla_and_exit_evidence():
    temp, _base, repo, runtime, position = _case("a1-telemetry")
    update = {
        **_book(position["token_id"], "0.64", [("0.64", "5")]),
        "message_hash": "a1-telemetry-book",
        "received_at": "2026-09-07T17:00:00+00:00",
    }
    try:
        async def scenario():
            runtime.set_exit_book_provider(lambda _token_id: dict(update))
            await runtime._manage_position(
                market={},
                update=update,
                event_ready=True,
                frame_hash="a1-frame",
                received_at=update["received_at"],
            )
            await runtime._drive_latched_exits_once()

        asyncio.run(scenario())
        health = runtime._exit_tracker.health()
        assert len(health) == 1
        record = health[0]
        assert record["latched_exit_selected_at"]
        assert record["latched_exit_orchestration_started_at"]
        assert record["market_exit_fak_started_at"]

        runtime._exit_evidence.drain_pending()
        runtime._exit_evidence.reconcile_active(set())
        runtime._exit_evidence.drain_pending()
        audit = repo.exit_audit(str(position["position_id"]))
        assert audit["latched_at"]
        assert audit["submitted_at"]
        assert audit["execution_latency_ms"] is not None
        assert audit["submit_book_hash"] == "a1-telemetry-book"
        assert audit["exit_purpose"] == "STOP_066"
    finally:
        temp.cleanup()
