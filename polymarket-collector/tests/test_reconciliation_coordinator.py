import asyncio

import pytest

from live.market_websocket import MarketWebSocketManager
from live.reconciliation_coordinator import (
    GapBackoffTracker,
    ReconciliationCadencePolicy,
    ReconciliationCoordinator,
)


class ControlledWorker:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.running = 0
        self.max_running = 0
        self.cancelled = 0
        self.reset_calls = 0
        self.guards = []

    def reset_retry_backoff(self, _actor):
        self.reset_calls += 1

    async def run_once(self, *, actor, ready_publish_guard, force=False):
        del actor, force
        self.calls += 1
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.guards.append(ready_publish_guard)
        self.started.set()
        try:
            await self.release.wait()
            return {
                "status": "ok",
                "gaps": [],
                "published_readiness": ready_publish_guard(),
            }
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


def test_reconnect_storm_is_single_flight_with_one_followup():
    async def scenario():
        worker = ControlledWorker()
        coordinator = ReconciliationCoordinator(worker)
        await coordinator.start()
        first = asyncio.create_task(coordinator.request("first"))
        await worker.started.wait()
        followers = [
            asyncio.create_task(coordinator.request(f"reconnect-{index}"))
            for index in range(20)
        ]
        await asyncio.sleep(0)
        worker.release.set()
        await asyncio.gather(first, *followers)
        assert worker.calls == 2
        assert worker.max_running == 1
        assert coordinator.health()["coalesced_total"] >= 19
        await coordinator.stop()

    asyncio.run(scenario())


def test_cancelled_ws_waiter_does_not_cancel_durable_run():
    async def scenario():
        worker = ControlledWorker()
        coordinator = ReconciliationCoordinator(worker)
        await coordinator.start()
        waiter = asyncio.create_task(coordinator.request("market_ws_reconnect"))
        await worker.started.wait()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert worker.cancelled == 0
        worker.release.set()
        for _ in range(20):
            if not coordinator.health()["coordinator_running"]:
                break
            await asyncio.sleep(0)
        assert worker.calls == 1
        assert worker.cancelled == 0
        await coordinator.stop()

    asyncio.run(scenario())



def test_market_ws_disconnect_does_not_wait_for_or_cancel_reconciliation():
    async def scenario():
        worker = ControlledWorker()
        coordinator = ReconciliationCoordinator(worker)
        await coordinator.start()
        manager = MarketWebSocketManager(
            object(),
            on_reconnect=lambda: coordinator.request("market_ws_reconnect"),
        )

        async def disconnect(_ws):
            await worker.started.wait()
            raise ConnectionError("socket closed")

        async def idle(_ws):
            await asyncio.Event().wait()

        manager._run_ingress_pipeline = disconnect
        manager._heartbeat = idle
        manager._subscription_loop = idle
        with pytest.raises(ConnectionError, match="socket closed"):
            await asyncio.wait_for(
                manager._run_connected_pipeline(object()), timeout=1
            )
        assert worker.running == 1
        assert worker.cancelled == 0
        worker.release.set()
        for _ in range(50):
            if worker.running == 0:
                break
            await asyncio.sleep(0)
        assert worker.running == 0
        assert worker.cancelled == 0
        await coordinator.stop()

    asyncio.run(scenario())

def test_shutdown_cancels_owned_run_cleanly():
    async def scenario():
        worker = ControlledWorker()
        coordinator = ReconciliationCoordinator(worker)
        await coordinator.start()
        waiter = asyncio.create_task(coordinator.request("periodic"))
        await worker.started.wait()
        await coordinator.stop()
        await asyncio.gather(waiter, return_exceptions=True)
        assert worker.cancelled == 1
        assert worker.running == 0
        assert not coordinator.health()["coordinator_running"]

    asyncio.run(scenario())


def test_stale_generation_cannot_publish_ready_and_gets_followup():
    async def scenario():
        generation = 1
        worker = ControlledWorker()
        coordinator = ReconciliationCoordinator(
            worker, current_generation=lambda: generation
        )
        await coordinator.start()
        first = asyncio.create_task(
            coordinator.request("market_ws_reconnect", generation=1)
        )
        await worker.started.wait()
        generation = 2
        worker.release.set()
        result = await first
        assert result["published_readiness"] is False
        for _ in range(50):
            if worker.calls >= 2:
                break
            await asyncio.sleep(0)
        assert worker.calls == 2
        assert coordinator.health()["last_result"]["published_readiness"] is True
        assert coordinator.health()["stale_generation_results"] == 1
        await coordinator.stop()

    asyncio.run(scenario())


def test_gap_backoff_is_bounded_and_evidence_aware():
    tracker = GapBackoffTracker()
    gap = [{
        "type": "local_position_missing_remote",
        "position_id": "p1",
        "token_id": "t1",
        "authoritative_balance": "0",
    }]
    observed = [tracker.observe(gap) for _ in range(100)]
    assert observed[:7] == [3, 3, 5, 10, 15, 30, 60]
    assert observed[-1] == 60
    assert tracker.repeat_count == 100

    changed = [dict(gap[0], authoritative_balance="1")]
    assert tracker.observe(changed) == 3
    assert tracker.repeat_count == 1

    unrelated = [dict(changed[0], position_id="p2")]
    assert tracker.observe(unrelated) == 3
    assert tracker.repeat_count == 1

    assert tracker.observe([]) == 0
    assert tracker.repeat_count == 0


def test_stuck_active_work_returns_to_normal_cadence():
    now = 0.0
    policy = ReconciliationCadencePolicy(
        3, 15, fast_window_seconds=60, clock=lambda: now
    )
    stuck = [{"kind": "position", "id": "p1", "state": "OPEN"}]
    assert policy.interval(stuck) == 3
    now = 59
    assert policy.interval(stuck) == 3
    now = 61
    assert policy.interval(stuck) == 15
    now = 62
    changed = [dict(stuck[0], state="EXITING")]
    assert policy.interval(changed) == 3
    assert policy.interval([]) == 15
