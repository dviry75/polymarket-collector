from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any, Callable


GAP_BACKOFF_SECONDS = (3.0, 3.0, 5.0, 10.0, 15.0, 30.0, 60.0)
GAP_IDENTITY_KEYS = (
    "type",
    "token_id",
    "position_id",
    "order_id",
    "polymarket_order_id",
    "intent_id",
    "condition_id",
)


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_gap_fingerprints(
    gaps: list[dict[str, Any]],
) -> tuple[str, str]:
    """Return stable identity and full-evidence fingerprints for a gap set."""
    identities = []
    evidence = []
    for gap in gaps:
        normalized = {str(key): gap[key] for key in sorted(gap)}
        identities.append({
            key: normalized.get(key)
            for key in GAP_IDENTITY_KEYS
            if normalized.get(key) not in (None, "")
        })
        evidence.append(normalized)
    identities.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    evidence.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return _fingerprint(identities), _fingerprint(evidence)


class GapBackoffTracker:
    """Fingerprint-aware, bounded retry schedule for unchanged gap evidence."""

    def __init__(
        self,
        schedule: tuple[float, ...] = GAP_BACKOFF_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not schedule or any(value <= 0 for value in schedule):
            raise ValueError("gap backoff schedule must contain positive values")
        self.schedule = tuple(float(value) for value in schedule)
        self.clock = clock
        self.identity_fingerprint = ""
        self.evidence_fingerprint = ""
        self.repeat_count = 0
        self.current_backoff_seconds = 0.0
        self.last_changed_at = ""

    def observe(self, gaps: list[dict[str, Any]]) -> float:
        if not gaps:
            self.reset()
            return 0.0
        identity, evidence = stable_gap_fingerprints(gaps)
        changed = (
            identity != self.identity_fingerprint
            or evidence != self.evidence_fingerprint
        )
        if changed:
            self.identity_fingerprint = identity
            self.evidence_fingerprint = evidence
            self.repeat_count = 1
            self.last_changed_at = datetime.now(timezone.utc).isoformat()
        else:
            self.repeat_count += 1
        index = min(self.repeat_count - 1, len(self.schedule) - 1)
        self.current_backoff_seconds = self.schedule[index]
        return self.current_backoff_seconds

    def reset(self) -> None:
        self.identity_fingerprint = ""
        self.evidence_fingerprint = ""
        self.repeat_count = 0
        self.current_backoff_seconds = 0.0
        self.last_changed_at = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "fingerprint": self.identity_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "repeat_count": self.repeat_count,
            "current_backoff_seconds": self.current_backoff_seconds,
            "last_changed_at": self.last_changed_at,
        }


class ReconciliationCadencePolicy:
    """Allow fast cadence only for a bounded unchanged-work window."""

    def __init__(
        self,
        active_interval_seconds: float,
        normal_interval_seconds: float,
        *,
        fast_window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.active_interval_seconds = max(1.0, float(active_interval_seconds))
        self.normal_interval_seconds = max(
            self.active_interval_seconds, float(normal_interval_seconds)
        )
        self.fast_window_seconds = max(
            self.active_interval_seconds, float(fast_window_seconds)
        )
        self.clock = clock
        self._signature = ""
        self._unchanged_since = 0.0

    def interval(self, active_work: list[dict[str, Any]]) -> float:
        if not active_work:
            self._signature = ""
            self._unchanged_since = 0.0
            return self.normal_interval_seconds
        signature = _fingerprint(active_work)
        now = self.clock()
        if signature != self._signature:
            self._signature = signature
            self._unchanged_since = now
            return self.active_interval_seconds
        if now - self._unchanged_since <= self.fast_window_seconds:
            return self.active_interval_seconds
        return self.normal_interval_seconds

    def snapshot(self) -> dict[str, Any]:
        elapsed = (
            max(0.0, self.clock() - self._unchanged_since)
            if self._signature else 0.0
        )
        return {
            "active_signature": self._signature,
            "unchanged_seconds": round(elapsed, 3),
            "fast_window_seconds": self.fast_window_seconds,
            "active_interval_seconds": self.active_interval_seconds,
            "normal_interval_seconds": self.normal_interval_seconds,
        }


@dataclass
class _RequestBatch:
    actors: set[str] = field(default_factory=set)
    generations: set[int] = field(default_factory=set)
    waiters: list[asyncio.Future[dict[str, Any]]] = field(default_factory=list)
    evidence_changed: bool = False
    force: bool = False

    @property
    def latest_generation(self) -> int | None:
        return max(self.generations) if self.generations else None

    @property
    def actor(self) -> str:
        joined = ",".join(sorted(self.actors))
        return f"coordinator:{joined}"[:200]


class ReconciliationCoordinator:
    """Own reconciliation tasks independently from WebSocket lifecycles."""

    def __init__(
        self,
        worker: Any,
        *,
        current_generation: Callable[[], int] | None = None,
    ):
        self.worker = worker
        self.current_generation = current_generation
        self._lock = asyncio.Lock()
        self._pending: _RequestBatch | None = None
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False
        self.requests_total = 0
        self.runs_total = 0
        self.coalesced_total = 0
        self.stale_generation_results = 0
        self.max_concurrency = 0
        self._running_count = 0
        self.last_result: dict[str, Any] | None = None

    async def start(self) -> None:
        async with self._lock:
            self._stopping = False

    async def request(
        self,
        actor: str,
        *,
        generation: int | None = None,
        evidence_changed: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._lock:
            if self._stopping:
                raise asyncio.CancelledError("reconciliation coordinator stopping")
            self.requests_total += 1
            if self._pending is None:
                self._pending = _RequestBatch()
            else:
                self.coalesced_total += 1
            self._pending.actors.add(str(actor or "system"))
            if generation is not None:
                self._pending.generations.add(int(generation))
            self._pending.waiters.append(waiter)
            self._pending.evidence_changed |= bool(evidence_changed)
            self._pending.force |= bool(force)
            if self._runner is None or self._runner.done():
                self._runner = asyncio.create_task(
                    self._drain(), name="reconciliation-coordinator"
                )
        # The coordinator owns the task. Cancellation of a reconnect caller
        # must not propagate into durable reconciliation work.
        return await asyncio.shield(waiter)

    async def _take_pending(self) -> _RequestBatch | None:
        async with self._lock:
            batch = self._pending
            self._pending = None
            if batch is None and self._runner is asyncio.current_task():
                self._runner = None
            return batch

    async def _drain(self) -> None:
        try:
            while not self._stopping:
                batch = await self._take_pending()
                if batch is None:
                    return
                if batch.evidence_changed and hasattr(
                    self.worker, "reset_retry_backoff"
                ):
                    self.worker.reset_retry_backoff(batch.actor)
                generation = batch.latest_generation

                def publish_guard() -> bool:
                    return bool(
                        generation is None
                        or self.current_generation is None
                        or int(self.current_generation()) == generation
                    )

                while not self._stopping:
                    self._running_count += 1
                    self.max_concurrency = max(
                        self.max_concurrency, self._running_count
                    )
                    try:
                        result = await self.worker.run_once(
                            actor=batch.actor,
                            ready_publish_guard=publish_guard,
                            force=batch.force,
                        )
                    finally:
                        self._running_count -= 1
                    if result.get("status") != "backoff":
                        break
                    await asyncio.sleep(
                        max(0.05, float(result.get("retry_after_seconds") or 0.05))
                    )
                if self._stopping:
                    raise asyncio.CancelledError
                self.runs_total += 1
                if not publish_guard() and result.get("status") == "ok":
                    self.stale_generation_results += 1
                    result = dict(result)
                    result["published_readiness"] = False
                    result["stale_generation"] = generation
                    current = (
                        int(self.current_generation())
                        if self.current_generation is not None else None
                    )
                    async with self._lock:
                        if self._pending is None:
                            self._pending = _RequestBatch(
                                actors={"stale_generation_followup"}
                            )
                        if current is not None:
                            self._pending.generations.add(current)
                self.last_result = result
                for waiter in batch.waiters:
                    if not waiter.done():
                        waiter.set_result(dict(result))
        except asyncio.CancelledError:
            batch = locals().get("batch")
            if isinstance(batch, _RequestBatch):
                for waiter in batch.waiters:
                    if not waiter.done():
                        waiter.cancel()
            raise
        except Exception as exc:
            batch = locals().get("batch")
            if isinstance(batch, _RequestBatch):
                for waiter in batch.waiters:
                    if not waiter.done():
                        waiter.set_exception(exc)
        finally:
            async with self._lock:
                owns_runner = self._runner is asyncio.current_task()
                pending = (
                    self._pending if owns_runner and self._stopping else None
                )
                if pending is not None:
                    self._pending = None
                if owns_runner:
                    self._runner = None
            if pending is not None:
                for waiter in pending.waiters:
                    if not waiter.done():
                        waiter.cancel()

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            runner = self._runner
        if runner is not None and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    def health(self) -> dict[str, Any]:
        pending = self._pending
        return {
            "coordinator_running": bool(
                self._runner is not None and not self._runner.done()
            ),
            "run_in_progress": self._running_count > 0,
            "pending": pending is not None,
            "pending_trigger_count": (
                len(pending.waiters) if pending is not None else 0
            ),
            "requests_total": self.requests_total,
            "runs_total": self.runs_total,
            "coalesced_total": self.coalesced_total,
            "max_concurrency": self.max_concurrency,
            "stale_generation_results": self.stale_generation_results,
            "last_result": self.last_result,
        }
