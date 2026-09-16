"""Bounded outbound-request telemetry and priority-aware backoff.

No payloads, credentials, URLs with query strings, or response bodies are
recorded.  Retry-After is scoped to an endpoint family so a throttled
reconciliation positions read cannot queue an emergency order behind it.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import IntEnum
from typing import Any, Awaitable, Callable, TypeVar


T = TypeVar("T")


class RequestPriority(IntEnum):
    EXIT = 0
    ENTRY = 1
    ORDER_STATE = 2
    MARKET_DATA = 3
    RECONCILIATION = 5


def _response_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    raw = getattr(response, "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            instant = parsedate_to_datetime(str(value))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return max(
                0.0, (instant - datetime.now(timezone.utc)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True, slots=True)
class RequestSample:
    endpoint: str
    priority: int
    duration_ms: float
    queue_wait_ms: float
    status_code: int | None
    exception_class: str
    retry_after_seconds: float | None
    success: bool


class PriorityRequestObserver:
    """Traffic isolation, endpoint-scoped Retry-After, and bounded metrics."""

    def __init__(self, *, sample_capacity: int = 2048) -> None:
        self._samples: deque[RequestSample] = deque(
            maxlen=max(64, sample_capacity)
        )
        self._blocked_until: dict[str, float] = {}
        self._trading_slots = asyncio.Semaphore(4)
        self._read_slots = asyncio.Semaphore(2)
        self._reconciliation_slots = asyncio.Semaphore(1)
        self._calls: dict[str, int] = defaultdict(int)
        self._errors: dict[str, int] = defaultdict(int)
        self._rate_limits: dict[str, int] = defaultdict(int)

    @staticmethod
    def _family(endpoint: str) -> str:
        return str(endpoint).split(".", 1)[0]

    def _semaphore(self, priority: RequestPriority) -> asyncio.Semaphore:
        if priority <= RequestPriority.ENTRY:
            return self._trading_slots
        if priority >= RequestPriority.RECONCILIATION:
            return self._reconciliation_slots
        return self._read_slots

    async def call(
        self,
        endpoint: str,
        priority: RequestPriority,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        queued = time.perf_counter()
        family = self._family(endpoint)
        # A positions/trades 429 delays reconciliation reads only. Trading has
        # independent slots and endpoint families, so no priority inversion.
        while priority >= RequestPriority.RECONCILIATION:
            remaining = self._blocked_until.get(family, 0.0) - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 1.0))
        semaphore = self._semaphore(priority)
        async with semaphore:
            started = time.perf_counter()
            wait_ms = max(0.0, (started - queued) * 1000)
            self._calls[endpoint] += 1
            try:
                result = await operation()
            except Exception as exc:
                status = _response_status(exc)
                retry_after = retry_after_seconds(exc)
                if status == 429 or retry_after is not None:
                    self._rate_limits[endpoint] += 1
                    delay = retry_after if retry_after is not None else 1.0
                    self._blocked_until[family] = max(
                        self._blocked_until.get(family, 0.0),
                        time.monotonic() + min(300.0, max(0.05, delay)),
                    )
                self._errors[endpoint] += 1
                self._samples.append(RequestSample(
                    endpoint=endpoint,
                    priority=int(priority),
                    duration_ms=max(0.0, (time.perf_counter() - started) * 1000),
                    queue_wait_ms=wait_ms,
                    status_code=status,
                    exception_class=type(exc).__name__,
                    retry_after_seconds=retry_after,
                    success=False,
                ))
                raise
            self._samples.append(RequestSample(
                endpoint=endpoint,
                priority=int(priority),
                duration_ms=max(0.0, (time.perf_counter() - started) * 1000),
                queue_wait_ms=wait_ms,
                status_code=200,
                exception_class="",
                retry_after_seconds=None,
                success=True,
            ))
            return result

    def health(self) -> dict[str, Any]:
        by_endpoint: dict[str, list[RequestSample]] = defaultdict(list)
        for sample in self._samples:
            by_endpoint[sample.endpoint].append(sample)

        def percentile(values: list[float], quantile: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = min(len(ordered) - 1, int(len(ordered) * quantile))
            return round(ordered[index], 3)

        endpoints: dict[str, dict[str, Any]] = {}
        for endpoint, samples in by_endpoint.items():
            durations = [item.duration_ms for item in samples]
            waits = [item.queue_wait_ms for item in samples]
            endpoints[endpoint] = {
                "calls": self._calls[endpoint],
                "errors": self._errors[endpoint],
                "rate_limits": self._rate_limits[endpoint],
                "latency_p50_ms": percentile(durations, 0.50),
                "latency_p95_ms": percentile(durations, 0.95),
                "latency_p99_ms": percentile(durations, 0.99),
                "latency_max_ms": round(max(durations), 3),
                "queue_wait_p95_ms": percentile(waits, 0.95),
                "last_exception_class": next(
                    (
                        item.exception_class
                        for item in reversed(samples)
                        if item.exception_class
                    ),
                    "",
                ),
                "last_status_code": samples[-1].status_code,
                "last_retry_after_seconds": samples[-1].retry_after_seconds,
            }
        now = time.monotonic()
        return {
            "sample_count": len(self._samples),
            "sample_capacity": self._samples.maxlen,
            "blocked_families_seconds": {
                family: round(max(0.0, deadline - now), 3)
                for family, deadline in self._blocked_until.items()
                if deadline > now
            },
            "endpoints": endpoints,
        }
