"""Stop-loss exit forensic evidence collection.

The trader records *why* a latched STOP / EMERGENCY exit priced the way it did:
the order book at cross / latch / submit, the detection and execution latencies,
and the realized vs book-implied VWAP.

Hot-path safety is the whole point of this module:

* Every ``note_*`` method is a synchronous dict update + ``deque.append`` +
  ``Event.set()``. No serialization, no book walk, no DB write ever runs on the
  SELL submission path.
* All materialization (book JSON, attribution math, ``expected_vwap``, every
  ``INSERT``) happens on :meth:`ExitEvidenceCollector.run`, a dedicated asyncio
  task that swallows every exception into ``last_error``.
* The queue is bounded; on overflow the oldest job is dropped and the affected
  episode is marked ``DEGRADED_EVIDENCE_DROPPED``.

An *episode* is one latched exit obligation for one position. It is created when
a frame first shows the position stop-eligible, but nothing is written until the
position actually latches — a position that dips sub-stop and recovers leaves no
row.
"""

from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
import json
from typing import Any, Callable

from .book_walk import book_walk_vwap
from .repository import now_iso
from .strategy_repository import StrategyRepository, stable_id

_MAX_QUEUE = 512
_MAX_LADDER_LEVELS = 250
_DEFAULT_DEEP_CAPTURE_MAX_VWAP = Decimal("0.55")
_DEFAULT_ACCEPTABLE_MIN_VWAP = Decimal("0.60")


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _text(value: Any) -> str | None:
    d = _dec(value)
    return format(d, "f") if d is not None else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms(seconds: Any) -> int | None:
    if seconds is None:
        return None
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return None


class _Episode:
    __slots__ = (
        "exit_audit_id", "position_id", "episode_seq", "event_id",
        "condition_id", "token_id", "deal_id", "entry_intent_id",
        "exit_intent_id", "fields", "books", "book_refs",
        "latched", "finalized", "below_stop",
        "last_bid", "cross_count", "uncross_count", "min_bid_seen",
        "degraded",
    )

    def __init__(self, position: dict[str, Any], *, episode_seq: int = 1) -> None:
        self.position_id = str(position.get("position_id") or "")
        self.episode_seq = int(episode_seq)
        self.exit_audit_id = stable_id(
            "exit-audit",
            self.position_id
            if episode_seq == 1
            else f"{self.position_id}:{episode_seq}",
        )
        self.event_id = str(position.get("event_id") or "")
        self.condition_id = str(position.get("condition_id") or "") or None
        self.token_id = str(position.get("token_id") or "") or None
        self.deal_id = stable_id("deal", self.event_id) if self.event_id else None
        self.entry_intent_id: str | None = None
        self.exit_intent_id: str | None = None
        self.fields: dict[str, Any] = {}
        self.books: dict[str, dict[str, Any]] = {}
        self.book_refs: dict[str, Any] = {}
        self.latched = False
        self.finalized = False
        self.below_stop = True
        self.last_bid: Decimal | None = None
        self.cross_count = 1
        self.uncross_count = 0
        self.min_bid_seen: Decimal | None = None
        self.degraded: str | None = None

    def identity_fields(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "entry_intent_id": self.entry_intent_id,
        }

    def observe_bid(self, bid: Decimal | None, stop_price: Decimal) -> None:
        if bid is None:
            return
        if self.min_bid_seen is None or bid < self.min_bid_seen:
            self.min_bid_seen = bid
        below = bid <= stop_price
        if below and not self.below_stop:
            self.cross_count += 1
        elif not below and self.below_stop:
            self.uncross_count += 1
        self.below_stop = below
        self.last_bid = bid


class ExitEvidenceCollector:
    def __init__(
        self,
        repo: StrategyRepository,
        *,
        deep_capture_max_vwap: Decimal | str | float | None = None,
        acceptable_min_vwap: Decimal | str | float | None = None,
        book_provider: Callable[[str], dict[str, Any] | None] | None = None,
        logger: Any = None,
    ) -> None:
        self.repo = repo
        self.deep_capture_max_vwap = (
            _dec(deep_capture_max_vwap) or _DEFAULT_DEEP_CAPTURE_MAX_VWAP
        )
        self.acceptable_min_vwap = (
            _dec(acceptable_min_vwap) or _DEFAULT_ACCEPTABLE_MIN_VWAP
        )
        self._book_provider = book_provider
        self._logger = logger
        self._queue: deque[dict[str, Any]] = deque()
        self._episodes: dict[str, _Episode] = {}
        self.wakeup = asyncio.Event()
        self.enqueued = 0
        self.applied = 0
        self.dropped = 0
        self.audits_written = 0
        self.snapshots_written = 0
        self.last_error = ""

    def set_book_provider(
        self, provider: Callable[[str], dict[str, Any] | None] | None
    ) -> None:
        self._book_provider = provider

    def stats(self) -> dict[str, Any]:
        return {
            "queue_depth": len(self._queue),
            "active_episodes": len(self._episodes),
            "enqueued": self.enqueued,
            "applied": self.applied,
            "dropped": self.dropped,
            "audits_written": self.audits_written,
            "snapshots_written": self.snapshots_written,
            "last_error": self.last_error,
        }

    # ---------------------------------------------------------------- #
    # Hot-path notes — synchronous, allocation-light, never touch the DB
    # ---------------------------------------------------------------- #
    def _episode(self, position: dict[str, Any]) -> _Episode:
        pid = str(position.get("position_id") or "")
        episode = self._episodes.get(pid)
        if episode is None:
            episode = _Episode(position)
            self._episodes[pid] = episode
        return episode

    def _enqueue(self, job: dict[str, Any]) -> None:
        if len(self._queue) >= _MAX_QUEUE:
            self._queue.popleft()
            self.dropped += 1
            pid = job.get("position_id")
            episode = self._episodes.get(str(pid)) if pid else None
            if episode is not None:
                episode.degraded = "DEGRADED_EVIDENCE_DROPPED"
        self._queue.append(job)
        self.enqueued += 1
        self.wakeup.set()

    def note_cross(
        self,
        position: dict[str, Any],
        *,
        bid: Decimal | None,
        bid_size: Any,
        update: dict[str, Any],
        received_at: str | None,
        stop_price: Decimal,
        entry_intent_id: str | None = None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        fresh = pid not in self._episodes
        episode = self._episode(position)
        if entry_intent_id and not episode.entry_intent_id:
            episode.entry_intent_id = str(entry_intent_id)
        if not fresh:
            episode.observe_bid(bid, stop_price)
            return
        episode.min_bid_seen = bid
        episode.last_bid = bid
        episode.fields.update(
            {
                "cross_detected_at": received_at
                or str(update.get("received_at") or now_iso()),
                "cross_exchange_timestamp_ms": _int(
                    update.get("exchange_timestamp_ms")
                ),
                "cross_book_generation": _int(update.get("generation")),
                "cross_book_hash": update.get("message_hash"),
                "cross_book_age_ms": _int(update.get("exchange_age_ms")),
                "cross_receive_latency_ms": _int(
                    update.get("receive_latency_ms")
                ),
                "cross_best_bid_text": _text(bid),
                "cross_best_bid_size_text": _text(bid_size),
                "cross_trigger_id": update.get("_critical_trigger_id"),
                "cross_frame_source": (
                    update.get("event_type") or update.get("source")
                ),
            }
        )
        episode.book_refs["CROSS"] = self._book_ref(update, episode.token_id)
        self._enqueue(
            {
                "kind": "capture_book",
                "position_id": pid,
                "phase": "CROSS",
                "captured_at": episode.fields["cross_detected_at"],
            }
        )

    def observe_bid(
        self, position_id: str, *, bid: Decimal | None, stop_price: Decimal
    ) -> None:
        episode = self._episodes.get(str(position_id))
        if episode is not None:
            episode.observe_bid(bid, stop_price)

    def note_latch(
        self,
        position: dict[str, Any],
        *,
        bid: Decimal | None,
        update: dict[str, Any] | None,
        source: str,
        liquidity_hash: str | None = None,
        latched_at: str | None = None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        episode.latched = True
        episode.fields.update(
            {
                "latched_at": latched_at or now_iso(),
                "latch_best_bid_text": _text(bid),
                "latch_source": source,
                "latch_liquidity_hash": liquidity_hash,
            }
        )
        if update is not None:
            episode.fields.update(
                {
                    "latch_exchange_timestamp_ms": _int(
                        update.get("exchange_timestamp_ms")
                    ),
                    "latch_book_generation": _int(update.get("generation")),
                    "latch_book_hash": update.get("message_hash"),
                    "latch_book_age_ms": _int(update.get("exchange_age_ms")),
                }
            )
            episode.book_refs["LATCH"] = self._book_ref(
                update, episode.token_id
            )
            self._enqueue(
                {
                    "kind": "capture_book",
                    "position_id": pid,
                    "phase": "LATCH",
                    "captured_at": episode.fields["latched_at"],
                }
            )
        self._enqueue({"kind": "sync", "position_id": pid})

    def note_tp_cancel(
        self,
        position: dict[str, Any],
        *,
        intent_id: str | None,
        event: str,
        result: str | None = None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        if intent_id:
            episode.fields["tp_cancel_intent_id"] = str(intent_id)
        if event == "started":
            episode.fields["tp_cancel_started_at"] = now_iso()
        elif event == "confirmed":
            episode.fields["tp_cancel_confirmed_at"] = now_iso()
        if result is not None:
            episode.fields["tp_cancel_result"] = result
        self._enqueue({"kind": "sync", "position_id": pid})

    def note_prior_exit_cancel(
        self,
        position: dict[str, Any],
        *,
        intent_id: str | None,
        result: str | None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        if intent_id:
            episode.fields["prior_exit_cancel_intent_id"] = str(intent_id)
        if result is not None:
            episode.fields["prior_exit_cancel_result"] = result
        self._enqueue({"kind": "sync", "position_id": pid})

    def note_submit(
        self,
        position: dict[str, Any],
        *,
        exit_intent_id: str | None,
        purpose: str | None,
        min_price: Any,
        requested_shares: Any,
        frame_hash: str | None,
        update: dict[str, Any],
        submitted_at: str | None,
        stop_to_submit_seconds: float | None,
        frame_to_submit_seconds: float | None,
        attempt_count: int | None = None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        # Reaching a real stop/emergency SELL submit is itself a committed exit
        # obligation: keep the episode even if note_latch never ran (operator
        # emergency close, or a latch site we do not hook).
        episode.latched = True
        if exit_intent_id:
            episode.exit_intent_id = str(exit_intent_id)
        episode.fields.update(
            {
                "exit_intent_id": episode.exit_intent_id,
                "exit_purpose": purpose,
                "min_price_floor_text": _text(min_price),
                "requested_shares_text": _text(requested_shares),
                "submit_frame_hash": frame_hash,
                "submit_best_bid_text": _text(update.get("best_bid")),
                "submit_best_bid_size_text": _text(update.get("best_bid_size")),
                "submit_book_generation": _int(update.get("generation")),
                "submit_book_hash": update.get("message_hash"),
                "submit_book_age_ms": _int(update.get("exchange_age_ms")),
                "stop_to_submit_latency_ms": _ms(stop_to_submit_seconds),
                "frame_to_submit_ms": _ms(frame_to_submit_seconds),
            }
        )
        if submitted_at:
            episode.fields["submitted_at"] = submitted_at
        if attempt_count is not None:
            episode.fields["attempt_count"] = int(attempt_count)
        episode.book_refs["SUBMIT"] = self._book_ref(update, episode.token_id)
        self._enqueue(
            {
                "kind": "capture_book",
                "position_id": pid,
                "phase": "SUBMIT",
                "captured_at": submitted_at or now_iso(),
            }
        )
        self._enqueue({"kind": "sync", "position_id": pid})

    def note_submit_result(
        self,
        position: dict[str, Any],
        *,
        clob_status: str | None = None,
        remote_order_id: str | None = None,
        exit_intent_id: str | None = None,
        requested_shares: Any = None,
        submitted_at: str | None = None,
    ) -> None:
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        if exit_intent_id:
            episode.exit_intent_id = str(exit_intent_id)
            episode.fields["exit_intent_id"] = str(exit_intent_id)
        if requested_shares is not None:
            episode.fields["requested_shares_text"] = _text(requested_shares)
        if submitted_at:
            episode.fields.setdefault("submitted_at", submitted_at)
        if clob_status is not None:
            episode.fields["clob_status"] = str(clob_status)
        if remote_order_id is not None:
            episode.fields["remote_order_id"] = str(remote_order_id)
        self._enqueue({"kind": "sync", "position_id": pid})

    def note_waiting_sellable(self, position: dict[str, Any]) -> None:
        """The SELL could not go out because the entry has not settled on-chain
        yet. Distinguishes an expected settlement wait from a real code delay."""
        pid = str(position.get("position_id") or "")
        episode = self._episode(position)
        episode.fields["settlement_wait"] = 1
        self._enqueue({"kind": "sync", "position_id": pid})

    def reconcile_active(self, active_ids: set[str]) -> None:
        for pid, episode in list(self._episodes.items()):
            if pid in active_ids or episode.finalized:
                continue
            if episode.latched:
                self._enqueue({"kind": "finalize", "position_id": pid})
            else:
                self._episodes.pop(pid, None)

    # ---------------------------------------------------------------- #
    # Drain task — everything below runs off the hot path
    # ---------------------------------------------------------------- #
    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(self.wakeup.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            self.wakeup.clear()
            self.drain_pending()

    def drain_pending(self) -> None:
        while self._queue:
            job = self._queue.popleft()
            try:
                self._apply(job)
            except Exception as exc:  # never propagate onto the exit path
                self.last_error = (
                    f"EXIT_EVIDENCE:{type(exc).__name__}:{exc}"
                )[:500]
                if self._logger is not None:
                    self._logger.warning(
                        "exit-evidence job failed kind=%s: %s",
                        job.get("kind"), exc,
                    )
            finally:
                self.applied += 1

    def _apply(self, job: dict[str, Any]) -> None:
        kind = job.get("kind")
        pid = str(job.get("position_id") or "")
        episode = self._episodes.get(pid)
        if episode is None:
            return
        if kind == "capture_book":
            phase = str(job["phase"])
            ref = episode.book_refs.pop(phase, None)
            serialized = self._serialize_book(ref, job.get("captured_at"))
            if serialized is not None:
                episode.books[phase] = serialized
            return
        if kind == "sync":
            if episode.latched:
                self._write_audit(episode)
            return
        if kind == "finalize":
            self._finalize(episode)
            return

    def _write_audit(self, episode: _Episode, **extra: Any) -> None:
        fields = dict(episode.identity_fields())
        fields.update(episode.fields)
        fields.update(extra)
        fields.setdefault("cross_count", episode.cross_count)
        fields.setdefault("uncross_count", episode.uncross_count)
        if episode.min_bid_seen is not None:
            fields.setdefault("min_bid_seen_text", _text(episode.min_bid_seen))
        if episode.degraded and not fields.get("evidence_quality"):
            fields["evidence_quality"] = episode.degraded
        self.repo.record_exit_audit(
            episode.exit_audit_id,
            position_id=episode.position_id,
            event_id=episode.event_id,
            episode_seq=episode.episode_seq,
            **{k: v for k, v in fields.items() if v is not None},
        )
        self.audits_written += 1

    def _finalize(self, episode: _Episode) -> None:
        if episode.finalized:
            return
        if not episode.entry_intent_id and episode.event_id:
            try:
                episode.entry_intent_id = (
                    self.repo.entry_intent_id_for_event(episode.event_id) or None
                )
            except Exception:
                episode.entry_intent_id = None
        summary = {}
        if episode.exit_intent_id:
            try:
                summary = self.repo.fill_summary(episode.exit_intent_id)
            except Exception:
                summary = {}
        shares = _dec(summary.get("shares"))
        vwap = _dec(summary.get("average_price"))
        fees = _dec(summary.get("fees"))
        if (not shares or shares <= 0) and episode.exit_intent_id:
            # No durable fills reconciled yet (or paper mode): fall back to the
            # intent's own aggregate.
            try:
                intent = self.repo.intent(episode.exit_intent_id) or {}
            except Exception:
                intent = {}
            i_shares = _dec(intent.get("filled_shares_text"))
            i_vwap = _dec(intent.get("average_price_text"))
            if i_shares and i_shares > 0 and i_vwap and i_vwap > 0:
                shares, vwap = i_shares, i_vwap
                fees = _dec(intent.get("fee_text"))
        has_fill = bool(shares and shares > 0 and vwap and vwap > 0)

        final: dict[str, Any] = {"cross_count": episode.cross_count,
                                 "uncross_count": episode.uncross_count}
        if episode.min_bid_seen is not None:
            final["min_bid_seen_text"] = _text(episode.min_bid_seen)
        if has_fill:
            final["actual_fill_shares_text"] = _text(shares)
            final["actual_fill_vwap_text"] = _text(vwap)
            if fees is not None:
                final["actual_fill_fees_text"] = _text(fees)
        fill_times = self._fill_times(episode.exit_intent_id)
        final.update(fill_times)

        row = self.repo.exit_audit(
            episode.position_id, episode_seq=episode.episode_seq
        ) or {}
        p_cross = _dec(row.get("cross_best_bid_text"))
        p_latch = _dec(row.get("latch_best_bid_text"))
        p_submit = _dec(row.get("submit_best_bid_text"))
        detection = self._delta_ms(
            row.get("cross_detected_at"), row.get("latched_at")
        )
        execution = self._delta_ms(
            row.get("latched_at"), row.get("submitted_at")
        )
        if detection is not None:
            final["detection_latency_ms"] = detection
        if execution is not None:
            final["execution_latency_ms"] = execution

        expected_vwap = None
        submit_book = episode.books.get("SUBMIT")
        requested = _dec(row.get("requested_shares_text"))
        if submit_book and requested:
            ev, method, fillable = book_walk_vwap(
                submit_book.get("bids_json"),
                requested,
                best_bid=row.get("submit_best_bid_text"),
            )
            expected_vwap = ev
            if ev is not None:
                final["expected_vwap_at_submit_text"] = _text(ev)
                final["expected_vwap_method"] = method
                final["expected_fillable_shares_text"] = _text(fillable)

        if p_cross is not None and p_latch is not None:
            final["loss_detection_text"] = _text(p_cross - p_latch)
        if p_latch is not None and p_submit is not None:
            final["loss_execution_text"] = _text(p_latch - p_submit)
        if expected_vwap is not None and vwap is not None:
            final["loss_fill_text"] = _text(expected_vwap - vwap)

        if not has_fill:
            final["exit_outcome"] = "NO_EXECUTION"
        elif vwap is not None and vwap >= self.acceptable_min_vwap:
            final["exit_outcome"] = "ACCEPTABLE"
        else:
            final["exit_outcome"] = "BAD_EXIT"

        deep = (not has_fill) or (
            vwap is not None and vwap < self.deep_capture_max_vwap
        )
        final["deep_capture"] = 1 if deep else 0
        final["evidence_quality"] = self._evidence_quality(episode, deep)

        self._write_audit(episode, **final)

        if deep:
            for phase, book in episode.books.items():
                self._write_snapshot(episode, phase, book)

        episode.finalized = True
        self._episodes.pop(episode.position_id, None)

    def _write_snapshot(
        self, episode: _Episode, phase: str, book: dict[str, Any]
    ) -> None:
        self.repo.record_exit_book_snapshot(
            stable_id("exit-book", f"{episode.exit_audit_id}:{phase}"),
            exit_audit_id=episode.exit_audit_id,
            position_id=episode.position_id,
            phase=phase,
            captured_at=str(book.get("captured_at") or now_iso()),
            exit_intent_id=episode.exit_intent_id,
            materialized_at=now_iso(),
            book_source=book.get("book_source"),
            book_generation=book.get("book_generation"),
            book_update_number=book.get("book_update_number"),
            book_message_hash=book.get("book_message_hash"),
            exchange_timestamp_ms=book.get("exchange_timestamp_ms"),
            exchange_age_ms=book.get("exchange_age_ms"),
            receive_latency_ms=book.get("receive_latency_ms"),
            best_bid_text=book.get("best_bid_text"),
            best_ask_text=book.get("best_ask_text"),
            best_bid_size_text=book.get("best_bid_size_text"),
            bids_json=book.get("bids_json"),
            asks_json=book.get("asks_json"),
            level_count=book.get("level_count"),
            truncated=book.get("truncated", 0),
        )
        self.snapshots_written += 1

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #
    def _book_ref(
        self, update: dict[str, Any], token_id: str | None
    ) -> dict[str, Any]:
        bids = update.get("bids")
        if bids is not None:
            return {
                "mode": "inline",
                "bids": bids,
                "asks": update.get("asks"),
                "meta": {
                    "book_source": (
                        update.get("event_type") or update.get("source")
                    ),
                    "book_generation": _int(update.get("generation")),
                    "book_update_number": _int(update.get("update_number")),
                    "book_message_hash": update.get("message_hash"),
                    "exchange_timestamp_ms": _int(
                        update.get("exchange_timestamp_ms")
                    ),
                    "exchange_age_ms": _int(update.get("exchange_age_ms")),
                    "receive_latency_ms": _int(
                        update.get("receive_latency_ms")
                    ),
                    "best_bid_text": _text(update.get("best_bid")),
                    "best_ask_text": _text(update.get("best_ask")),
                    "best_bid_size_text": _text(update.get("best_bid_size")),
                },
            }
        return {"mode": "callable", "token_id": token_id}

    def _serialize_book(
        self, ref: Any, captured_at: Any
    ) -> dict[str, Any] | None:
        if not ref:
            return None
        if ref.get("mode") == "callable":
            provider = self._book_provider
            if provider is None:
                return None
            try:
                view = provider(str(ref.get("token_id") or ""))
            except Exception as exc:
                self.last_error = (
                    f"EXIT_EVIDENCE_BOOK:{type(exc).__name__}:{exc}"
                )[:500]
                return None
            if not view:
                return None
            ref = self._book_ref(dict(view), ref.get("token_id"))
            if ref.get("mode") == "callable":
                return None

        bids = list(ref.get("bids") or [])
        asks = list(ref.get("asks") or [])
        truncated = 1 if len(bids) > _MAX_LADDER_LEVELS else 0
        meta = dict(ref.get("meta") or {})
        meta.update(
            {
                "captured_at": captured_at,
                "bids_json": json.dumps(bids[:_MAX_LADDER_LEVELS]),
                "asks_json": json.dumps(asks[:_MAX_LADDER_LEVELS]),
                "level_count": len(bids),
                "truncated": truncated,
            }
        )
        return meta

    def _fill_times(self, intent_id: str | None) -> dict[str, Any]:
        if not intent_id:
            return {}
        try:
            with self.repo.base.connect() as conn:
                row = conn.execute(
                    "SELECT MIN(matched_at) AS first_at, MAX(matched_at) AS last_at "
                    "FROM live_strategy_fills WHERE intent_id=?",
                    (intent_id,),
                ).fetchone()
        except Exception:
            return {}
        if row is None:
            return {}
        out: dict[str, Any] = {}
        if row["first_at"]:
            out["first_fill_at"] = str(row["first_at"])
        if row["last_at"]:
            out["last_fill_at"] = str(row["last_at"])
        return out

    @staticmethod
    def _delta_ms(start: Any, end: Any) -> int | None:
        from datetime import datetime

        def _parse(value: Any) -> Any:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None

        a, b = _parse(start), _parse(end)
        if a is None or b is None:
            return None
        return int(round((b - a).total_seconds() * 1000))

    @staticmethod
    def _evidence_quality(episode: _Episode, deep: bool) -> str:
        if episode.degraded:
            return episode.degraded
        if not deep:
            return "OK"
        have = set(episode.books)
        if "SUBMIT" not in have:
            return "DEGRADED_NO_BOOK_AT_SUBMIT"
        if "CROSS" not in have:
            return "DEGRADED_NO_BOOK_AT_CROSS"
        return "OK"
