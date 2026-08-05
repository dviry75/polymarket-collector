from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, Awaitable, Callable

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
            entry_window_seconds=config.strategy_entry_window_seconds,
        )
        self.policy.validate()
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self.frames_processed = 0
        self.last_error = ""

    def enabled(self) -> bool:
        return self.config.live_module_enabled and self.config.execution_mode in {
            "PAPER_TRADING", "REAL_TRADING"
        }

    def paper_mode(self) -> bool:
        return self.config.execution_mode == "PAPER_TRADING"

    def schedule_frame(self, context: dict[str, Any]) -> None:
        if not self.enabled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.process_atomic_frame(context))
            return
        loop.create_task(self.process_atomic_frame(context))

    async def process_atomic_frame(self, context: dict[str, Any]) -> None:
        self.frames_processed += 1
        updates = [item for item in context.get("updates") or [] if isinstance(item, dict)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for update in updates:
            condition_id = str(update.get("condition_id") or "")
            if condition_id:
                grouped.setdefault(condition_id, []).append(update)
        for condition_id, condition_updates in grouped.items():
            market = self.base.latest_market(condition_id)
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
        decision = choose_entry(
            updates=updates,
            yes_token_id=yes_token,
            no_token_id=no_token,
            event_ready=event_ready,
            paused=(
                self.repo.pause_entries()
                or (
                    not self.paper_mode()
                    and self.base.get_state("canary_armed", "false").lower() != "true"
                )
            ),
            event_locked=self.repo.event_state(event_id) is not None,
            active_exposure=self.repo.exposure(),
            observed_at=observed_at,
            event_id=event_id,
            policy=self.policy,
        )
        has_trigger = any(
            exact_trigger(update.get("best_ask"), self.policy.entry_price) for update in updates
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
                        readiness_reason if decision.reason == "MARKET_DATA_NOT_READY"
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
        viable, viability_reason = AllInBudget(self.policy.max_spend).minimum_viable(
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
        reservation = self.repo.reserve_event_entry(
            event_id=event_id, condition_id=condition_id,
            token_id=decision.token_id, side=decision.side,
            simultaneous=False, reason_code=decision.reason,
        )
        if reservation.get("_duplicate"):
            return
        intent_id = str(reservation["entry_intent_id"])
        self.repo.timeline(
            severity="INFO", category="ORDER", component="strategy",
            source="market_ws", event_id=event_id, condition_id=condition_id,
            token_id=decision.token_id, side=decision.side,
            deal_id=stable_id("deal", event_id),
            correlation_id=stable_id("correlation", event_id),
            intent_id=intent_id, requested_action="BUY_MARKET_FAK",
            reason_code="ENTRY_PRICE_EXACT", previous_state="ELIGIBLE",
            new_state="ENTRY_INTENT_RESERVED", result_status="RESERVED",
            requested_amount_text="5", parameters_json={
                "max_price": "0.76", "max_spend": "5", "all_in": True,
                "frame_hash": frame_hash,
            },
        )
        if not self.paper_mode():
            self.repo.consume_canary()
        update = next(
            item for item in updates if str(item.get("asset_id")) == decision.token_id
        )
        await self._submit_entry(
            market=market, update=update, side=str(decision.side),
            intent_id=intent_id, fee_rate=fee_rate,
        )

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
        if self.paper_mode():
            fill = simulate_buy_fak(
                update.get("asks") or [],
                max_price=self.policy.entry_max_price,
                max_spend=self.policy.max_spend,
                fee_rate=fee_rate,
            )
            if fill.filled_shares <= 0:
                self.repo.mark_zero_fill(event_id, "FAK_ZERO_FILL")
                self.repo.timeline(
                    severity="WARNING", category="FILL", component="paper_strategy",
                    source="deterministic_book", event_id=event_id,
                    condition_id=market["condition_id"], token_id=update["asset_id"],
                    side=side, intent_id=intent_id, requested_action="BUY_MARKET_FAK",
                    reason_code="FAK_ZERO_FILL", result_status="ZERO_FILL",
                    requested_amount_text="5", filled_shares_text="0",
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
                requested_amount_text="5",
                filled_shares_text=canonical_decimal(fill.filled_shares),
                average_price_text=canonical_decimal(fill.average_price),
                fees_text=canonical_decimal(fill.fee),
                remaining_shares_text=canonical_decimal(fill.remaining_request),
            )
            await self._ensure_take_profit(position)
            return

        self.repo.update_intent(intent_id, state="SUBMITTING", submitted_at=now_iso())
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
            "requested_amount_usd": "5",
            "max_spend": "5",
            "max_price": "0.76",
        })
        status = str(response.get("status") or "unknown").lower()
        remote_id = response.get("polymarket_order_id")
        if status == "rejected" and response.get("failure_reason") in {
            "fak_not_filled", "FAK_NOT_FILLED"
        }:
            self.repo.mark_zero_fill(event_id, "FAK_ZERO_FILL")
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
        await self._reconcile("entry_submission")

    async def _ensure_take_profit(self, position: dict[str, Any]) -> None:
        remaining = decimal_value(position.get("sellable_shares_text")) or Decimal("0")
        if remaining <= 0 or position.get("tp_intent_id"):
            return
        intent = self.repo.reserve_position_intent(
            position, action="TP", purpose="TAKE_PROFIT", order_type="GTC",
            shares=remaining, price_limit=self.policy.take_profit_price,
            book_hash="entry-settlement",
        )
        if intent.get("_duplicate"):
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
            "requested_price": "0.96",
            "requested_size": canonical_decimal(remaining),
        })
        status = str(response.get("status") or "unknown").upper()
        if response.get("failure_reason") == "INSUFFICIENT_BALANCE":
            status = "WAITING_SELLABLE"
        self.repo.update_intent(
            str(intent["intent_id"]), state=status,
            remote_order_id=response.get("polymarket_order_id"),
            reason_code=response.get("failure_reason"),
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
        positions = self.repo.active_positions(token_id)
        if not positions or not event_ready:
            return
        bid = decimal_value(update.get("best_bid"))
        if bid is None:
            return
        for position in positions:
            tp_intent = (
                self.repo.intent(str(position.get("tp_intent_id")))
                if position.get("tp_intent_id") else None
            )
            if tp_intent and tp_intent.get("state") == "LIVE" and bid >= self.policy.take_profit_price:
                await self._paper_tp_fill(position, tp_intent, update, frame_hash)
                continue
            if exact_trigger(bid, self.policy.stop_price):
                await self._emergency_exit(
                    position, update, purpose="STOP_066",
                    min_price=self.policy.stop_min_price, frame_hash=frame_hash,
                )
            elif exact_trigger(bid, self.policy.emergency_price):
                await self._emergency_exit(
                    position, update, purpose="EMERGENCY_060",
                    min_price=self.policy.emergency_min_price, frame_hash=frame_hash,
                )

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
            fee_rate=self._fee_rate(self.base.latest_market(position["condition_id"]) or {}),
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

    async def _emergency_exit(
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
        if position.get("tp_intent_id"):
            tp = self.repo.cancel_tp(position["position_id"], purpose)
            if tp:
                if self.paper_mode():
                    self.repo.finalize_cancel(str(tp["intent_id"]), True, "PAPER_CANCEL_ACK")
                else:
                    response = await self.adapter.cancel_order(str(tp.get("remote_order_id") or ""))
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
                    await self._reconcile("tp_cancel")
            refreshed = self.repo.active_positions(str(position["token_id"]))
            position = next(
                (item for item in refreshed if item["position_id"] == position["position_id"]),
                position,
            )
        shares = decimal_value(position.get("sellable_shares_text")) or Decimal("0")
        if shares <= 0:
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
                fee_rate=self._fee_rate(self.base.latest_market(position["condition_id"]) or {}),
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
            "requested_size": canonical_decimal(shares),
            "min_price": canonical_decimal(min_price),
        })
        self.repo.update_intent(
            str(intent["intent_id"]), state="RECONCILIATION_REQUIRED",
            remote_order_id=response.get("polymarket_order_id"),
            reason_code=response.get("failure_reason") or response.get("status"),
        )
        await self._reconcile("exit_submission")

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
        market = self.base.latest_market(condition_id) or {}
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
            await self._emergency_exit(
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
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="polymarket-order-heartbeat"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if self.config.real_submission_armed():
                response = await self.adapter.heartbeat()
                success = bool(response.get("success"))
                self.base.set_state(
                    "order_heartbeat_status", "OK" if success else "FAILED", "heartbeat"
                )
                if not success:
                    self.repo.set_pause_entries(True, "heartbeat", "HEARTBEAT_FAILURE")
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
            "last_error": self.last_error,
            **self.repo.strategy_status(),
        }
