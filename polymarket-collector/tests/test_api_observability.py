from __future__ import annotations

import asyncio
import time

import pytest

from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.api_observability import PriorityRequestObserver, RequestPriority
from live.config import LiveConfig


class _Response:
    status_code = 429
    headers = {"Retry-After": "0.15"}


class _RateLimited(RuntimeError):
    response = _Response()


def test_retry_after_is_recorded_and_scoped_away_from_orders() -> None:
    async def scenario() -> None:
        observer = PriorityRequestObserver()

        async def fail() -> None:
            raise _RateLimited("too many requests")

        with pytest.raises(_RateLimited):
            await observer.call(
                "positions.list", RequestPriority.RECONCILIATION, fail
            )
        started = time.perf_counter()
        assert await observer.call(
            "orders.post",
            RequestPriority.EXIT,
            lambda: asyncio.sleep(0, result="ok"),
        ) == "ok"
        assert time.perf_counter() - started < 0.05
        health = observer.health()
        assert health["endpoints"]["positions.list"]["rate_limits"] == 1
        assert health["endpoints"]["positions.list"]["last_status_code"] == 429
        assert health["endpoints"]["orders.post"]["errors"] == 0
        assert health["blocked_families_seconds"]["positions"] > 0

    asyncio.run(scenario())


def test_reconciliation_calls_honor_retry_after() -> None:
    async def scenario() -> None:
        observer = PriorityRequestObserver()
        calls = 0

        async def fail_once() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _RateLimited("429")
            return "ok"

        with pytest.raises(_RateLimited):
            await observer.call(
                "positions.list", RequestPriority.RECONCILIATION, fail_once
            )
        started = time.perf_counter()
        assert await observer.call(
            "positions.list", RequestPriority.RECONCILIATION, fail_once
        ) == "ok"
        assert time.perf_counter() - started >= 0.12

    asyncio.run(scenario())


def test_real_adapter_collateral_snapshot_uses_one_remote_call() -> None:
    class Client:
        calls = 0

        async def get_balance_allowance(self, **_kwargs):
            self.calls += 1
            return {
                "balance": 10_000_000,
                "allowances": {"exchange": 9_000_000},
            }

    async def scenario() -> None:
        client = Client()
        adapter = RealPolymarketTradingAdapter(
            LiveConfig(), secure_client=client
        )
        snapshot = await adapter.get_collateral_snapshot()
        assert client.calls == 1
        assert snapshot["balance"]["balance_usd"] == "10"
        assert snapshot["allowances"]["allowance_usd"] == "9"
        health = adapter.request_health()
        assert health["endpoints"]["collateral.balance_allowance"]["calls"] == 1

    asyncio.run(scenario())
