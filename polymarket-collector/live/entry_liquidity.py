"""Entry-time liquidity / exit-ability instrumentation.

Phase 1 is measurement only: right around a BUY the trader records the bid side
of the token it is about to hold — cumulative depth above a set of price levels,
and a simulated marketable exit (VWAP, worst fill price, slippage vs best bid)
for the position size and buffer multiples of it. None of this gates or delays
an order.

Hot-path safety mirrors :mod:`live.exit_forensics`:

* Every ``note_*`` method is a synchronous dict update + ``deque.append`` +
  ``Event.set()``. No serialization, no book walk, no DB write on the BUY path.
* All materialization runs on :meth:`EntryLiquidityCollector.run`, a dedicated
  asyncio task that swallows every exception into ``last_error``.
* The queue is bounded; on overflow the oldest job is dropped and the affected
  episode is marked ``DEGRADED_EVIDENCE_DROPPED``.

An *episode* is one entry attempt for one intent. It is created on
``note_signal`` and finalized on fill / abort / terminal submit result, or by a
time-based sweep as a backstop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
import json
import time
from typing import Any, Callable

from .book_walk import book_walk_detail, cumulative_bid_depth
from .repository import now_iso
from .strategy_repository import StrategyRepository, stable_id

_MAX_QUEUE = 512
_MAX_LADDER_LEVELS = 250
_EPISODE_TTL_SECONDS = 120.0
_DEFAULT_BUCKET_LEVELS = (
    Decimal("0.66"), Decimal("0.60"), Decimal("0.55"), Decimal("0.46"),
)
_DEFAULT_BUFFER_MULTIPLES = (Decimal("3"), Decimal("4"), Decimal("5"))
_DEFAULT_DEEP_CAPTURE_MAX_VWAP = Decimal("0.60")

_PHASE_ORDER = ("SUBMIT", "REVALIDATION", "SIGNAL", "FILL")
_PHASE_PREFIX = {
    "SIGNAL": "signal",
    "REVALIDATION": "reval",
    "SUBMIT": "submit",
    "FILL": "fill",
}


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


def _spread_text(bid: Any, ask: Any) -> str | None:
    b, a = _dec(bid), _dec(ask)
    if b is None or a is None:
        return None
    return format(a - b, "f")


class _EntryEpisode:
    __slots__ = (
        "intent_id", "event_id", "condition_id", "token_id", "side",
        "requested_shares", "filled_shares",
        "fields", "books", "book_refs",
        "created_monotonic", "finalized", "degraded",
    )

    def __init__(
        self,
        intent_id: str,
        *,
        event_id: str,
        condition_id: str | None,
        token_id: str | None,
        side: str | None,
        requested_shares: Any,
    ) -> None:
        self.intent_id = str(intent_id)
        self.event_id = str(event_id or "")
        self.condition_id = str(condition_id or "") or None
        self.token_id = str(token_id or "") or None
        self.side = side
        self.requested_shares = _dec(requested_shares)
        self.filled_shares: Decimal | None = None
        self.fields: dict[str, Any] = {}
        self.books: dict[str, dict[str, Any]] = {}
        self.book_refs: dict[str, Any] = {}
        self.created_monotonic = time.monotonic()
        self.finalized = False
        self.degraded: str | None = None


class EntryLiquidityCollector:
    def __init__(
        self,
        repo: StrategyRepository,
        *,
        bucket_levels: Any = None,
        buffer_multiples: Any = None,
        deep_capture_max_vwap: Decimal | str | float | None = None,
        book_provider: Callable[[str], dict[str, Any] | None] | None = None,
        logger: Any = None,
    ) -> None:
        self.repo = repo
        self.bucket_levels = self._decimal_tuple(
            bucket_levels, _DEFAULT_BUCKET_LEVELS
        )
        self.buffer_multiples = self._decimal_tuple(
            buffer_multiples, _DEFAULT_BUFFER_MULTIPLES
        )
        self.deep_capture_max_vwap = (
            _dec(deep_capture_max_vwap) or _DEFAULT_DEEP_CAPTURE_MAX_VWAP
        )
        self._book_provider = book_provider
        self._logger = logger
        self._queue: deque[dict[str, Any]] = deque()
        self._episodes: dict[str, _EntryEpisode] = {}
        self.wakeup = asyncio.Event()
        self.enqueued = 0
        self.applied = 0
        self.dropped = 0
        self.audits_written = 0
        self.snapshots_written = 0
        self.last_error = ""

    @staticmethod
    def _decimal_tuple(value: Any, default: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        if value is None:
            return default
        out: list[Decimal] = []
        for item in value:
            d = _dec(item)
            if d is not None:
                out.append(d)
        return tuple(out) or default

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
    def _enqueue(self, job: dict[str, Any]) -> None:
        if len(self._queue) >= _MAX_QUEUE:
            self._queue.popleft()
            self.dropped += 1
            iid = job.get("intent_id")
            episode = self._episodes.get(str(iid)) if iid else None
            if episode is not None:
                episode.degraded = "DEGRADED_EVIDENCE_DROPPED"
        self._queue.append(job)
        self.enqueued += 1
        self.wakeup.set()

    def _capture(
        self, episode: _EntryEpisode, phase: str, update: dict[str, Any]
    ) -> None:
        prefix = _PHASE_PREFIX[phase]
        bid = update.get("best_bid")
        ask = update.get("best_ask")
        episode.fields[f"{prefix}_best_bid_text"] = _text(bid)
        episode.fields[f"{prefix}_best_bid_size_text"] = _text(
            update.get("best_bid_size")
        )
        if phase != "FILL":
            episode.fields[f"{prefix}_best_ask_text"] = _text(ask)
            episode.fields[f"{prefix}_spread_text"] = _spread_text(bid, ask)
        if phase in ("REVALIDATION", "SUBMIT"):
            episode.fields[f"{prefix}_book_generation"] = _int(
                update.get("generation")
            )
            episode.fields[f"{prefix}_book_hash"] = update.get("message_hash")
        episode.fields[f"{prefix}_book_age_ms"] = _int(
            update.get("exchange_age_ms")
        )
        episode.book_refs[phase] = self._book_ref(update, episode.token_id)
        self._enqueue({
            "kind": "capture_book",
            "intent_id": episode.intent_id,
            "phase": phase,
            "captured_at": now_iso(),
        })
        self._enqueue({"kind": "sync", "intent_id": episode.intent_id})

    def note_signal(
        self,
        intent_id: str,
        *,
        event_id: str,
        condition_id: str | None,
        token_id: str | None,
        side: str | None,
        requested_shares: Any,
        update: dict[str, Any],
    ) -> None:
        iid = str(intent_id)
        episode = self._episodes.get(iid)
        if episode is None:
            episode = _EntryEpisode(
                iid, event_id=event_id, condition_id=condition_id,
                token_id=token_id, side=side, requested_shares=requested_shares,
            )
            self._episodes[iid] = episode
        self._capture(episode, "SIGNAL", update)

    def note_revalidation(
        self,
        intent_id: str,
        *,
        update: dict[str, Any] | None,
        result_reason: str | None = None,
    ) -> None:
        episode = self._episodes.get(str(intent_id))
        if episode is None or not update:
            return
        self._capture(episode, "REVALIDATION", update)

    def note_submit(
        self,
        intent_id: str,
        *,
        update: dict[str, Any] | None,
        requested_shares: Any = None,
    ) -> None:
        episode = self._episodes.get(str(intent_id))
        if episode is None or not update:
            return
        if requested_shares is not None:
            episode.requested_shares = _dec(requested_shares)
        self._capture(episode, "SUBMIT", update)

    def note_submit_result(
        self,
        intent_id: str,
        *,
        clob_status: str | None = None,
        remote_order_id: str | None = None,
    ) -> None:
        episode = self._episodes.get(str(intent_id))
        if episode is None:
            return
        if clob_status is not None:
            episode.fields["clob_status"] = str(clob_status)
        terminal = str(clob_status or "").lower() in {
            "rejected", "failed", "blocked", "fak_not_filled", "zero_fill",
        }
        self._enqueue({"kind": "sync", "intent_id": episode.intent_id})
        if terminal:
            self._enqueue({"kind": "finalize", "intent_id": episode.intent_id})

    def note_fill(
        self,
        intent_id: str,
        *,
        filled_shares: Any,
        fill_price: Any = None,
        update: dict[str, Any] | None = None,
    ) -> None:
        episode = self._episodes.get(str(intent_id))
        if episode is None:
            return
        episode.filled_shares = _dec(filled_shares)
        if update:
            self._capture(episode, "FILL", update)
        self._enqueue({"kind": "finalize", "intent_id": episode.intent_id})

    def note_abort(self, intent_id: str, *, reason: str | None = None) -> None:
        episode = self._episodes.get(str(intent_id))
        if episode is None:
            return
        if reason:
            episode.fields["entry_liquidity_abort_reason"] = str(reason)
        self._enqueue({"kind": "finalize", "intent_id": episode.intent_id})

    def reconcile_active(self, active_intent_ids: set[str]) -> None:
        for iid, episode in list(self._episodes.items()):
            if iid in active_intent_ids or episode.finalized:
                continue
            self._enqueue({"kind": "finalize", "intent_id": iid})

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
            self._sweep_stale()
            self.drain_pending()

    def _sweep_stale(self) -> None:
        cutoff = time.monotonic() - _EPISODE_TTL_SECONDS
        for iid, episode in list(self._episodes.items()):
            if not episode.finalized and episode.created_monotonic < cutoff:
                self._queue.append({"kind": "finalize", "intent_id": iid})
                self.enqueued += 1

    def drain_pending(self) -> None:
        while self._queue:
            job = self._queue.popleft()
            try:
                self._apply(job)
            except Exception as exc:  # never propagate onto the entry path
                self.last_error = (
                    f"ENTRY_LIQUIDITY:{type(exc).__name__}:{exc}"
                )[:500]
                if self._logger is not None:
                    self._logger.warning(
                        "entry-liquidity job failed kind=%s: %s",
                        job.get("kind"), exc,
                    )
            finally:
                self.applied += 1

    def _apply(self, job: dict[str, Any]) -> None:
        kind = job.get("kind")
        iid = str(job.get("intent_id") or "")
        episode = self._episodes.get(iid)
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
            self._write_audit(episode)
            return
        if kind == "finalize":
            self._finalize(episode)
            return

    def _write_audit(self, episode: _EntryEpisode, **extra: Any) -> None:
        fields = dict(episode.fields)
        fields.update(extra)
        if episode.degraded and not fields.get("entry_liquidity_evidence_quality"):
            fields["entry_liquidity_evidence_quality"] = episode.degraded
        payload = {k: v for k, v in fields.items() if v is not None}
        if not payload:
            return
        self.repo.record_entry_audit(
            episode.intent_id,
            event_id=episode.event_id,
            condition_id=episode.condition_id,
            token_id=episode.token_id,
            side=episode.side,
            **payload,
        )
        self.audits_written += 1

    def _finalize(self, episode: _EntryEpisode) -> None:
        if episode.finalized:
            return

        primary = next((p for p in _PHASE_ORDER if p in episode.books), None)
        size = episode.filled_shares or episode.requested_shares
        final: dict[str, Any] = {
            "entry_liquidity_captured_at": now_iso(),
            "liquidity_bucket_levels_text": ",".join(
                str(lvl) for lvl in self.bucket_levels
            ),
            "liquidity_buffer_multiples_text": ",".join(
                str(m) for m in self.buffer_multiples
            ),
        }
        if size is not None:
            final["sim_exit_shares_text"] = _text(size)

        deep = False
        if primary is not None:
            final["entry_liquidity_phase_primary"] = primary
            book = episode.books[primary]
            bids_json = book.get("bids_json")
            best_bid = book.get("best_bid_text")
            final["full_ladder_captured_synchronously"] = (
                1 if book.get("_inline") else 0
            )

            depth = cumulative_bid_depth(bids_json, self.bucket_levels)
            for column, level in zip(
                ("depth_at_066_text", "depth_at_060_text",
                 "depth_at_055_text", "depth_at_046_text"),
                self.bucket_levels,
            ):
                final[column] = _text(depth.get(level))

            if size is not None and size > 0:
                walk = book_walk_detail(bids_json, size, best_bid=best_bid)
                final["sim_exit_vwap_text"] = _text(walk.vwap)
                final["sim_exit_method"] = walk.method
                final["sim_exit_fillable_shares_text"] = _text(
                    walk.fillable_shares
                )
                final["sim_exit_worst_price_text"] = _text(walk.worst_price)
                bb = _dec(best_bid)
                if bb is not None and walk.vwap is not None:
                    final["sim_exit_slippage_text"] = _text(bb - walk.vwap)

                buffers: dict[str, Any] = {}
                for multiple in self.buffer_multiples:
                    b_walk = book_walk_detail(
                        bids_json, size * multiple, best_bid=best_bid
                    )
                    slip = (
                        _text(bb - b_walk.vwap)
                        if bb is not None and b_walk.vwap is not None
                        else None
                    )
                    buffers[str(multiple)] = {
                        "vwap": _text(b_walk.vwap),
                        "method": b_walk.method,
                        "fillable_shares": _text(b_walk.fillable_shares),
                        "worst_price": _text(b_walk.worst_price),
                        "slippage": slip,
                    }
                final["sim_exit_buffers_json"] = json.dumps(buffers)

                partial = (
                    walk.fillable_shares is not None
                    and walk.fillable_shares < size
                )
                vwap_bad = (
                    walk.vwap is not None
                    and walk.vwap < self.deep_capture_max_vwap
                )
                deep = bool(
                    partial
                    or vwap_bad
                    or walk.method in {"BEST_BID_FALLBACK", "NONE"}
                )

        final["entry_liquidity_deep_capture"] = 1 if deep else 0
        final["entry_liquidity_evidence_quality"] = self._evidence_quality(
            episode, primary
        )

        self._write_audit(episode, **final)

        if deep:
            for phase, book in episode.books.items():
                self._write_snapshot(episode, phase, book, size)

        episode.finalized = True
        self._episodes.pop(episode.intent_id, None)

    def _write_snapshot(
        self,
        episode: _EntryEpisode,
        phase: str,
        book: dict[str, Any],
        size: Decimal | None,
    ) -> None:
        bids_json = book.get("bids_json")
        depth = cumulative_bid_depth(bids_json, self.bucket_levels)
        cumulative_depth_json = json.dumps(
            {str(lvl): _text(depth.get(lvl)) for lvl in self.bucket_levels}
        )
        sim_exit_json = None
        if size is not None and size > 0:
            walk = book_walk_detail(
                bids_json, size, best_bid=book.get("best_bid_text")
            )
            sim_exit_json = json.dumps({
                "shares": _text(size),
                "vwap": _text(walk.vwap),
                "method": walk.method,
                "fillable_shares": _text(walk.fillable_shares),
                "worst_price": _text(walk.worst_price),
            })
        self.repo.record_entry_book_snapshot(
            stable_id("entry-book", f"{episode.intent_id}:{phase}"),
            entry_intent_id=episode.intent_id,
            phase=phase,
            captured_at=str(book.get("captured_at") or now_iso()),
            event_id=episode.event_id or None,
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
            bids_json=bids_json,
            asks_json=book.get("asks_json"),
            level_count=book.get("level_count"),
            truncated=book.get("truncated", 0),
            cumulative_depth_json=cumulative_depth_json,
            sim_exit_json=sim_exit_json,
            full_ladder_captured_synchronously=(
                1 if book.get("_inline") else 0
            ),
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
        inline = ref.get("mode") == "inline"
        if ref.get("mode") == "callable":
            provider = self._book_provider
            if provider is None:
                return None
            try:
                view = provider(str(ref.get("token_id") or ""))
            except Exception as exc:
                self.last_error = (
                    f"ENTRY_LIQUIDITY_BOOK:{type(exc).__name__}:{exc}"
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
        meta.update({
            "captured_at": captured_at,
            "bids_json": json.dumps(bids[:_MAX_LADDER_LEVELS]),
            "asks_json": json.dumps(asks[:_MAX_LADDER_LEVELS]),
            "level_count": len(bids),
            "truncated": truncated,
            "_inline": inline,
        })
        return meta

    @staticmethod
    def _evidence_quality(
        episode: _EntryEpisode, primary: str | None
    ) -> str:
        if episode.degraded:
            return episode.degraded
        if primary is None:
            return "DEGRADED_NO_BOOK"
        return "OK"
