"""Decision ingress boundary; no strategy, provider loop or telemetry DB writer."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import math
from typing import Callable

from .domain.models import MarketObservation, require_aware


class ObservationRejected(RuntimeError):
    pass


class DecisionStream:
    """Single event-loop owner, one ordered provider connection per stream.

    No sampling/coalescing. Loss, stale data or overflow invalidates the stream;
    consumers must await a new generation and fresh observations, not replay.
    """

    def __init__(
        self, *, capacity: int = 1024, max_age_seconds: float = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if capacity < 1 or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("decision capacity and maximum age must be positive")
        self._queue: asyncio.Queue[MarketObservation | None] = asyncio.Queue(capacity)
        self._clock = clock
        self._max_age_seconds = max_age_seconds
        self._generation = 0
        self._last_sequence = 0
        self.reason = "AWAITING_GENERATION"

    def begin_generation(self, generation: int) -> None:
        if generation <= self._generation:
            raise ValueError("new generation must advance")
        capacity = self._queue.maxsize
        self._retire_queue()
        self._queue = asyncio.Queue(capacity)
        self._generation = generation
        self._last_sequence = 0
        self.reason = ""

    def _discard_pending(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def _retire_queue(self) -> None:
        self._discard_pending()
        # Wake the single consumer on this retired queue. A new generation
        # gets a different queue so its first observation cannot be swallowed
        # by an old waiter racing with recovery.
        self._queue.put_nowait(None)

    def invalidate(self, reason: str) -> None:
        """Provider must call this immediately on disconnect or detected loss."""
        if not reason:
            raise ValueError("invalidation requires a reason")
        self.reason = reason
        self._retire_queue()

    def _reject(self, reason: str) -> None:
        self.invalidate(reason)
        raise ObservationRejected(reason)

    def _check_fresh(self, observation: MarketObservation) -> None:
        now = self._clock()
        require_aware(now)
        ages = [(now - stamp).total_seconds() for stamp in (observation.observed_at, observation.received_at)]
        if not observation.book_ready or any(age < 0 or age > self._max_age_seconds for age in ages):
            self._reject("OBSERVATION_NOT_FRESH_AND_READY")

    def publish(self, observation: MarketObservation) -> None:
        if self.reason:
            raise ObservationRejected(self.reason)
        if observation.generation != self._generation:
            self._reject("GENERATION_MISMATCH")
        if observation.sequence != self._last_sequence + 1:
            self._reject("OBSERVATION_SEQUENCE_GAP_OR_REORDER")
        self._check_fresh(observation)
        if self._queue.full():
            self._reject("DECISION_QUEUE_OVERFLOW")
        self._queue.put_nowait(observation)
        self._last_sequence = observation.sequence

    async def next_observation(self) -> MarketObservation:
        if self.reason:
            raise ObservationRejected(self.reason)
        queue = self._queue
        observation = await queue.get()
        if observation is None or queue is not self._queue:
            raise ObservationRejected(self.reason or "GENERATION_CHANGED")
        if self.reason or observation.generation != self._generation:
            raise ObservationRejected(self.reason or "GENERATION_MISMATCH")
        # Waiting for a consumer must not turn a formerly fresh quote into a
        # stale decision. Recheck at delivery, not only at ingress.
        self._check_fresh(observation)
        return observation


class TelemetryBuffer:
    """Optional lossy last-value buffer; it never feeds DecisionStream."""

    def __init__(self, capacity: int = 64) -> None:
        if capacity < 1:
            raise ValueError("telemetry capacity must be positive")
        self.capacity = capacity
        self._latest: OrderedDict[tuple[str, str], MarketObservation] = OrderedDict()

    def offer(self, observation: MarketObservation) -> None:
        key = (observation.instance_id, observation.token_id)
        self._latest[key] = observation
        self._latest.move_to_end(key)
        while len(self._latest) > self.capacity:
            self._latest.popitem(last=False)

    def drain(self) -> tuple[MarketObservation, ...]:
        observations = tuple(self._latest.values())
        self._latest.clear()
        return observations
