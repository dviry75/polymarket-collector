from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import time
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters.base import TradingAdapter
from .config import LiveConfig
from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso
from .strategy import (
    AllInBudget,
    StrategyPolicy,
    choose_entry,
    exact_trigger,
    simulate_buy_fak,
    simulate_sell_fak,
)
from .strategy_repository import StrategyRepository, stable_id


def _is_confirmed_fak_zero_fill_response(response: dict[str, Any]) -> bool:
    if response.get("polymarket_order_id"):
        return False
    reason = " ".join(str(response.get("failure_reason") or "").lower().replace("_", " ").split())
    message = " ".join(str(response.get("message") or "").lower().replace("_", " ").split())
    if reason == "fak not filled":
        return True
    combined = f"{reason} {message}"
    return "fak" in combined and any(
        marker in combined
        for marker in (
            "no orders found to match",
            "no match",
            "not filled",
            "zero fill",
            "zero execution",
        )
    )


class LiveStrategyRuntime:
    def __init__(
        self,
        config: LiveConfig,
        base_repo: LiveRepository,
        strategy_repo: StrategyRepository,
        adapter: TradingAdapter,
        *,
        reconciliation: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ):
        self.config = config
        self.base = base_repo
        self.repo = strategy_repo
        self.adapter = adapter
        self.reconciliation = reconciliation
        self.policy = StrategyPolicy(
            entry_price=config.strategy_entry_price,
            entry_max_price=config.strategy_entry_max_price,
            take_profit_price=config.strategy_take_profit_price,
            stop_price=config.strategy_stop_price,
            emergency_price=config.strategy_emergency_price,
            stop_min_price=config.strategy_stop_min_price,
            emergency_min_price=config.strategy_emergency_min_price,
            max_spend=config.max_trade_amount_usd,
            max_exposure=config.max_total_exposure_usd,
            max_shares=config.max_trade_tokens,
            entry_window_seconds=config.strategy_entry_window_seconds,
        )
        self.policy.validate()
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._frame_task: asyncio.Task[Any] | None = None
        self._frame_event = asyncio.Event()
        self._pending_frames: OrderedDict[str, dict[str, Any]] = OrderedDict()

        # Exact entry triggers are never placed in the lossy/conflated queue.
        # Regular market state may be replaced by newer state, but an observed
        # exact 0.74 trigger must survive until the strategy evaluates it.
        self._critical_frames: deque[dict[str, Any]] = deque()

        self._frame_queue_capacity = 32
        self.frames_coalesced = 0
        self.frames_dropped = 0

        self.critical_triggers_queued = 0
        self.critical_triggers_processed = 0
        self.critical_triggers_dropped = 0
        self.max_critical_queue_depth = 0

        # Last observed top-of-book prices per condition/token.
        # Critical signals are latched only when price ENTERS an exact
        # trigger level, not on every frame while it remains there.
        self._critical_price_state: dict[
            tuple[str, str], dict[str, Any]
        ] = {}
        # NOT_READY observations are deduplicated independently. They must be
        # visible and fail-closed, but must not consume the next actionable
        # READY edge after deterministic book alignment/resync.
        self._critical_observed_price_state: dict[
            tuple[str, str], dict[str, Any]
        ] = {}

        self._stop = asyncio.Event()
        self.frames_processed = 0
        self.last_error = ""
        self._market_freshness: Callable[[str], dict[str, Any]] | None = None
        self._market_provider: Callable[[str], dict[str, Any] | None] | None = None
        self._logger = logging.getLogger(__name__)
        self._entry_trigger_log_state: dict[tuple[str, str], str] = {}

        self._hot_state_task: asyncio.Task[Any] | None = None
        self._hot_state_refresh_interval_seconds = 0.25
        self._hot_state_max_age_seconds = 1.0
        self.hot_state_refresh_failures = 0

        try:
            self._hot_state = self.repo.hot_state_snapshot()
            self._hot_state_refreshed_monotonic = time.monotonic()
        except Exception:
            # Startup failure must be fail-closed.
            self._hot_state = {
                "pause_entries": True,
                "kill_switch": True,
                "canary_armed": False,
                "canary_consumed": False,
                "reconciliation_readiness": "NOT_READY",
                "locked_event_ids": set(),
                "active_exposure": self.policy.max_exposure,
                "positions_by_token": {},
                "loaded_at": "",
            }
            self._hot_state_refreshed_monotonic = 0.0
            self.hot_state_refresh_failures += 1


    @staticmethod
    def entry_schedule_status(at: datetime | None = None) -> dict[str, Any]:
        instant = at or datetime.now(timezone.utc)
        local = instant.astimezone(ZoneInfo("Asia/Jerusalem"))
        friday_exception = local.date().isoformat() == "2026-08-14"
        inactive = (
            local.weekday() < 5
            and 14 <= local.hour < 23
            and not friday_exception
        )
        return {
            "allowed": not inactive,
            "reason": "ENTRY_SCHEDULE_INACTIVE" if inactive else "ENTRY_SCHEDULE_ACTIVE",
            "timezone": "Asia/Jerusalem",
            "local_time": local.isoformat(),
        }

    def set_market_freshness_provider(
        self, provider: Callable[[str], dict[str, Any]]
    ) -> None:
        self._market_freshness = provider

    def set_market_provider(
        self, provider: Callable[[str], dict[str, Any] | None]
    ) -> None:
        self._market_provider = provider

    def _market(self, condition_id: str) -> dict[str, Any] | None:
        """Use the in-memory market cache on the hot path.

        SQLite remains a fail-safe fallback for startup/tests or an unexpected
        cache miss, but normal Market WS strategy processing should stay in RAM.
        """
        if self._market_provider is not None:
            market = self._market_provider(str(condition_id))
            if market:
                return market
        return self.base.latest_market(str(condition_id))

    def _entry_state_from_ram(
        self,
        event_id: str,
    ) -> dict[str, Any]:
        snapshot = self._hot_state

        refreshed = self._hot_state_refreshed_monotonic
        age_seconds = (
            time.monotonic() - refreshed
            if refreshed > 0
            else float("inf")
        )
        stale = age_seconds > self._hot_state_max_age_seconds

        exposure = (
            decimal_value(snapshot.get("active_exposure"))
            or Decimal("0")
        )

        return {
            "ready": not stale,
            "stale": stale,
            "age_seconds": age_seconds,
            # A stale RAM snapshot is always fail-closed for ENTRY.
            "paused": (
                stale
                or bool(snapshot.get("pause_entries"))
                or bool(snapshot.get("kill_switch"))
            ),
            "pause_entries": bool(snapshot.get("pause_entries")),
            "kill_switch": bool(snapshot.get("kill_switch")),
            "canary_armed": bool(snapshot.get("canary_armed")),
            "canary_consumed": bool(snapshot.get("canary_consumed")),
            "reconciliation_readiness": str(
                snapshot.get(
                    "reconciliation_readiness",
                    "NOT_READY",
                )
            ),
            "event_locked": event_id in (
                snapshot.get("locked_event_ids") or set()
            ),
            "active_exposure": exposure,
        }

    def _positions_from_ram(
        self,
        token_id: str,
    ) -> list[dict[str, Any]]:
        positions = (
            self._hot_state.get("positions_by_token")
            or {}
        ).get(str(token_id), [])

        # Return shallow copies so strategy code cannot mutate the
        # immutable snapshot currently visible to other readers.
        return [
            dict(position)
            for position in positions
            if isinstance(position, dict)
        ]

    def _position_from_ram(
        self,
        token_id: str,
        position_id: str,
    ) -> dict[str, Any] | None:
        for position in self._positions_from_ram(
            token_id
        ):
            if str(
                position.get("position_id") or ""
            ) == str(position_id):
                return position

        return None

    async def _refresh_hot_state_once(self) -> None:
        try:
            snapshot = await asyncio.to_thread(
                self.repo.hot_state_snapshot
            )
        except Exception as exc:
            self.hot_state_refresh_failures += 1
            self.last_error = (
                f"HOT_STATE_REFRESH:{type(exc).__name__}:{exc}"
            )[:500]
            return

        # Atomic reference replacement. Readers never see a half-built state.
        self._hot_state = snapshot
        self._hot_state_refreshed_monotonic = time.monotonic()

    async def _hot_state_loop(self) -> None:
        while not self._stop.is_set():
            await self._refresh_hot_state_once()

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    self._hot_state_refresh_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _mark_event_locked_ram(self, event_id: str) -> None:
        current = self._hot_state
        locked = set(current.get("locked_event_ids") or set())
        locked.add(str(event_id))

        self._hot_state = {
            **current,
            "locked_event_ids": locked,
        }

    def _mark_event_unlocked_ram(self, event_id: str) -> None:
        current = self._hot_state
        locked = set(current.get("locked_event_ids") or set())
        locked.discard(str(event_id))
        self._hot_state = {
            **current,
            "locked_event_ids": locked,
        }

    def _durable_entry_gate(
        self,
        *,
        check_pause: bool,
        require_canary: bool,
    ) -> tuple[bool, str]:
        """Durable safety gate used only on an actual entry attempt.

        SQLite is intentionally retained here. This function is not called
        for ordinary market frames.
        """
        if self.base.kill_switch_active():
            return False, "KILL_SWITCH_ACTIVE"

        if check_pause and self.repo.pause_entries():
            return False, "PAUSE_ENTRIES"

        if (
            require_canary
            and self.base.get_state(
                "canary_armed",
                "false",
            ).lower() != "true"
        ):
            return False, "CANARY_NOT_ARMED"

        return True, "READY"

    def _freshness(
        self, condition_id: str, update: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._market_freshness is None:
            if self.paper_mode():
                return {"ready": True, "reason": "PAPER_PROVIDER_NOT_CONFIGURED"}
            return {"ready": False, "reason": "FRESHNESS_PROVIDER_UNAVAILABLE"}
        result = self._market_freshness(condition_id)
        if not result.get("ready"):
            return result
        if update is not None:
            token_id = str(update.get("asset_id") or "")

            if update.get("_critical_trigger_latched"):
                # A newer market frame must not erase the historical fact that
                # best ask was exactly 0.74. Instead of requiring the latched
                # frame to still be the latest book version, enforce a strict
                # age bound on the trigger itself.
                exchange_timestamp_ms = update.get("exchange_timestamp_ms")
                try:
                    trigger_ms = int(exchange_timestamp_ms)
                except (TypeError, ValueError):
                    return {
                        **result,
                        "ready": False,
                        "reason": "MISSING_CRITICAL_TRIGGER_TIMESTAMP",
                    }

                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                trigger_age_ms = now_ms - trigger_ms
                max_age_ms = int(self.config.max_market_data_age_seconds * 1000)

                if trigger_age_ms < -1000:
                    return {
                        **result,
                        "ready": False,
                        "reason": "FUTURE_CRITICAL_TRIGGER_TIMESTAMP",
                        "critical_trigger_age_ms": trigger_age_ms,
                    }

                if trigger_age_ms > max_age_ms:
                    return {
                        **result,
                        "ready": False,
                        "reason": "CRITICAL_TRIGGER_EXPIRED",
                        "critical_trigger_age_ms": trigger_age_ms,
                    }

                return {
                    **result,
                    "critical_trigger_latched": True,
                    "critical_trigger_age_ms": trigger_age_ms,
                }

            expected_generation = update.get("generation")
            expected_number = update.get("update_number")
            version = (result.get("book_versions") or {}).get(token_id) or {}
            if (
                expected_generation is not None
                and int(version.get("generation", -1)) != int(expected_generation)
            ) or (
                expected_number is not None
                and int(version.get("update_number", -1)) != int(expected_number)
            ):
                return {**result, "ready": False, "reason": "FRAME_SUPERSEDED"}
        return result

    def _record_freshness_block(
        self, *, market: dict[str, Any], reason: str, intent_id: str | None = None,
        phase: str, details: dict[str, Any] | None = None,
    ) -> None:
        self.repo.timeline(
            severity="WARNING", category="DECISION", component="strategy",
            source="market_ws", event_id=str(market.get("event_id") or ""),
            condition_id=str(market.get("condition_id") or ""),
            intent_id=intent_id, requested_action="ENTRY",
            reason_code=reason, result_status="SKIPPED",
            parameters_json={"freshness_phase": phase, **(details or {})},
        )

    def enabled(self) -> bool:
        return self.config.live_module_enabled and self.config.execution_mode in {
            "PAPER_TRADING", "REAL_TRADING"
        }

    def paper_mode(self) -> bool:
        return self.config.execution_mode == "PAPER_TRADING"

    def _trace_critical(
        self,
        stage: str,
        *,
        update: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        reason: str = "",
        result: str = "",
        intent_id: str = "",
    ) -> None:
        item = update or {}
        frame = context or {}
        getattr(self, "_logger", logging.getLogger(__name__)).warning(
            "CRITICAL_TRIGGER_LIFECYCLE stage=%s correlation_id=%s "
            "event=%s condition=%s token=%s types=%s reason=%s result=%s "
            "intent_id=%s message_hash=%s",
            stage,
            item.get("_critical_trigger_id")
            or frame.get("_critical_trigger_id")
            or "",
            item.get("event_id") or "",
            item.get("condition_id") or "",
            item.get("asset_id") or "",
            frame.get("_critical_trigger_types") or [],
            reason,
            result,
            intent_id,
            frame.get("message_hash") or item.get("message_hash") or "",
        )

    def schedule_frame(self, context: dict[str, Any]) -> None:
        if not hasattr(self, "_critical_observed_price_state"):
            # Compatibility for focused unit fixtures constructed via __new__.
            self._critical_observed_price_state = {}
        final_updates = [
            item
            for item in context.get("updates") or []
            if isinstance(item, dict)
        ]

        transition_updates = [
            item
            for item in context.get("top_transitions") or []
            if isinstance(item, dict)
        ]

        # Observability must see historical exact-entry edges as well, even
        # when the final atomic book already moved away from 0.74.
        observed_context = {
            **context,
            "updates": [
                *transition_updates,
                *final_updates,
            ],
        }
        self._observe_entry_trigger(
            observed_context
        )

        if not self.enabled():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(
                self.process_atomic_frame(
                    context
                )
            )
            return

        if (
            str(context.get("event_type") or "")
            == "market_resolved"
        ):
            loop.create_task(
                self.process_atomic_frame(
                    context
                )
            )
            return

        if (
            self._frame_task is None
            or self._frame_task.done()
        ):
            self._frame_task = loop.create_task(
                self._frame_worker(),
                name="strategy-frame-worker",
            )

        grouped_final: dict[
            str,
            list[dict[str, Any]],
        ] = {}

        grouped_observed: dict[
            str,
            list[dict[str, Any]],
        ] = {}

        condition_order: list[str] = []

        for update in final_updates:
            condition_id = str(
                update.get("condition_id") or ""
            )

            if not condition_id:
                continue

            grouped_final.setdefault(
                condition_id,
                [],
            ).append(update)

        # Intermediate transitions MUST be examined before the final state.
        # They already carry raw-message ordering from OrderBookSet.
        for update in [
            *transition_updates,
            *final_updates,
        ]:
            condition_id = str(
                update.get("condition_id") or ""
            )

            if not condition_id:
                continue

            if condition_id not in grouped_observed:
                condition_order.append(
                    condition_id
                )

            grouped_observed.setdefault(
                condition_id,
                [],
            ).append(update)

        for condition_id in condition_order:
            updates = grouped_final.get(
                condition_id,
                [],
            )

            observations = grouped_observed.get(
                condition_id,
                [],
            )

            readiness = (
                (
                    context.get(
                        "event_readiness"
                    )
                    or {}
                ).get(
                    condition_id,
                    {
                        "ready": False,
                        "reason": "NOT_READY",
                    },
                )
            )

            latched_updates: list[
                dict[str, Any]
            ] = []

            critical_types: set[str] = set()

            for update in observations:
                item = dict(update)

                asset_id = str(
                    item.get("asset_id")
                    or ""
                )

                state_key = (
                    condition_id,
                    asset_id,
                )

                readiness_ready = bool(readiness.get("ready"))
                state_store = (
                    self._critical_price_state
                    if readiness_ready
                    else self._critical_observed_price_state
                )
                previous = (
                    state_store.get(
                        state_key,
                        {
                            "best_ask": None,
                            "best_bid": None,
                        },
                    )
                )

                ask = item.get(
                    "best_ask"
                )
                bid = item.get(
                    "best_bid"
                )

                entry_now = exact_trigger(
                    ask,
                    self.policy.entry_price,
                )
                entry_before = exact_trigger(
                    previous.get("best_ask"),
                    self.policy.entry_price,
                )

                bid_value = decimal_value(bid)
                previous_bid = decimal_value(
                    previous.get("best_bid")
                )

                stop_now = (
                    bid_value is not None
                    and bid_value <= self.policy.stop_price
                )
                stop_before = (
                    previous_bid is not None
                    and previous_bid <= self.policy.stop_price
                )

                # Always deduplicate observed transitions. Only a READY state
                # advances the actionable edge state; NOT_READY 0.74/0.66
                # therefore remains blocked without disappearing after resync.
                next_state = {
                    "best_ask": ask,
                    "best_bid": bid,
                }
                self._critical_observed_price_state[state_key] = next_state
                if readiness_ready:
                    self._critical_price_state[state_key] = next_state

                latched = False

                if (
                    entry_now
                    and not entry_before
                ):
                    item[
                        "_critical_entry_latched"
                    ] = True
                    critical_types.add(
                        "ENTRY_074"
                    )
                    latched = True

                if (
                    stop_now
                    and not stop_before
                ):
                    item[
                        "_critical_stop_latched"
                    ] = True
                    critical_types.add(
                        "STOP_066"
                    )
                    latched = True

                if latched:
                    item[
                        "_critical_trigger_latched"
                    ] = True
                    item["_critical_trigger_id"] = (
                        str(item.get("correlation_id") or "")
                        or stable_id(
                            "critical-trigger",
                            ":".join((
                                condition_id, asset_id,
                                str(item.get("exchange_timestamp_ms") or ""),
                                str(item.get("_raw_change_index") or ""),
                                ",".join(sorted(critical_types)),
                            )),
                        )
                    )
                    self._trace_critical(
                        "TRIGGER_DETECTED", update=item, context=context
                    )
                    self._trace_critical(
                        "TRIGGER_LATCHED", update=item, context=context
                    )
                    latched_updates.append(
                        item
                    )

            if critical_types:
                # Critical historical states must come first. The only
                # strategy loss-exit edge is the latched STOP_066.
                # Append the latest atomic frame afterwards for other-side/
                # event context.
                def signature(
                    item: dict[str, Any],
                ) -> tuple[str, str, str, str]:
                    return (
                        str(
                            item.get(
                                "asset_id"
                            )
                            or ""
                        ),
                        str(
                            item.get(
                                "best_bid"
                            )
                        ),
                        str(
                            item.get(
                                "best_ask"
                            )
                        ),
                        str(
                            item.get(
                                "exchange_timestamp_ms"
                            )
                        ),
                    )

                latched_signatures = {
                    signature(item)
                    for item in latched_updates
                }

                context_updates = [
                    *latched_updates,
                    *[
                        dict(item)
                        for item in updates
                        if signature(item)
                        not in latched_signatures
                    ],
                ]

                critical = {
                    **context,
                    "_critical_trigger": True,
                    "_critical_trigger_types": (
                        sorted(
                            critical_types
                        )
                    ),
                    "updates": context_updates,
                    "_critical_trigger_id": (
                        latched_updates[0].get("_critical_trigger_id")
                        if latched_updates else ""
                    ),
                    "event_readiness": {
                        condition_id: readiness
                    },
                }

                # Critical edges are strict FIFO and never dropped because
                # of normal latest-state queue pressure.
                self._critical_frames.append(
                    critical
                )

                self.critical_triggers_queued += 1
                self._trace_critical(
                    "CRITICAL_FRAME_QUEUED",
                    update=latched_updates[0] if latched_updates else None,
                    context=critical, result="QUEUED",
                )

                self.max_critical_queue_depth = max(
                    self.max_critical_queue_depth,
                    len(
                        self._critical_frames
                    ),
                )

                continue

            if not updates:
                continue

            isolated = {
                **context,
                "updates": updates,
                "event_readiness": {
                    condition_id: readiness
                },
            }

            # Ordinary latest-state traffic remains intentionally conflatable.
            if (
                condition_id
                in self._pending_frames
            ):
                self._pending_frames.pop(
                    condition_id
                )
                self.frames_coalesced += 1

            elif (
                len(self._pending_frames)
                >= self._frame_queue_capacity
            ):
                self._pending_frames.popitem(
                    last=False
                )
                self.frames_dropped += 1

            self._pending_frames[
                condition_id
            ] = isolated

        if grouped_observed:
            self._frame_event.set()

    def _observe_entry_trigger(self, context: dict[str, Any]) -> None:
        """Log the 0.74 decision path, including while execution is READ_ONLY."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for update in context.get("updates") or []:
            if isinstance(update, dict) and update.get("condition_id"):
                grouped.setdefault(str(update["condition_id"]), []).append(update)
        for condition_id, updates in grouped.items():
            triggers = [
                update for update in updates
                if exact_trigger(update.get("best_ask"), self.policy.entry_price)
            ]
            if not triggers:
                continue

            market = self._market(condition_id)
            if not market:
                continue
            event_id = str(market.get("event_id") or "")
            readiness = (context.get("event_readiness") or {}).get(condition_id) or {}
            active_tokens = {str(update.get("asset_id") or "") for update in triggers}
            for key in [key for key in self._entry_trigger_log_state if key[0] == event_id]:
                if key[1] not in active_tokens:
                    self._entry_trigger_log_state.pop(key, None)
            if not triggers:
                continue
            schedule = self.entry_schedule_status()
            ram_state = self._entry_state_from_ram(event_id)

            paused = (
                ram_state["paused"]
                or not schedule["allowed"]
                or (
                    self.config.execution_mode == "REAL_TRADING"
                    and not self.config.continuous_trading_enabled
                    and not ram_state["canary_armed"]
                )
            )

            decision = choose_entry(
                updates=updates,
                yes_token_id=str(market.get("yes_token_id") or ""),
                no_token_id=str(market.get("no_token_id") or ""),
                event_ready=bool(readiness.get("ready")),
                paused=paused,
                event_locked=bool(ram_state["event_locked"]),
                active_exposure=ram_state["active_exposure"],
                observed_at=datetime.now(timezone.utc),
                event_id=event_id,
                policy=self.policy,
            )
            reason = decision.reason
            if reason == "PAUSE_ENTRIES" and not schedule["allowed"]:
                reason = str(schedule["reason"])
            elif reason == "MARKET_DATA_NOT_READY":
                reason = str(readiness.get("reason") or reason)
            outcome = "WOULD_ENTER" if decision.allowed else "BLOCKED"
            for update in triggers:
                token_id = str(update.get("asset_id") or "")
                state = f"{outcome}:{reason}"
                key = (event_id, token_id)
                if self._entry_trigger_log_state.get(key) == state:
                    continue
                self._entry_trigger_log_state[key] = state
                self._logger.warning(
                    "ENTRY_074 event=%s outcome=%s reason=%s side=%s readiness=%s",
                    event_id, outcome, reason, update.get("outcome"),
                    readiness.get("reason") or "NOT_READY",
                )

    async def _frame_worker(self) -> None:
        while (
            not self._stop.is_set()
            or self._critical_frames
            or self._pending_frames
        ):
            if not self._critical_frames and not self._pending_frames:
                self._frame_event.clear()
                try:
                    await asyncio.wait_for(self._frame_event.wait(), 0.25)
                except asyncio.TimeoutError:
                    continue

            await asyncio.sleep(0)

            if self._critical_frames:
                context = self._critical_frames.popleft()
                self.critical_triggers_processed += 1
                self._trace_critical(
                    "CRITICAL_FRAME_PROCESSING",
                    update=next((
                        item for item in context.get("updates") or []
                        if isinstance(item, dict)
                        and item.get("_critical_trigger_latched")
                    ), None),
                    context=context, result="PROCESSING",
                )
            elif self._pending_frames:
                _condition_id, context = self._pending_frames.popitem(
                    last=False
                )
            else:
                continue

            timing = context.get("_latency_timing")
            if isinstance(timing, dict):
                strategy_started = time.perf_counter()
                timing["strategy_check_monotonic"] = strategy_started
                scheduled = timing.get("strategy_scheduled_monotonic")
                if scheduled is not None:
                    timing["strategy_queue_delay_ms"] = (
                        strategy_started - scheduled
                    ) * 1000

            await self.process_atomic_frame(context)

            if isinstance(timing, dict):
                timing["strategy_finished_monotonic"] = time.perf_counter()


    async def process_atomic_frame(self, context: dict[str, Any]) -> None:
        self.frames_processed += 1
        updates = [item for item in context.get("updates") or [] if isinstance(item, dict)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for update in updates:
            condition_id = str(update.get("condition_id") or "")
            if condition_id:
                grouped.setdefault(condition_id, []).append(update)
        for condition_id, condition_updates in grouped.items():
            market = self._market(condition_id)
            if not market:
                continue
            event_id = str(market.get("event_id") or "")
            lock = self._event_locks.setdefault(event_id, asyncio.Lock())
            async with lock:
                try:
                    await self._process_event(
                        market=market,
                        updates=condition_updates,
                        event_ready=bool(
                            (context.get("event_readiness") or {}).get(condition_id, {}).get("ready")
                        ),
                        readiness_reason=str(
                            (context.get("event_readiness") or {}).get(condition_id, {}).get("reason")
                            or "NOT_READY"
                        ),
                        received_at=str(context.get("received_at") or now_iso()),
                        frame_hash=str(context.get("message_hash") or ""),
                    )
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                    self.repo.alert(
                        alert_type="STRATEGY_RUNTIME",
                        severity="CRITICAL",
                        reason_code="STRATEGY_PROCESSING_FAILED",
                        message=self.last_error,
                        entity_type="event",
                        entity_id=event_id,
                    )
                    self.repo.timeline(
                        severity="CRITICAL", category="ERROR", component="strategy",
                        source="market_ws", event_id=event_id, condition_id=condition_id,
                        requested_action="PROCESS_FRAME", reason_code="PROCESSING_FAILURE",
                        result_status="FAILED", error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )

    async def _process_event(
        self,
        *,
        market: dict[str, Any],
        updates: list[dict[str, Any]],
        event_ready: bool,
        readiness_reason: str,
        received_at: str,
        frame_hash: str,
    ) -> None:
        event_id = str(market.get("event_id") or "")
        condition_id = str(market.get("condition_id") or "")
        yes_token = str(market.get("yes_token_id") or "")
        no_token = str(market.get("no_token_id") or "")
        if not self._eligible_market(market):
            if any(exact_trigger(update.get("best_ask"), self.policy.entry_price) for update in updates):
                self.repo.timeline(
                    severity="WARNING", category="DECISION", component="strategy",
                    source="market_ws", event_id=event_id, condition_id=condition_id,
                    requested_action="ENTRY", reason_code="MARKET_SCOPE_MISMATCH",
                    result_status="SKIPPED",
                )
            return

        for update in updates:
            await self._manage_position(
                market=market, update=update, event_ready=event_ready, frame_hash=frame_hash
            )

        if market.get("market_resolved"):
            await self._handle_resolution(market)
            return

        try:
            observed_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError:
            observed_at = datetime.now(timezone.utc)
        has_trigger = any(
            exact_trigger(
                update.get("best_ask"),
                self.policy.entry_price,
            )
            for update in updates
        )

        # Daily-loss persistence is entry-path work, not market-frame work.
        daily_loss_blocked = (
            self._daily_loss_blocked()
            if has_trigger
            else False
        )

        schedule = self.entry_schedule_status()
        ram_state = self._entry_state_from_ram(event_id)

        decision = choose_entry(
            updates=updates,
            yes_token_id=yes_token,
            no_token_id=no_token,
            event_ready=event_ready,
            paused=(
                ram_state["paused"]
                or daily_loss_blocked
                or not schedule["allowed"]
                or (
                    not self.paper_mode()
                    and not self.config.continuous_trading_enabled
                    and not ram_state["canary_armed"]
                )
            ),
            event_locked=bool(ram_state["event_locked"]),
            active_exposure=ram_state["active_exposure"],
            observed_at=observed_at,
            event_id=event_id,
            policy=self.policy,
        )
        trigger_update = next((
            item for item in updates
            if item.get("_critical_entry_latched")
        ), None)
        if trigger_update is not None:
            self._trace_critical(
                "ENTRY_DECISION", update=trigger_update,
                reason=decision.reason,
                result="ALLOWED" if decision.allowed else "BLOCKED",
            )
        if not decision.allowed:
            if decision.simultaneous:
                self.repo.reserve_event_entry(
                    event_id=event_id, condition_id=condition_id, token_id=None,
                    side=None, simultaneous=True, reason_code=decision.reason,
                )
            if has_trigger or decision.simultaneous:
                self.repo.timeline(
                    severity="WARNING" if decision.reason != "EVENT_LOCKED" else "INFO",
                    category="DECISION", component="strategy", source="market_ws",
                    event_id=event_id, condition_id=condition_id,
                    requested_action="ENTRY", reason_code=(
                        schedule["reason"] if decision.reason == "PAUSE_ENTRIES" and not schedule["allowed"]
                        else readiness_reason if decision.reason == "MARKET_DATA_NOT_READY"
                        else decision.reason
                    ),
                    result_status="SKIPPED",
                    parameters_json={
                        "event_ready": event_ready,
                        "frame_hash": frame_hash,
                        "asks": {
                            str(update.get("outcome")): update.get("best_ask")
                            for update in updates
                        },
                    },
                )
                if trigger_update is not None:
                    self._trace_critical(
                        "TERMINAL_RESULT", update=trigger_update,
                        reason=(
                            schedule["reason"]
                            if decision.reason == "PAUSE_ENTRIES" and not schedule["allowed"]
                            else readiness_reason
                            if decision.reason == "MARKET_DATA_NOT_READY"
                            else decision.reason
                        ),
                        result="SKIPPED",
                    )
            return

        min_order = decimal_value(market.get("min_order_size"))
        tick = decimal_value(market.get("min_tick_size"))
        fee_rate = self._fee_rate(market)
        if (
            min_order is None or min_order <= 0 or tick is None or tick <= 0
            or (self.policy.entry_price / tick) % 1 != 0
            or (self.policy.entry_max_price / tick) % 1 != 0
        ):
            self.repo.lock_event_skip(
                event_id=event_id, condition_id=condition_id,
                reason_code="MISSING_DYNAMIC_MARKET_CONSTRAINTS",
            )
            return
        viable, viability_reason = AllInBudget(self.policy.max_spend, self.policy.max_shares).minimum_viable(
            min_order_shares=min_order,
            maximum_price=self.policy.entry_max_price,
            maximum_fee_fraction=fee_rate,
        )
        if not viable:
            self.repo.lock_event_skip(
                event_id=event_id, condition_id=condition_id,
                reason_code=viability_reason,
            )
            self.repo.timeline(
                severity="WARNING", category="DECISION", component="strategy",
                source="market_ws", event_id=event_id, condition_id=condition_id,
                token_id=decision.token_id, side=decision.side,
                requested_action="ENTRY", reason_code=viability_reason,
                result_status="SKIPPED",
                parameters_json={
                    "min_order_shares": canonical_decimal(min_order),
                    "tick_size": canonical_decimal(tick),
                    "max_spend": canonical_decimal(self.policy.max_spend),
                    "fee_rate": canonical_decimal(fee_rate),
                },
            )
            return
        selected_update = next(
            item for item in updates if str(item.get("asset_id")) == decision.token_id
        )
        freshness = self._freshness(condition_id, selected_update)
        if trigger_update is not None:
            self._trace_critical(
                "PRE_INTENT_GATE", update=trigger_update,
                reason=str(freshness.get("reason") or ""),
                result="PASSED" if freshness.get("ready") else "BLOCKED",
            )
        if not freshness.get("ready"):
            self._record_freshness_block(
                market=market, reason=str(freshness.get("reason") or "FRESHNESS_FAILED"),
                phase="PRE_INTENT", details=freshness,
            )
            return
        require_canary = (
            not self.paper_mode()
            and not self.config.continuous_trading_enabled
        )

        if not self.paper_mode():
            durable_ready, durable_reason = self._durable_entry_gate(
                check_pause=True,
                require_canary=require_canary,
            )

            if not durable_ready:
                self.repo.timeline(
                    severity="WARNING",
                    category="DECISION",
                    component="strategy",
                    source="durable_safety_gate",
                    event_id=event_id,
                    condition_id=condition_id,
                    token_id=decision.token_id,
                    side=decision.side,
                    requested_action="ENTRY",
                    reason_code=durable_reason,
                    result_status="SKIPPED",
                )
                return

        reservation = self.repo.reserve_event_entry(
            event_id=event_id, condition_id=condition_id,
            token_id=decision.token_id, side=decision.side,
            simultaneous=False, reason_code=decision.reason,
            consume_canary=(not self.paper_mode() and not self.config.continuous_trading_enabled),
            require_empty_slot=(not self.paper_mode() and self.config.continuous_trading_enabled),
        )
        if reservation.get("_blocked"):
            reservation_reason = str(
                reservation.get("reason") or "ENTRY_RESERVATION_BLOCKED"
            )
            if trigger_update is not None:
                self._trace_critical(
                    "ENTRY_RESERVATION", update=trigger_update,
                    reason=reservation_reason, result="BLOCKED",
                )
                self._trace_critical(
                    "TERMINAL_RESULT", update=trigger_update,
                    reason=reservation_reason, result="SKIPPED",
                )
            self.repo.timeline(
                severity="WARNING", category="DECISION", component="strategy",
                source="entry_slot_gate", event_id=event_id,
                condition_id=condition_id, token_id=decision.token_id,
                side=decision.side, requested_action="ENTRY",
                reason_code=reservation_reason, result_status="SKIPPED",
                parameters_json={
                    key: reservation.get(key)
                    for key in (
                        "blocker_kind", "blocking_intent_id",
                        "blocking_position_id", "blocking_event_id",
                        "blocking_state", "blocking_closed_at",
                        "blocking_remaining_shares_text",
                        "blocking_sellable_shares_text",
                    )
                },
            )
            return
        if reservation.get("_duplicate"):
            if trigger_update is not None:
                self._trace_critical(
                    "ENTRY_RESERVATION", update=trigger_update,
                    reason="EVENT_ENTRY_ALREADY_RESERVED", result="DUPLICATE",
                )
            return

        self._mark_event_locked_ram(event_id)

        intent_id = str(reservation["entry_intent_id"])
        if trigger_update is not None:
            self._trace_critical(
                "ENTRY_INTENT_RESERVED", update=trigger_update,
                reason="ENTRY_PRICE_EXACT", result="RESERVED",
                intent_id=intent_id,
            )
        self.repo.timeline(
            severity="INFO", category="ORDER", component="strategy",
            source="market_ws", event_id=event_id, condition_id=condition_id,
            token_id=decision.token_id, side=decision.side,
            deal_id=stable_id("deal", event_id),
            correlation_id=stable_id("correlation", event_id),
            intent_id=intent_id, requested_action="BUY_MARKET_FAK",
            reason_code="ENTRY_PRICE_EXACT", previous_state="ELIGIBLE",
            new_state="ENTRY_INTENT_RESERVED", result_status="RESERVED",
            requested_amount_text="3.8", requested_shares_text="5", parameters_json={
                "max_price": "0.76", "max_spend": "5", "max_tokens": "5", "all_in": True,
                "frame_hash": frame_hash,
            },
        )
        await self._submit_entry(
            market=market, update=selected_update, side=str(decision.side),
            intent_id=intent_id, fee_rate=fee_rate,
        )

    def _daily_loss_blocked(self) -> bool:
        try:
            tz = ZoneInfo("Asia/Jerusalem")
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        day_key = datetime.now(tz).date().isoformat()
        daily = self.base.current_daily_limit(day_key, "Asia/Jerusalem")
        realized = decimal_value(daily.get("realized_pnl_usd")) or Decimal("0")
        blocked = realized <= -abs(self.config.max_daily_realized_loss_usd)
        if blocked:
            self.base.set_state("canary_armed", "false", "strategy_daily_loss")
            self.repo.set_pause_entries(
                True, "strategy_daily_loss", "DAILY_LOSS_LIMIT",
                owner="MACHINE", auto_recoverable=False,
            )
            self.repo.alert(
                alert_type="RISK",
                severity="CRITICAL",
                reason_code="DAILY_LOSS_LIMIT",
                message="Daily realized loss limit reached; LIVE entries locked",
            )
        return blocked

    def _eligible_market(self, market: dict[str, Any]) -> bool:
        event_id = str(market.get("event_id") or "")
        if not event_id.startswith("btc-updown-5m-"):
            return False
        if market.get("token_mapping_status") not in {"verified", "matched"}:
            return False
        if not market.get("yes_token_id") or not market.get("no_token_id"):
            return False
        try:
            raw = json.loads(market.get("raw_market_info") or "{}")
        except (TypeError, ValueError):
            raw = {}
        scope_verified = raw.get("scope_verified")
        return scope_verified is True or raw.get("slug") == event_id

    @staticmethod
    def _fee_rate(market: dict[str, Any]) -> Decimal:
        direct = decimal_value(market.get("taker_base_fee"))
        if direct is not None and direct >= 0:
            return direct
        try:
            details = json.loads(market.get("fee_details") or "{}")
        except (TypeError, ValueError):
            details = {}
        return decimal_value(details.get("rate") or details.get("r")) or Decimal("0")

    async def _submit_entry(
        self,
        *,
        market: dict[str, Any],
        update: dict[str, Any],
        side: str,
        intent_id: str,
        fee_rate: Decimal,
    ) -> None:
        event_id = str(market["event_id"])
        freshness = self._freshness(str(market["condition_id"]), update)
        if not freshness.get("ready"):
            reason = str(freshness.get("reason") or "FRESHNESS_FAILED")
            self.repo.update_intent(
                intent_id, state="REJECTED", reason_code=reason,
                normalized_error="Blocked by exchange timestamp freshness before submission",
                final_at=now_iso(),
            )
            self._record_freshness_block(
                market=market, reason=reason, intent_id=intent_id,
                phase="PRE_SUBMISSION", details=freshness,
            )
            if not self.paper_mode():
                self.repo.set_pause_entries(
                    True, "strategy", reason, owner="MACHINE", auto_recoverable=True
                )
                self.base.set_states({
                    "canary_armed": "false",
                    "strategy_readiness": "NOT_READY",
                    "strategy_block_reason": reason,
                }, "strategy")
            return
        schedule = self.entry_schedule_status()
        if not schedule["allowed"]:
            reason = str(schedule["reason"])
            self.repo.update_intent(
                intent_id, state="REJECTED", reason_code=reason,
                normalized_error="Blocked by entry schedule before submission",
                final_at=now_iso(),
            )
            self.repo.timeline(
                severity="WARNING", category="DECISION", component="strategy",
                source="schedule", event_id=str(market.get("event_id") or ""),
                condition_id=str(market.get("condition_id") or ""), intent_id=intent_id,
                requested_action="ENTRY", reason_code=reason, result_status="SKIPPED",
                parameters_json=schedule,
            )
            return
        if self.paper_mode():
            fill = simulate_buy_fak(
                update.get("asks") or [],
                max_price=self.policy.entry_max_price,
                max_spend=self.policy.max_spend,
                fee_rate=fee_rate,
                max_shares=self.policy.max_shares,
            )
            if fill.filled_shares <= 0:
                self.repo.mark_zero_fill(
                    event_id, "FAK_ZERO_FILL", intent_id=intent_id
                )
                self._mark_event_unlocked_ram(event_id)
                self.repo.timeline(
                    severity="WARNING", category="FILL", component="paper_strategy",
                    source="deterministic_book", event_id=event_id,
                    condition_id=market["condition_id"], token_id=update["asset_id"],
                    side=side, intent_id=intent_id, requested_action="BUY_MARKET_FAK",
                    reason_code="FAK_ZERO_FILL", result_status="ZERO_FILL",
                    requested_amount_text="3.8", requested_shares_text="5", filled_shares_text="0",
                    remaining_shares_text="0",
                )
                return
            self.repo.add_fill(
                intent_id=intent_id, remote_trade_id=f"paper-{intent_id}-fill-1",
                shares=fill.filled_shares, price=fill.average_price, fee=fill.fee,
                status="SETTLED", matched_at=now_iso(),
                raw={"source": "deterministic_order_book"},
            )
            position = self.repo.open_position(
                event_id=event_id, condition_id=str(market["condition_id"]),
                token_id=str(update["asset_id"]), outcome=side,
                shares=fill.filled_shares, average_price=fill.average_price,
                cost_all_in=fill.all_in, fees=fill.fee,
                min_sellable=self._min_order(str(market["condition_id"])),
                entry_intent_id=intent_id,
            )
            self.repo.timeline(
                severity="INFO", category="FILL", component="paper_strategy",
                source="deterministic_book", event_id=event_id,
                condition_id=market["condition_id"], token_id=update["asset_id"],
                side=side, deal_id=stable_id("deal", event_id), intent_id=intent_id,
                fill_id=f"paper-{intent_id}-fill-1", requested_action="BUY_MARKET_FAK",
                reason_code="PAPER_BOOK_FILL", previous_state="RESERVED",
                new_state="POSITION_OPEN", result_status=(
                    "PARTIAL" if fill.remaining_request > 0 else "FILLED"
                ),
                requested_amount_text="3.8", requested_shares_text="5",
                filled_shares_text=canonical_decimal(fill.filled_shares),
                average_price_text=canonical_decimal(fill.average_price),
                fees_text=canonical_decimal(fill.fee),
                remaining_shares_text=canonical_decimal(fill.remaining_request),
            )
            # Make the newly opened position visible in RAM before any
            # queued STOP/EMERGENCY critical frame can be processed.
            await self._refresh_hot_state_once()

            await self._ensure_take_profit(position)

            # TP creation changes position + intent state as well.
            await self._refresh_hot_state_once()
            return

        durable_ready, durable_reason = self._durable_entry_gate(
            check_pause=self.config.continuous_trading_enabled,
            require_canary=False,
        )

        if not durable_ready:
            self.repo.update_intent(
                intent_id,
                state="REJECTED",
                reason_code=durable_reason,
                normalized_error=(
                    "Blocked by durable safety gate before order submission"
                ),
                final_at=now_iso(),
            )
            self.repo.timeline(
                severity="WARNING",
                category="DECISION",
                component="strategy",
                source="durable_safety_gate",
                event_id=event_id,
                condition_id=str(market["condition_id"]),
                token_id=str(update["asset_id"]),
                side=side,
                intent_id=intent_id,
                requested_action="ENTRY",
                reason_code=durable_reason,
                result_status="SKIPPED",
            )
            return

        self.repo.update_intent(
            intent_id,
            state="SUBMITTING",
            submitted_at=now_iso(),
        )
        self._trace_critical(
            "ORDER_SUBMIT_ATTEMPT", update=update,
            reason="ENTRY_PRICE_EXACT", result="SUBMITTING",
            intent_id=intent_id,
        )
        entry_params = AllInBudget(
            self.policy.max_spend, self.policy.max_shares
        ).sdk_buy_parameters(self.policy.entry_max_price)
        response = await self.adapter.create_order({
            "idempotency_key": intent_id,
            "durable_intent_reserved": True,
            "event_id": event_id,
            "condition_id": market["condition_id"],
            "token_id": update["asset_id"],
            "outcome": side,
            "side": "BUY",
            "order_type": "FAK",
            "purpose": "ENTRY",
            "deal_id": stable_id("deal", event_id),
            "requested_amount_usd": entry_params["amount"],
            "max_spend": entry_params["max_spend"],
            "max_tokens": canonical_decimal(self.policy.max_shares),
            "max_price": "0.76",
        })
        status = str(response.get("status") or "unknown").lower()
        remote_id = response.get("polymarket_order_id")
        self._trace_critical(
            "TERMINAL_RESULT"
            if status in {"rejected", "failed", "blocked"}
            else "ORDER_SUBMIT_RESULT",
            update=update,
            reason=str(response.get("failure_reason") or status),
            result=status.upper(), intent_id=intent_id,
        )
        if status == "rejected" and _is_confirmed_fak_zero_fill_response(response):
            self.repo.mark_zero_fill(
                event_id, "FAK_ZERO_FILL", intent_id=intent_id
            )
            self._mark_event_unlocked_ram(event_id)
        else:
            self.repo.update_intent(
                intent_id, state=(
                    "RECONCILIATION_REQUIRED"
                    if status in {"matched", "delayed", "unknown", "live"}
                    else "REJECTED"
                ),
                remote_order_id=remote_id,
                reason_code=str(response.get("failure_reason") or status).upper(),
                normalized_error=response.get("message"),
            )
        reconciled = await self._reconcile("entry_submission")

        if reconciled.get("status") == "ok":
            recovered = self.repo.position_for_token(
                str(update["asset_id"])
            )

            if recovered:
                # Position recovery is durable action-path work. Publish it
                # into RAM before creating/observing its TP.
                await self._refresh_hot_state_once()

                recovered = (
                    self._position_from_ram(
                        str(update["asset_id"]),
                        str(recovered["position_id"]),
                    )
                    or recovered
                )

                await self._ensure_take_profit(
                    recovered
                )

                await self._refresh_hot_state_once()

    @staticmethod
    def _is_waiting_sellable_response(
        response: dict[str, Any],
    ) -> bool:
        """True only when SELL definitely never reached post_order due balance."""
        if response.get("polymarket_order_id"):
            return False

        submission_state = str(
            response.get("submission_state") or ""
        ).upper()

        reason = str(
            response.get("failure_reason") or ""
        ).upper()

        return (
            submission_state == "NOT_SUBMITTED"
            and "INSUFFICIENT_BALANCE" in reason
        )

    def _finalize_known_no_remote_submission(
        self,
        intent: dict[str, Any],
        response: dict[str, Any],
        *,
        fak: bool = False,
    ) -> bool:
        """Finalize only outcomes proven to have no live remote order.

        UNKNOWN_AFTER_SUBMISSION deliberately remains unresolved/fail-closed.
        """
        if response.get("polymarket_order_id"):
            return False

        submission_state = str(
            response.get("submission_state") or ""
        ).upper()

        status = str(
            response.get("status") or "unknown"
        ).upper()

        reason = str(
            response.get("failure_reason") or status
        )

        if fak and reason == "FAK_NOT_FILLED":
            final_state = "ZERO_FILL"

        elif submission_state == "NOT_SUBMITTED":
            final_state = "FAILED"

        elif (
            submission_state
            in {"RESPONSE_RECEIVED", "CONFIRMED_TERMINAL"}
            and status in {"REJECTED", "FAILED", "BLOCKED"}
        ):
            final_state = (
                "REJECTED"
                if status == "REJECTED"
                else "FAILED"
            )

        else:
            return False

        self.repo.finalize_position_intent_failure(
            str(intent["intent_id"]),
            state=final_state,
            reason=reason,
            normalized_error=response.get("message"),
        )

        return True

    def _clear_local_waiting_intent(
        self,
        intent_id: str,
        *,
        reason: str,
    ) -> bool:
        """Cancel a WAITING_SELLABLE intent that never reached Polymarket."""
        intent = self.repo.intent(str(intent_id))

        if not intent:
            return False

        if (
            str(intent.get("state") or "").upper()
            != "WAITING_SELLABLE"
        ):
            return False

        if intent.get("remote_order_id"):
            # Defensive invariant: WAITING_SELLABLE must be local-only.
            return False

        self.repo.finalize_cancel(
            str(intent_id),
            True,
            reason,
        )

        return True

    async def _resume_waiting_sellable_intent(
        self,
        position: dict[str, Any],
        update: dict[str, Any],
        *,
        bid: Decimal,
        frame_hash: str,
        reconciliation_ready: bool,
    ) -> bool:
        """Resume a local-only SELL trigger once remote sellability is confirmed.

        Priority rules:
        - a latched STOP always resumes as soon as shares are sellable;
        - a fresh STOP supersedes a pending TP;
        - UNKNOWN_AFTER_SUBMISSION is never touched here.
        """
        sellable = (
            decimal_value(position.get("sellable_shares_text"))
            or Decimal("0")
        )

        if sellable <= 0:
            return False

        active_id = position.get("active_exit_intent_id")
        active_state = str(
            position.get("active_exit_intent_state") or ""
        ).upper()

        if active_id and active_state == "WAITING_SELLABLE":
            intent = self.repo.intent(str(active_id))

            if (
                not intent
                or str(intent.get("state") or "").upper()
                != "WAITING_SELLABLE"
                or intent.get("remote_order_id")
            ):
                return False

            purpose = str(intent.get("purpose") or "").upper()

            if purpose == "STOP_066":
                if not self._clear_local_waiting_intent(
                    str(active_id),
                    reason="RETRY_SELLABLE_STOP_066",
                ):
                    return False

                await self._refresh_hot_state_once()

                refreshed = (
                    self._position_from_ram(
                        str(position["token_id"]),
                        str(position["position_id"]),
                    )
                    or position
                )

                await self._market_exit_fak(
                    refreshed,
                    update,
                    purpose="STOP_066",
                    min_price=self.policy.stop_min_price,
                    frame_hash=frame_hash,
                )

                await self._refresh_hot_state_once()
                return True

            if purpose.startswith("EMERGENCY_"):
                min_price = decimal_value(
                    intent.get("price_limit_text")
                )

                # price_limit_text is persisted when the intent is reserved.
                # If it is somehow absent, keep the pending intent fail-closed.
                if min_price is None:
                    return False

                if not self._clear_local_waiting_intent(
                    str(active_id),
                    reason=f"RETRY_SELLABLE_{purpose}",
                ):
                    return False

                await self._refresh_hot_state_once()

                refreshed = (
                    self._position_from_ram(
                        str(position["token_id"]),
                        str(position["position_id"]),
                    )
                    or position
                )

                await self._market_exit_fak(
                    refreshed,
                    update,
                    purpose=purpose,
                    min_price=min_price,
                    frame_hash=frame_hash,
                )

                await self._refresh_hot_state_once()
                return True

            # Unknown EXIT purpose: do not guess or clear it.
            return False

        tp_id = position.get("tp_intent_id")
        tp_state = str(
            position.get("tp_intent_state") or ""
        ).upper()

        if tp_id and tp_state == "WAITING_SELLABLE":
            # A latched STOP takes priority over retrying TP.
            if bid <= self.policy.stop_price:
                return False

            if not reconciliation_ready:
                return False

            intent = self.repo.intent(str(tp_id))

            if (
                not intent
                or str(intent.get("state") or "").upper()
                != "WAITING_SELLABLE"
                or intent.get("remote_order_id")
                or str(intent.get("purpose") or "").upper()
                != "TAKE_PROFIT"
            ):
                return False

            if not self._clear_local_waiting_intent(
                str(tp_id),
                reason="RETRY_SELLABLE_TAKE_PROFIT",
            ):
                return False

            await self._refresh_hot_state_once()

            refreshed = (
                self._position_from_ram(
                    str(position["token_id"]),
                    str(position["position_id"]),
                )
                or position
            )

            await self._ensure_take_profit(refreshed)
            await self._refresh_hot_state_once()
            return True

        return False


    async def _ensure_take_profit(self, position: dict[str, Any]) -> None:
        remaining = (
            decimal_value(position.get("remaining_shares_text"))
            or Decimal("0")
        )
        sellable = (
            decimal_value(position.get("sellable_shares_text"))
            or Decimal("0")
        )
        min_sellable = self._min_order(str(position.get("condition_id") or ""))
        if (
            remaining <= 0
            or remaining < min_sellable
            or position.get("tp_intent_id")
            or str(position.get("state") or "") != "OPEN"
            or int(position.get("stop_stage") or 0) > 0
        ):
            return
        intent = self.repo.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
            shares=remaining, price_limit=self.policy.take_profit_price,
            book_hash="entry-settlement",
        )
        if intent.get("_duplicate"):
            return
        if not self.paper_mode() and sellable < remaining:
            self.repo.mark_waiting_sellable(
                str(intent["intent_id"]),
                reason="TAKE_PROFIT_WAITING_FOR_FULL_SELLABLE_BALANCE",
            )
            return
        if self.paper_mode():
            self.repo.update_intent(
                str(intent["intent_id"]), state="LIVE", requested_shares_text=canonical_decimal(remaining)
            )
            return
        response = await self.adapter.create_order({
            "idempotency_key": intent["intent_id"],
            "durable_intent_reserved": True,
            "event_id": position["event_id"],
            "condition_id": position["condition_id"],
            "token_id": position["token_id"],
            "outcome": position["outcome"],
            "side": "SELL",
            "order_type": "GTC",
            "purpose": "TAKE_PROFIT",
            "position_id": position["position_id"],
            "deal_id": stable_id("deal", position["event_id"]),
            "requested_price": "0.96",
            "requested_size": canonical_decimal(remaining),
        })
        if self._is_waiting_sellable_response(response):
            self.repo.mark_waiting_sellable(
                str(intent["intent_id"]),
                reason=str(
                    response.get("failure_reason")
                    or "INSUFFICIENT_BALANCE"
                ),
                normalized_error=response.get("message"),
            )
            return

        if self._finalize_known_no_remote_submission(
            intent,
            response,
        ):
            return

        status = str(
            response.get("status") or "unknown"
        ).upper()

        self.repo.update_intent(
            str(intent["intent_id"]),
            state=status,
            remote_order_id=response.get("polymarket_order_id"),
            reason_code=response.get("failure_reason"),
        )

    @staticmethod
    def _exit_liquidity_hash(update: dict[str, Any]) -> str:
        """Identify executable bid liquidity without conflating frame churn."""
        levels: list[tuple[Decimal, Decimal]] = []
        for level in update.get("bids") or []:
            if not isinstance(level, dict):
                continue
            price = decimal_value(level.get("price"))
            size = decimal_value(level.get("size"))
            if (
                price is None
                or size is None
                or price < Decimal("0.01")
                or size <= 0
            ):
                continue
            levels.append((price, size))
        levels.sort(key=lambda item: item[0], reverse=True)
        payload = {
            "asset_id": str(update.get("asset_id") or ""),
            "generation": int(update.get("generation") or 0),
            "bids": [
                [canonical_decimal(price), canonical_decimal(size)]
                for price, size in levels
            ],
        }
        return stable_id(
            "exit-liquidity",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def _note_exit_no_liquidity(
        self,
        position: dict[str, Any],
        update: dict[str, Any],
        book_hash: str,
    ) -> None:
        if not self.repo.note_exit_liquidity_wait(
            str(position["position_id"]),
            book_hash,
        ):
            return
        bid = decimal_value(update.get("best_bid"))
        self.repo.timeline(
            severity="WARNING",
            category="EXIT",
            component="strategy",
            source="deterministic_book",
            event_id=position["event_id"],
            condition_id=position["condition_id"],
            token_id=position["token_id"],
            side="SELL",
            deal_id=stable_id("deal", position["event_id"]),
            requested_action="SELL_MARKET_FAK",
            reason_code="EXIT_NO_LIQUIDITY_ABOVE_FLOOR",
            result_status="EXIT_RETRY_PENDING",
            remaining_shares_text=position["remaining_shares_text"],
            parameters_json={
                "best_bid": (
                    canonical_decimal(bid)
                    if bid is not None
                    else None
                ),
                "min_price": canonical_decimal(
                    self.policy.stop_min_price
                ),
                "liquidity_hash": book_hash,
            },
        )

    async def _manage_position(
        self,
        *,
        market: dict[str, Any],
        update: dict[str, Any],
        event_ready: bool,
        frame_hash: str,
    ) -> None:
        token_id = str(update.get("asset_id") or "")
        positions = self._positions_from_ram(token_id)

        if not positions or not event_ready:
            return

        bid = decimal_value(update.get("best_bid"))

        reconciliation_ready = (
            self.paper_mode()
            or str(
                self._hot_state.get(
                    "reconciliation_readiness",
                    "NOT_READY",
                )
            ) == "READY"
        )

        exit_book_hash = self._exit_liquidity_hash(update)
        for position in positions:
            stop_latched = int(position.get("stop_stage") or 0) >= 1

            if (
                not stop_latched
                and bid is not None
                and bid <= self.policy.stop_price
            ):
                position = self.repo.latch_stop_exit(
                    str(position["position_id"])
                )
                stop_latched = True
                if position.pop("_newly_latched", False):
                    self.repo.timeline(
                        severity="WARNING",
                        category="EXIT",
                        component="strategy",
                        source="deterministic_book",
                        event_id=position["event_id"],
                        condition_id=position["condition_id"],
                        token_id=position["token_id"],
                        side="SELL",
                        deal_id=stable_id(
                            "deal", position["event_id"]
                        ),
                        requested_action="LATCH_MARKET_EXIT",
                        reason_code="STOP_066_TRIGGER_LATCHED",
                        result_status="LATCHED",
                        remaining_shares_text=position[
                            "remaining_shares_text"
                        ],
                        parameters_json={
                            "trigger_bid": canonical_decimal(bid),
                            "min_price": canonical_decimal(
                                self.policy.stop_min_price
                            ),
                            "liquidity_hash": exit_book_hash,
                        },
                    )
                await self._refresh_hot_state_once()

                position = (
                    self._position_from_ram(
                        token_id,
                        str(position["position_id"]),
                    )
                    or position
                )

            if stop_latched:
                if (
                    str(position.get("state") or "").upper()
                    == "EXIT_RECONCILIATION_REQUIRED"
                ):
                    reconciled = await self._reconcile(
                        "latched_stop_reconciliation_required"
                    )
                    if reconciled.get("status") == "ok":
                        self.repo.clear_exit_reconciliation(
                            str(position["position_id"])
                        )
                    await self._refresh_hot_state_once()
                    continue

                if (
                    bid is None
                    or bid < self.policy.stop_min_price
                ):
                    self._note_exit_no_liquidity(
                        position,
                        update,
                        exit_book_hash,
                    )
                    continue

                resumed_waiting = (
                    await self._resume_waiting_sellable_intent(
                        position,
                        update,
                        bid=bid,
                        frame_hash=exit_book_hash,
                        reconciliation_ready=reconciliation_ready,
                    )
                )
                if resumed_waiting:
                    continue

                # A non-terminal remote intent may exist. Its identity/fills
                # must be reconciled before another SELL can be reserved.
                if position.get("active_exit_intent_id"):
                    continue

                await self._market_exit_fak(
                    position,
                    update,
                    purpose="STOP_066",
                    min_price=self.policy.stop_min_price,
                    frame_hash=exit_book_hash,
                )
                await self._refresh_hot_state_once()
                continue

            if bid is None:
                continue

            resumed_waiting = await self._resume_waiting_sellable_intent(
                position,
                update,
                bid=bid,
                frame_hash=frame_hash,
                reconciliation_ready=reconciliation_ready,
            )

            if resumed_waiting:
                continue

            if (
                not position.get("tp_intent_id")
                and not position.get("active_exit_intent_id")
                and position.get("state") == "OPEN"
                and reconciliation_ready
            ):
                # Creating a TP is an actual durable action, so a RAM
                # refresh afterwards is appropriate and keeps the next
                # critical STOP frame synchronized with the new TP.
                await self._ensure_take_profit(position)
                await self._refresh_hot_state_once()

                position = (
                    self._position_from_ram(
                        token_id,
                        str(position["position_id"]),
                    )
                    or position
                )

            tp_intent = None

            if position.get("tp_intent_id"):
                tp_intent = {
                    "intent_id": str(
                        position["tp_intent_id"]
                    ),
                    "state": str(
                        position.get("tp_intent_state")
                        or ""
                    ),
                }

            if (
                tp_intent
                and tp_intent.get("state") == "LIVE"
                and bid >= self.policy.take_profit_price
            ):
                await self._paper_tp_fill(
                    position,
                    tp_intent,
                    update,
                    frame_hash,
                )

                # Durable position/fill state changed.
                await self._refresh_hot_state_once()
                continue

    async def _paper_tp_fill(
        self,
        position: dict[str, Any],
        intent: dict[str, Any],
        update: dict[str, Any],
        frame_hash: str,
    ) -> None:
        if not self.paper_mode():
            return
        shares = decimal_value(position["remaining_shares_text"]) or Decimal("0")
        fill = simulate_sell_fak(
            update.get("bids") or [], shares=shares,
            min_price=self.policy.take_profit_price,
            fee_rate=self._fee_rate(self._market(position["condition_id"]) or {}),
        )
        if fill.filled_shares <= 0:
            return
        updated = self.repo.apply_exit_fill(
            position_id=position["position_id"], intent_id=intent["intent_id"],
            sold_shares=fill.filled_shares, average_price=fill.average_price,
            fees=fill.fee,
            final_state="FILLED" if fill.remaining_request == 0 else "PARTIAL",
            min_sellable=self._min_order(position["condition_id"]),
            purpose="TAKE_PROFIT", book_hash=frame_hash,
        )
        self.repo.timeline(
            severity="INFO", category="FILL", component="paper_strategy",
            source="deterministic_book", event_id=position["event_id"],
            condition_id=position["condition_id"], token_id=position["token_id"],
            side="SELL", deal_id=stable_id("deal", position["event_id"]),
            intent_id=intent["intent_id"], requested_action="SELL_LIMIT_GTC",
            reason_code="TAKE_PROFIT_FILL", result_status=(
                "FILLED" if updated["state"] == "CLOSED" else "PARTIAL"
            ),
            requested_shares_text=canonical_decimal(shares),
            filled_shares_text=canonical_decimal(fill.filled_shares),
            average_price_text=canonical_decimal(fill.average_price),
            fees_text=canonical_decimal(fill.fee),
            remaining_shares_text=updated["remaining_shares_text"],
            pnl_text=updated["realized_pnl_text"],
        )


    async def _market_exit_fak(
        self,
        position: dict[str, Any],
        update: dict[str, Any],
        *,
        purpose: str,
        min_price: Decimal,
        frame_hash: str,
    ) -> None:
        if position.get("last_exit_book_hash") == frame_hash:
            return
        if position.get("active_exit_intent_id"):
            active_id = str(position["active_exit_intent_id"])
            active_current = self.repo.intent(active_id)

            if (
                active_current
                and str(active_current.get("state") or "").upper()
                == "WAITING_SELLABLE"
                and not active_current.get("remote_order_id")
            ):
                # This EXIT never reached Polymarket. The current market-exit
                # request supersedes it locally; no remote cancel is needed.
                self._clear_local_waiting_intent(
                    active_id,
                    reason=f"SUPERSEDED_BY_{purpose}",
                )

                refreshed = self.repo.active_positions(
                    str(position["token_id"])
                )
                position = next(
                    (
                        item
                        for item in refreshed
                        if item["position_id"]
                        == position["position_id"]
                    ),
                    position,
                )
            elif (
                active_current
                and not active_current.get("remote_order_id")
                and not self.paper_mode()
            ):
                self.repo.require_exit_reconciliation(
                    str(position["position_id"])
                )
                self.repo.alert(
                    alert_type="EXIT", severity="CRITICAL",
                    reason_code="ACTIVE_EXIT_REMOTE_ORDER_ID_UNKNOWN",
                    message="Active EXIT remote order identity is unresolved; market SELL was not sent",
                    entity_type="position", entity_id=position["position_id"],
                )
                return
            else:
                active = self.repo.cancel_active_exit(
                    position["position_id"],
                    purpose,
                )
                if active:
                    if self.paper_mode():
                        self.repo.finalize_cancel(
                            str(active["intent_id"]), True, "PAPER_CANCEL_ACK"
                        )
                    else:
                        response = await self.adapter.cancel_order_with_context(
                            active.get("remote_order_id"),
                            {
                                "event_id": position["event_id"],
                                "condition_id": position["condition_id"],
                                "token_id": position["token_id"],
                                "intent_id": active["intent_id"],
                                "position_id": position["position_id"],
                                "deal_id": stable_id("deal", position["event_id"]),
                                "purpose": purpose,
                                "side": "SELL",
                                "order_type": active.get("order_type"),
                            },
                        )
                        if not response.get("success"):
                            self.repo.finalize_cancel(
                                str(active["intent_id"]), False, "CANCEL_UNCERTAIN"
                            )
                            self.repo.alert(
                                alert_type="EXIT",
                                severity="CRITICAL",
                                reason_code="STOP_CANCEL_UNCERTAIN",
                                message="EXIT cancellation is uncertain; market SELL was not sent",
                                entity_type="position",
                                entity_id=position["position_id"],
                            )
                            return
                        self.repo.finalize_cancel(
                            str(active["intent_id"]), True, "CANCEL_ACK"
                        )
                        reconciled = await self._reconcile("exit_cancel_before_market_sell")
                        if reconciled.get("status") != "ok":
                            self.repo.require_exit_reconciliation(
                                str(position["position_id"])
                            )
                            return
            refreshed = self.repo.active_positions(str(position["token_id"]))
            position = next(
                (item for item in refreshed if item["position_id"] == position["position_id"]),
                position,
            )
        if position.get("tp_intent_id"):
            tp_id = str(position["tp_intent_id"])
            tp_current = self.repo.intent(tp_id)

            if (
                tp_current
                and str(tp_current.get("state") or "").upper()
                == "WAITING_SELLABLE"
                and not tp_current.get("remote_order_id")
            ):
                self._clear_local_waiting_intent(
                    tp_id,
                    reason=f"SUPERSEDED_BY_{purpose}",
                )

                refreshed = self.repo.active_positions(
                    str(position["token_id"])
                )
                position = next(
                    (
                        item
                        for item in refreshed
                        if item["position_id"]
                        == position["position_id"]
                    ),
                    position,
                )
            elif (
                tp_current
                and not tp_current.get("remote_order_id")
                and not self.paper_mode()
            ):
                self.repo.require_exit_reconciliation(
                    str(position["position_id"])
                )
                self.repo.alert(
                    alert_type="EXIT", severity="CRITICAL",
                    reason_code="TP_REMOTE_ORDER_ID_UNKNOWN",
                    message="TP remote order identity is unresolved; market SELL was not sent",
                    entity_type="position", entity_id=position["position_id"],
                )
                return
            else:
                tp = self.repo.cancel_tp(
                    position["position_id"],
                    purpose,
                )
                if tp:
                    if self.paper_mode():
                        self.repo.finalize_cancel(str(tp["intent_id"]), True, "PAPER_CANCEL_ACK")
                    else:
                        response = await self.adapter.cancel_order_with_context(
                            tp.get("remote_order_id"),
                            {
                                "event_id": position["event_id"],
                                "condition_id": position["condition_id"],
                                "token_id": position["token_id"],
                                "intent_id": tp["intent_id"],
                                "position_id": position["position_id"],
                                "deal_id": stable_id("deal", position["event_id"]),
                                "purpose": purpose,
                                "side": "SELL",
                                "order_type": tp.get("order_type"),
                            },
                        )
                        if not response.get("success"):
                            self.repo.finalize_cancel(
                                str(tp["intent_id"]), False, "CANCEL_UNCERTAIN"
                            )
                            self.repo.alert(
                                alert_type="EXIT", severity="CRITICAL",
                                reason_code="EXIT_RECONCILIATION_REQUIRED",
                                message="TP cancellation is uncertain; SELL was not sent",
                                entity_type="position", entity_id=position["position_id"],
                            )
                            return
                        self.repo.finalize_cancel(str(tp["intent_id"]), True, "CANCEL_ACK")
                        reconciled = await self._reconcile(
                            "tp_cancel_before_market_sell"
                        )
                        if reconciled.get("status") != "ok":
                            self.repo.require_exit_reconciliation(
                                str(position["position_id"])
                            )
                            return
            refreshed = self.repo.active_positions(str(position["token_id"]))
            position = next(
                (item for item in refreshed if item["position_id"] == position["position_id"]),
                position,
            )
        shares = decimal_value(position.get("sellable_shares_text")) or Decimal("0")
        if shares <= 0:
            pending_shares = (
                decimal_value(position.get("remaining_shares_text"))
                or Decimal("0")
            )

            if pending_shares <= 0 or self.paper_mode():
                return

            intent = self.repo.reserve_position_intent(
                position,
                action="EXIT",
                purpose=purpose,
                order_type="FAK",
                shares=pending_shares,
                price_limit=min_price,
                book_hash=frame_hash,
            )

            if intent.get("_duplicate"):
                return

            self.repo.mark_waiting_sellable(
                str(intent["intent_id"]),
                reason=f"{purpose}_WAITING_FOR_SELLABLE_BALANCE",
            )

            self.repo.timeline(
                severity="CRITICAL",
                category="EXIT",
                component="strategy",
                source="deterministic_book",
                event_id=position["event_id"],
                condition_id=position["condition_id"],
                token_id=position["token_id"],
                side="SELL",
                deal_id=stable_id("deal", position["event_id"]),
                intent_id=intent["intent_id"],
                requested_action="SELL_MARKET_FAK",
                reason_code=f"{purpose}_WAITING_SELLABLE",
                result_status="WAITING",
                requested_shares_text=canonical_decimal(pending_shares),
                parameters_json={
                    "trigger_bid": canonical_decimal(
                        decimal_value(update.get("best_bid"))
                        or Decimal("0")
                    ),
                    "min_price": canonical_decimal(min_price),
                },
            )

            return

        intent = self.repo.reserve_position_intent(
            position, action="EXIT", purpose=purpose, order_type="FAK",
            shares=shares, price_limit=min_price, book_hash=frame_hash,
        )
        if intent.get("_duplicate"):
            return
        if self.paper_mode():
            fill = simulate_sell_fak(
                update.get("bids") or [], shares=shares, min_price=min_price,
                fee_rate=self._fee_rate(self._market(position["condition_id"]) or {}),
            )
            updated = self.repo.apply_exit_fill(
                position_id=position["position_id"], intent_id=intent["intent_id"],
                sold_shares=fill.filled_shares, average_price=fill.average_price,
                fees=fill.fee, final_state=(
                    "FILLED" if fill.remaining_request == 0
                    else "ZERO_FILL" if fill.filled_shares == 0
                    else "PARTIAL_FINAL"
                ),
                min_sellable=self._min_order(position["condition_id"]),
                purpose=purpose, book_hash=frame_hash,
            )
            self.repo.timeline(
                severity="WARNING", category="EXIT", component="paper_strategy",
                source="deterministic_book", event_id=position["event_id"],
                condition_id=position["condition_id"], token_id=position["token_id"],
                side="SELL", deal_id=stable_id("deal", position["event_id"]),
                intent_id=intent["intent_id"], requested_action="SELL_MARKET_FAK",
                reason_code=purpose, result_status=(
                    "ZERO_FILL" if fill.filled_shares == 0
                    else "FILLED" if updated["state"] == "CLOSED" else "PARTIAL"
                ),
                requested_shares_text=canonical_decimal(shares),
                filled_shares_text=canonical_decimal(fill.filled_shares),
                average_price_text=canonical_decimal(fill.average_price),
                fees_text=canonical_decimal(fill.fee),
                remaining_shares_text=updated["remaining_shares_text"],
                pnl_text=updated["realized_pnl_text"],
                parameters_json={"min_price": canonical_decimal(min_price)},
            )
            return

        response = await self.adapter.create_order({
            "idempotency_key": intent["intent_id"],
            "durable_intent_reserved": True,
            "event_id": position["event_id"],
            "condition_id": position["condition_id"],
            "token_id": position["token_id"],
            "outcome": position["outcome"],
            "side": "SELL",
            "order_type": "FAK",
            "purpose": purpose,
            "position_id": position["position_id"],
            "deal_id": stable_id("deal", position["event_id"]),
            "requested_size": canonical_decimal(shares),
            "min_price": canonical_decimal(min_price),
        })
        if self._is_waiting_sellable_response(response):
            self.repo.mark_waiting_sellable(
                str(intent["intent_id"]),
                reason=str(
                    response.get("failure_reason")
                    or "INSUFFICIENT_BALANCE"
                ),
                normalized_error=response.get("message"),
            )
            return

        if self._finalize_known_no_remote_submission(
            intent,
            response,
            fak=True,
        ):
            return

        self.repo.update_intent(
            str(intent["intent_id"]),
            state="RECONCILIATION_REQUIRED",
            remote_order_id=response.get("polymarket_order_id"),
            reason_code=(
                response.get("failure_reason")
                or response.get("status")
            ),
        )

        self.repo.require_exit_reconciliation(
            str(position["position_id"])
        )
        reconciled = await self._reconcile("exit_submission")
        if reconciled.get("status") == "ok":
            self.repo.clear_exit_reconciliation(
                str(position["position_id"])
            )

    async def _handle_resolution(self, market: dict[str, Any]) -> None:
        winner_asset = str(market.get("winning_asset_id") or "")
        for position in self.repo.unresolved_positions(str(market["condition_id"])):
            winner = position["token_id"] == winner_asset
            updated = self.repo.mark_position_resolved(
                position["position_id"], winner=winner,
                redeem_pending=winner and (
                    decimal_value(position["remaining_shares_text"]) or Decimal("0")
                ) > 0,
            )
            self.repo.timeline(
                severity="INFO", category="RESOLUTION", component="strategy",
                source="market_ws", event_id=position["event_id"],
                condition_id=position["condition_id"], token_id=position["token_id"],
                side="SELL", deal_id=stable_id("deal", position["event_id"]),
                requested_action="RESOLUTION", reason_code=(
                    "WINNER_REDEEM_PENDING" if winner else "LOSER_ZERO"
                ), result_status="SETTLED" if not winner else "PENDING",
                remaining_shares_text=updated["remaining_shares_text"],
                pnl_text=updated["realized_pnl_text"],
            )
            if winner and self.paper_mode():
                self.repo.mark_position_redeemed(position["position_id"], "paper-resolution")

    def _min_order(self, condition_id: str) -> Decimal:
        market = self._market(condition_id) or {}
        return decimal_value(market.get("min_order_size")) or Decimal("0.000001")

    async def _reconcile(self, reason: str) -> dict[str, Any]:
        if self.reconciliation is None:
            return {"status": "not_configured"}
        return await self.reconciliation(reason)

    async def emergency_close_all(self, books: Any, actor: str = "operator") -> dict[str, Any]:
        self.repo.set_pause_entries(True, actor, "EMERGENCY_CLOSE_REQUESTED")
        reconciliation = await self._reconcile("operator_emergency_preview")
        if reconciliation.get("status") != "ok":
            return {"status": "blocked", "reason": "RECONCILIATION_NOT_CLEAN", "results": []}
        results: list[dict[str, Any]] = []
        for position in self.repo.active_positions():
            token_id = str(position["token_id"])
            book = getattr(books, "books", {}).get(token_id)
            if book is None or not book.ready:
                results.append({
                    "position_id": position["position_id"], "status": "blocked",
                    "reason": "HEALTHY_BOOK_REQUIRED",
                })
                continue
            frame_hash = f"operator:{actor}:{book.generation}:{book.update_number}:{book.last_message_hash}"
            update = book.view(
                event_type="operator_emergency", timestamp=None, message_hash=frame_hash
            )
            await self._market_exit_fak(
                position, update, purpose="EMERGENCY_OPERATOR",
                min_price=self.policy.emergency_min_price, frame_hash=frame_hash,
            )
            refreshed = self.repo.position_for_token(token_id) or position
            results.append({
                "position_id": position["position_id"],
                "status": refreshed.get("state"),
                "remaining_shares_text": refreshed.get("remaining_shares_text"),
            })
        return {"status": "executed", "results": results}

    async def start_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        self._stop.clear()

        # Refresh once before workers begin consuming market frames.
        await self._refresh_hot_state_once()

        if self._hot_state_task is None or self._hot_state_task.done():
            self._hot_state_task = asyncio.create_task(
                self._hot_state_loop(),
                name="strategy-hot-state-refresh",
            )

        if self._frame_task is None or self._frame_task.done():
            self._frame_task = asyncio.create_task(
                self._frame_worker(), name="strategy-frame-worker"
            )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="polymarket-order-heartbeat"
        )

    async def stop(self) -> None:
        self._stop.set()

        if self._hot_state_task:
            self._hot_state_task.cancel()
            try:
                await self._hot_state_task
            except asyncio.CancelledError:
                pass
            self._hot_state_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._frame_task:
            self._frame_event.set()
            try:
                await asyncio.wait_for(self._frame_task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._frame_task.cancel()
            self._frame_task = None

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if self.config.real_submission_armed():
                response = await self.adapter.heartbeat()
                success = bool(response.get("success"))
                self.base.set_state(
                    "order_heartbeat_status", "OK" if success else "FAILED", "heartbeat"
                )
                if not success:
                    self.repo.set_pause_entries(
                        True, "heartbeat", "HEARTBEAT_FAILURE",
                        owner="MACHINE", auto_recoverable=True,
                    )
                    self.repo.alert(
                        alert_type="HEARTBEAT", severity="CRITICAL",
                        reason_code="HEARTBEAT_FAILURE",
                        message="Order heartbeat failed; entries paused",
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), self.config.order_heartbeat_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "mode": self.config.execution_mode,
            "frames_processed": self.frames_processed,
            "frame_queue_depth": len(self._pending_frames),
            "frames_coalesced": self.frames_coalesced,
            "frames_dropped": self.frames_dropped,
            "critical_queue_depth": len(self._critical_frames),
            "critical_triggers_queued": self.critical_triggers_queued,
            "critical_triggers_processed": self.critical_triggers_processed,
            "critical_triggers_dropped": self.critical_triggers_dropped,
            "max_critical_queue_depth": self.max_critical_queue_depth,
            "hot_state_age_ms": (
                max(
                    0.0,
                    (
                        time.monotonic()
                        - self._hot_state_refreshed_monotonic
                    ) * 1000,
                )
                if self._hot_state_refreshed_monotonic > 0
                else None
            ),
            "hot_state_refresh_failures": self.hot_state_refresh_failures,
            "last_error": self.last_error,
            "entry_schedule": self.entry_schedule_status(),
            **self.repo.strategy_status(),
        }
