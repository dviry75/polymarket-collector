"""BTC reference gate evaluation and persistence, off the trading hot path.

``evaluate`` and every ``note_*`` method are synchronous in-memory work (dict
lookups, Decimal arithmetic, dict/deque updates). Nothing here performs I/O on
the caller's stack. A dedicated task (``run``) drains the pending state to
SQLite through ``asyncio.to_thread`` once a second, and from there also
writes the per-window reference rows and the BTC exit snapshot of closed
deals. Exits never wait on, or read, anything in this module.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import time
from typing import Any

from .btc_reference_feed import (
    WINDOW_SECONDS,
    BtcReferenceProvider,
    side_outcome_map,
    window_start_from_event_id,
)
from .order_book import canonical_decimal, decimal_value
from .strategy import BtcGateConfig, BtcGateDecision, btc_reference_gate

EXIT_TYPE_BY_PURPOSE = {"TAKE_PROFIT": "TP", "STOP_066": "STOP"}
RESOLUTION_FINAL_REASONS = {"MARKET_RESOLUTION", "REDEEMED"}
# A fill's BTC tick can arrive ~1.4 s (p95 ~2 s) after its payload timestamp.
EXIT_SETTLE_MS = 5000
EXIT_LOOKBACK = timedelta(hours=6)
REFERENCE_WINDOWS_TRACKED = 3


def _text(value: Decimal | None) -> str | None:
    return canonical_decimal(value) if value is not None else None


def _iso_from_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time_ms(value: Any) -> int | None:
    """Epoch seconds/ms (number or numeric string) or ISO-8601 -> epoch ms."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        number = Decimal(text)
    except Exception:
        number = None
    if number is not None and number.is_finite():
        return int(number if number > 10 ** 12 else number * 1000)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def exit_type_for_purpose(purpose: str | None) -> str:
    name = str(purpose or "").upper()
    if name in EXIT_TYPE_BY_PURPOSE:
        return EXIT_TYPE_BY_PURPOSE[name]
    if name.startswith("EMERGENCY"):
        return "EMERGENCY"
    if name.startswith("STOP"):
        return "STOP"
    return name or "UNKNOWN"


def directional(side_outcome: str | None, price: Decimal | None,
                open_price: Decimal | None) -> Decimal | None:
    if price is None or open_price is None or side_outcome not in {"UP", "DOWN"}:
        return None
    delta = price - open_price
    return delta if side_outcome == "UP" else -delta


class BtcReferenceRecorder:
    def __init__(
        self,
        config: Any,
        strategy_repo: Any,
        base_repo: Any,
        *,
        provider: BtcReferenceProvider | None = None,
        logger: logging.Logger | None = None,
    ):
        self.repo = strategy_repo
        self.base = base_repo
        self.provider = provider
        self.gate_config = BtcGateConfig(
            mode=str(getattr(config, "btc_gate_mode", "observe")),
            min_delta_usd=getattr(config, "btc_gate_min_delta_usd", Decimal("10")),
            max_delta_usd=getattr(config, "btc_gate_max_delta_usd", None),
            max_price_age_ms=int(getattr(config, "btc_max_price_age_ms", 5000)),
        )
        self.open_tick_max_offset_ms = int(
            getattr(config, "btc_open_tick_max_offset_ms", 2000)
        )
        self._logger = logger or logging.getLogger(__name__)
        self._side_maps: dict[str, dict[str, str] | None] = {}
        self._evaluations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._deal_entries: deque[dict[str, Any]] = deque()
        self._reference_written: dict[int, tuple[Any, ...]] = {}
        self._last_exit_scan = 0.0
        self._last_outcome_fill = 0.0
        self._last_health_publish = 0.0
        self.evaluations_noted = 0
        self.decision_blocks = 0
        self.would_block = 0
        self.rows_written = 0
        self.deal_entries_written = 0
        self.deal_exits_written = 0
        self.writer_failures = 0
        self.last_error = ""

    @property
    def mode(self) -> str:
        return self.gate_config.mode

    def now_ms(self) -> int:
        if self.provider is not None:
            return self.provider.now_ms()
        return int(time.time() * 1000)

    # --------------------------------------------------------- hot path
    def side_outcome(self, market: dict[str, Any], side: str | None) -> str | None:
        condition_id = str(market.get("condition_id") or "")
        if condition_id not in self._side_maps:
            if len(self._side_maps) > 256:
                self._side_maps.clear()
            self._side_maps[condition_id] = side_outcome_map(market)
        mapping = self._side_maps[condition_id] or {}
        return mapping.get(str(side or "").upper())

    def evaluate(self, *, market: dict[str, Any], side: str | None,
                 token_id: str | None) -> dict[str, Any]:
        """Gate verdict plus the BTC values it saw. Memory reads only."""
        now = self.now_ms()
        event_id = str(market.get("event_id") or "")
        window_start = window_start_from_event_id(event_id)
        side_outcome = self.side_outcome(market, side)
        provider = self.provider
        current = provider.current() if provider is not None else None
        open_tick = (
            provider.open_price(window_start)
            if provider is not None and window_start is not None else None
        )
        age = (now - current.ts_ms) if current is not None else None
        decision = btc_reference_gate(
            side_outcome,
            open_tick.price if open_tick else None,
            current.price if current else None,
            age,
            self.gate_config,
        )
        return {
            "event_id": event_id,
            "window_start": window_start,
            "side": str(side or ""),
            "token_id": str(token_id or ""),
            "side_outcome": side_outcome,
            "open": open_tick,
            "current": current,
            "age_ms": age,
            "decision": decision,
            "observed_ms": now,
        }

    def error_evaluation(self, *, market: dict[str, Any], side: str | None,
                         token_id: str | None) -> dict[str, Any]:
        """Fail-closed verdict used when evaluation itself raised."""
        return {
            "event_id": str(market.get("event_id") or ""),
            "window_start": None, "side": str(side or ""),
            "token_id": str(token_id or ""), "side_outcome": None,
            "open": None, "current": None, "age_ms": None,
            "decision": BtcGateDecision(False, "BTC_GATE_ERROR"),
            "observed_ms": self.now_ms(),
        }

    def blocks(self, evaluation: dict[str, Any]) -> bool:
        return self.mode == "enforce" and not evaluation["decision"].allowed

    def note_evaluation(
        self,
        evaluation: dict[str, Any],
        *,
        source: str,
        trading_state: str,
        remaining_seconds: Decimal | None = None,
        intent_id: str | None = None,
    ) -> None:
        if self.mode == "off":
            return
        decision: BtcGateDecision = evaluation["decision"]
        key = (evaluation["event_id"], evaluation["side"], source, decision.reason)
        directional_delta = decision.directional_delta_usd
        row = self._evaluations.get(key)
        if row is None:
            # Raw values only; text formatting happens in the writer task.
            row = self._evaluations[key] = {
                "event_id": key[0], "side": key[1], "source": source,
                "reason": decision.reason, "first_ms": evaluation["observed_ms"],
                "count": 0, "first_dir": directional_delta,
                "max_dir": directional_delta, "min_dir": directional_delta,
                "intent_id": None,
            }
        elif directional_delta is not None:
            if row["max_dir"] is None or directional_delta > row["max_dir"]:
                row["max_dir"] = directional_delta
            if row["min_dir"] is None or directional_delta < row["min_dir"]:
                row["min_dir"] = directional_delta
        row["last_ms"] = evaluation["observed_ms"]
        row["count"] += 1
        row["trading_state"] = trading_state
        row["evaluation"] = evaluation
        row["remaining"] = remaining_seconds
        if intent_id:
            row["intent_id"] = intent_id
        self.evaluations_noted += 1
        if source == "DECISION" and not decision.allowed:
            if self.mode == "enforce":
                self.decision_blocks += 1
            else:
                self.would_block += 1

    def attach_intent(self, evaluation: dict[str, Any], intent_id: str) -> None:
        decision: BtcGateDecision = evaluation["decision"]
        key = (evaluation["event_id"], evaluation["side"], "DECISION", decision.reason)
        row = self._evaluations.get(key)
        if row is not None:
            row["intent_id"] = intent_id
        elif self.mode != "off":
            # Already flushed: an update row with count 0 sets the intent id.
            self.note_evaluation(evaluation, source="DECISION",
                                 trading_state="ENTRY_RESERVED", intent_id=intent_id)
            self._evaluations[key]["count"] = 0
            self.evaluations_noted -= 1

    def note_deal_entry(self, evaluation: dict[str, Any], *, intent_id: str) -> None:
        decision: BtcGateDecision = evaluation["decision"]
        current = evaluation["current"]
        open_tick = evaluation["open"]
        missing = None
        if current is None:
            missing = "BTC_FEED_UNAVAILABLE"
        elif evaluation["age_ms"] is None or evaluation["age_ms"] > self.gate_config.max_price_age_ms:
            missing = "BTC_PRICE_STALE"
        elif open_tick is None:
            missing = "BTC_OPEN_PRICE_UNKNOWN"
        elif evaluation["side_outcome"] is None:
            missing = "BTC_SIDE_MAPPING_UNKNOWN"
        stale = missing in {"BTC_FEED_UNAVAILABLE", "BTC_PRICE_STALE"}
        self._deal_entries.append({
            "event_id": evaluation["event_id"],
            "entry_intent_id": intent_id,
            "values": {
                "btc_open_price_text": _text(open_tick.price) if open_tick else None,
                "btc_open_tick_ts_ms": open_tick.ts_ms if open_tick else None,
                "btc_entry_price_text": None if stale else _text(current.price),
                "btc_entry_ts_ms": None if stale else current.ts_ms,
                "btc_entry_age_ms": None if stale else evaluation["age_ms"],
                "btc_entry_directional_delta_usd": _text(decision.directional_delta_usd),
                "btc_gate_reason": None if self.mode == "off" else decision.reason,
                "btc_gate_mode": self.mode,
                "btc_entry_missing_reason": missing,
            },
        })

    # ------------------------------------------------------ background
    async def run(self, stop: asyncio.Event, *, interval_seconds: float = 1.0) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval_seconds)
            except asyncio.TimeoutError:
                pass
            await self.flush()
        await self.flush()  # final drain on shutdown

    async def flush(self, *, force_scans: bool = False) -> None:
        now = time.monotonic()
        steps = [self._flush_evaluations, self._flush_deal_entries, self._flush_references]
        if force_scans or now - self._last_exit_scan >= 2.0:
            self._last_exit_scan = now
            steps.append(self._flush_deal_exits)
        if force_scans or now - self._last_outcome_fill >= 30.0:
            self._last_outcome_fill = now
            steps.append(self._flush_actual_outcomes)
        if force_scans or now - self._last_health_publish >= 15.0:
            self._last_health_publish = now
            steps.append(self._publish_health)
        for step in steps:
            try:
                await step()
            except Exception as exc:  # telemetry must never break trading
                self.writer_failures += 1
                self.last_error = f"{step.__name__}:{type(exc).__name__}:{exc}"[:500]
                self._logger.warning("BTC_REFERENCE_WRITE_FAILED %s", self.last_error)

    async def _flush_evaluations(self) -> None:
        if not self._evaluations:
            return
        pending, self._evaluations = self._evaluations, {}
        rows = [self._evaluation_db_row(row) for row in pending.values()]
        try:
            self.rows_written += await asyncio.to_thread(self.repo.upsert_btc_gate_evaluations, rows)
        except Exception:
            for key, row in pending.items():
                self._evaluations.setdefault(key, row)
            raise

    def _evaluation_db_row(self, row: dict[str, Any]) -> dict[str, Any]:
        evaluation = row["evaluation"]
        decision: BtcGateDecision = evaluation["decision"]
        current = evaluation["current"]
        open_tick = evaluation["open"]
        return {
            "event_id": row["event_id"], "side": row["side"], "source": row["source"],
            "reason": row["reason"], "count": row["count"],
            "first_seen_at": _iso_from_ms(row["first_ms"]),
            "last_seen_at": _iso_from_ms(row["last_ms"]),
            "trading_state": row["trading_state"],
            "side_outcome": evaluation["side_outcome"],
            "token_id": evaluation["token_id"],
            "mode": self.mode,
            "open_price_text": _text(open_tick.price) if open_tick else None,
            "current_price_text": _text(current.price) if current else None,
            "current_payload_ts_ms": current.ts_ms if current else None,
            "current_age_ms": evaluation["age_ms"],
            "delta_usd": _text(decision.delta_usd),
            "directional_delta_usd": _text(decision.directional_delta_usd),
            "first_directional_delta_usd": _text(row["first_dir"]),
            "max_directional_delta_usd": _text(row["max_dir"]),
            "min_directional_delta_usd": _text(row["min_dir"]),
            "remaining_seconds": _text(row["remaining"]),
            "allowed": int(decision.allowed),
            "would_block": int(not decision.allowed),
            "intent_id": row["intent_id"],
        }

    async def _flush_deal_entries(self) -> None:
        while self._deal_entries:
            job = self._deal_entries[0]
            await asyncio.to_thread(
                self.repo.record_deal_btc_entry,
                event_id=job["event_id"], entry_intent_id=job["entry_intent_id"],
                values=job["values"],
            )
            self._deal_entries.popleft()
            self.deal_entries_written += 1

    def reference_rows(self, now_ms: int | None = None) -> list[dict[str, Any]]:
        """Reference rows for the windows around now whose content changed."""
        provider = self.provider
        if provider is None:
            return []
        now = self.now_ms() if now_ms is None else now_ms
        newest = now // 1000 // WINDOW_SECONDS * WINDOW_SECONDS
        rows = []
        for index in range(REFERENCE_WINDOWS_TRACKED):
            window_start = newest - index * WINDOW_SECONDS
            open_tick = provider.open_price(window_start)
            close_tick = provider.close_price(window_start + WINDOW_SECONDS)
            stats = provider.state.window_stats(window_start)
            derived = "UNKNOWN"
            if open_tick is not None and close_tick is not None:
                # Gamma: Up when the end value is >= the value at the start.
                derived = "UP" if close_tick.price >= open_tick.price else "DOWN"
            row = {
                "event_id": f"btc-updown-5m-{window_start}",
                "window_start": window_start,
                "open_price_text": _text(open_tick.price) if open_tick else None,
                "open_tick_ts_ms": open_tick.ts_ms if open_tick else None,
                "open_received_at": _iso_from_ms(open_tick.received_ms) if open_tick else None,
                "close_price_text": _text(close_tick.price) if close_tick else None,
                "close_tick_ts_ms": close_tick.ts_ms if close_tick else None,
                "derived_outcome": derived,
                "feed_gaps_in_window": stats["gaps"],
                "feed_stalls_in_window": stats["stalls"],
            }
            signature = tuple(row.values())
            if self._reference_written.get(window_start) == signature:
                continue
            rows.append(row)
        return rows

    async def _flush_references(self) -> None:
        rows = self.reference_rows()
        if not rows:
            return
        await asyncio.to_thread(self.repo.upsert_btc_event_references, rows)
        for row in rows:
            self._reference_written[row["window_start"]] = tuple(row.values())
        if len(self._reference_written) > 32:
            for key in sorted(self._reference_written)[:-16]:
                del self._reference_written[key]

    def exit_values(self, deal: dict[str, Any], now_ms: int | None = None) -> dict[str, Any] | None:
        """BTC exit snapshot for a terminal deal; None when not ready yet."""
        now = self.now_ms() if now_ms is None else now_ms
        provider = self.provider
        event_id = str(deal.get("event_id") or "")
        window_start = window_start_from_event_id(event_id)
        mapping = side_outcome_map(deal) or {}
        side_outcome = mapping.get(str(deal.get("outcome") or "").upper())
        open_price = decimal_value(deal.get("btc_open_price_text"))
        if open_price is None and provider is not None and window_start is not None:
            tick = provider.open_price(window_start)
            open_price = tick.price if tick else None

        fills = []
        for fill in deal.get("exit_fills") or []:
            at = parse_time_ms(fill.get("matched_at")) or parse_time_ms(fill.get("created_at"))
            if at is not None:
                fills.append((at, fill.get("purpose")))
        if not fills:
            for intent in deal.get("exit_intents") or []:
                if (decimal_value(intent.get("filled_shares_text")) or Decimal("0")) <= 0:
                    continue
                at = parse_time_ms(intent.get("final_at")) or parse_time_ms(intent.get("updated_at"))
                if at is not None:
                    fills.append((at, intent.get("purpose")))

        values: dict[str, Any] = {
            "btc_exit_price_text": None, "btc_exit_ts_ms": None, "btc_exit_age_ms": None,
            "btc_exit_directional_delta_usd": None, "btc_exit_type": None,
            "btc_exit_missing_reason": None,
        }
        resolution = (
            str(deal.get("final_reason") or "").upper() in RESOLUTION_FINAL_REASONS
            or (not fills and str(deal.get("state") or "") != "CLOSED")
        )
        if resolution:
            if window_start is None:
                values["btc_exit_missing_reason"] = "EVENT_WINDOW_UNKNOWN"
                return values
            window_end = window_start + WINDOW_SECONDS
            if now < window_end * 1000 + self.open_tick_max_offset_ms + EXIT_SETTLE_MS:
                return None
            values["btc_exit_type"] = "RESOLUTION"
            if provider is None:
                values["btc_exit_missing_reason"] = "BTC_FEED_UNAVAILABLE"
                return values
            tick, status = provider.state.boundary_status(window_end)
            if tick is None:
                values["btc_exit_missing_reason"] = f"BTC_CLOSE_PRICE_{status}"
                return values
            values.update(
                btc_exit_price_text=_text(tick.price), btc_exit_ts_ms=tick.ts_ms,
                btc_exit_age_ms=tick.ts_ms - window_end * 1000,
                btc_exit_directional_delta_usd=_text(directional(side_outcome, tick.price, open_price)),
            )
            return values

        if not fills:
            values["btc_exit_missing_reason"] = "NO_EXIT_FILL"
            return values
        fill_ms, purpose = max(fills, key=lambda item: item[0])
        if now < fill_ms + EXIT_SETTLE_MS:
            return None
        values["btc_exit_type"] = exit_type_for_purpose(purpose)
        if provider is None:
            values["btc_exit_missing_reason"] = "BTC_FEED_UNAVAILABLE"
            return values
        tick = provider.price_at(fill_ms)
        if tick is None:
            values["btc_exit_missing_reason"] = "BTC_TICK_NOT_IN_BUFFER"
            return values
        age = fill_ms - tick.ts_ms
        if age > self.gate_config.max_price_age_ms:
            values["btc_exit_missing_reason"] = "BTC_PRICE_STALE"
            return values
        values.update(
            btc_exit_price_text=_text(tick.price), btc_exit_ts_ms=tick.ts_ms,
            btc_exit_age_ms=age,
            btc_exit_directional_delta_usd=_text(directional(side_outcome, tick.price, open_price)),
        )
        return values

    async def _flush_deal_exits(self) -> None:
        since = (datetime.now(timezone.utc) - EXIT_LOOKBACK).isoformat()
        deals = await asyncio.to_thread(self.repo.deals_pending_btc_exit, since=since)
        for deal in deals:
            values = self.exit_values(deal)
            if values is None:
                continue
            if await asyncio.to_thread(
                self.repo.record_deal_btc_exit, event_id=str(deal["event_id"]), values=values,
            ):
                self.deal_exits_written += 1

    async def _flush_actual_outcomes(self) -> None:
        await asyncio.to_thread(self.repo.fill_btc_event_actual_outcomes)

    async def _publish_health(self) -> None:
        if self.provider is None or self.base is None:
            return
        health = self.provider.health()
        await asyncio.to_thread(
            self.base.set_states,
            {
                "btc_feed_provider": str(health["provider"]),
                "btc_feed_connected": "true" if health["connected"] else "false",
                "btc_feed_last_tick_age_ms": str(health["last_tick_age_ms"]),
                "btc_feed_reconnects": str(health["reconnects"]),
                "btc_feed_stalls": str(health["stalls"]),
                "btc_feed_gaps": str(health["gaps"]),
                "btc_gate_mode": self.mode,
            },
            "btc_reference",
        )

    def health(self) -> dict[str, Any]:
        return {
            "gate_mode": self.mode,
            "min_delta_usd": canonical_decimal(self.gate_config.min_delta_usd),
            "max_delta_usd": _text(self.gate_config.max_delta_usd),
            "max_price_age_ms": self.gate_config.max_price_age_ms,
            "feed": self.provider.health() if self.provider is not None else None,
            "evaluations_noted": self.evaluations_noted,
            "decision_blocks": self.decision_blocks,
            "would_block": self.would_block,
            "pending_evaluations": len(self._evaluations),
            "pending_deal_entries": len(self._deal_entries),
            "rows_written": self.rows_written,
            "deal_entries_written": self.deal_entries_written,
            "deal_exits_written": self.deal_exits_written,
            "writer_failures": self.writer_failures,
            "last_error": self.last_error,
        }
