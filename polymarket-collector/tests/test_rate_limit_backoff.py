from __future__ import annotations

import asyncio

import pytest

from live.api_observability import (
    RATE_LIMIT_BACKOFF_CAP_SECONDS,
    PriorityRequestObserver,
    RequestPriority,
    rate_limit_backoff_seconds,
)


class _BareResponse:
    status_code = 429
    headers: dict[str, str] = {}


class _RateLimitedNoRetryAfter(RuntimeError):
    response = _BareResponse()


def test_backoff_schedule_doubles_and_caps() -> None:
    assert [rate_limit_backoff_seconds(n) for n in range(1, 9)] == [
        1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0,
    ]
    assert rate_limit_backoff_seconds(10_000) == RATE_LIMIT_BACKOFF_CAP_SECONDS


def test_consecutive_429s_without_retry_after_grow_the_block() -> None:
    async def scenario() -> None:
        observer = PriorityRequestObserver()

        async def fail() -> None:
            raise _RateLimitedNoRetryAfter("429 Too Many Requests")

        blocks = []
        for _ in range(3):
            # Skip the wait itself; only the scheduled block matters here.
            observer._blocked_until.clear()
            with pytest.raises(_RateLimitedNoRetryAfter):
                await observer.call(
                    "positions.list", RequestPriority.RECONCILIATION, fail
                )
            blocks.append(observer.health()["blocked_families_seconds"]["positions"])
        assert 0.9 < blocks[0] <= 1.0
        assert 1.9 < blocks[1] <= 2.0
        assert 3.9 < blocks[2] <= 4.0
        assert observer.health()["rate_limit_streaks"] == {"positions": 3}

    asyncio.run(scenario())


def test_success_resets_the_streak() -> None:
    async def scenario() -> None:
        observer = PriorityRequestObserver()

        async def fail() -> None:
            raise _RateLimitedNoRetryAfter("429 Too Many Requests")

        for _ in range(2):
            observer._blocked_until.clear()
            with pytest.raises(_RateLimitedNoRetryAfter):
                await observer.call(
                    "positions.list", RequestPriority.RECONCILIATION, fail
                )
        observer._blocked_until.clear()
        assert await observer.call(
            "positions.list",
            RequestPriority.RECONCILIATION,
            lambda: asyncio.sleep(0, result="ok"),
        ) == "ok"
        assert observer.health()["rate_limit_streaks"] == {}

        with pytest.raises(_RateLimitedNoRetryAfter):
            await observer.call(
                "positions.list", RequestPriority.RECONCILIATION, fail
            )
        assert observer.health()["blocked_families_seconds"]["positions"] <= 1.0

    asyncio.run(scenario())


def test_streak_does_not_slow_trading_families() -> None:
    async def scenario() -> None:
        observer = PriorityRequestObserver()

        async def fail() -> None:
            raise _RateLimitedNoRetryAfter("429")

        for _ in range(5):
            observer._blocked_until.clear()
            with pytest.raises(_RateLimitedNoRetryAfter):
                await observer.call(
                    "positions.list", RequestPriority.RECONCILIATION, fail
                )
        assert await asyncio.wait_for(
            observer.call(
                "orders.post",
                RequestPriority.EXIT,
                lambda: asyncio.sleep(0, result="ok"),
            ),
            timeout=0.1,
        ) == "ok"

    asyncio.run(scenario())
