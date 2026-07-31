from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .repository import LiveRepository


class PaperTradingEngine:
    """Market-data-only paper lifecycle.

    This module intentionally has no adapter, order manager, credentials, or CLOB
    write dependency. Its only inputs are persisted public Market WebSocket
    snapshots and PAPER_TRADING rules.
    """

    def __init__(
        self,
        repo: LiveRepository,
        *,
        enabled: bool = False,
        max_market_age_seconds: int = 5,
        taker_fee_rate: Decimal = Decimal("0.07"),
    ):
        self.repo = repo
        self.enabled = enabled
        self.max_market_age_seconds = max_market_age_seconds
        self.taker_fee_rate = taker_fee_rate
        self.evaluations = 0
        self.opened = 0
        self.closed = 0
        self.last_error: str | None = None

    def process_snapshot(self, snapshot: dict[str, Any]) -> dict[str, int]:
        result = {"evaluated": 0, "opened": 0, "closed": 0}
        if not self.enabled:
            return result
        if self._is_stale(snapshot):
            self.repo.audit(
                "paper_engine", "paper_snapshot_rejected", "blocked", "STALE_MARKET_DATA",
                {"snapshot_id": snapshot.get("id")},
            )
            return result

        closed_rule_ids: set[int] = set()
        bid = self._decimal(snapshot.get("best_bid"))
        if bid is not None:
            for deal in self.repo.open_paper_deals(asset_id=str(snapshot["asset_id"])):
                stop_loss = self._decimal(deal.get("stop_loss_price"))
                take_profit = self._decimal(deal.get("take_profit_price"))
                if snapshot.get("event_type") == "market_resolved":
                    self.repo.close_paper_deal(
                        deal, snapshot, reason="event_resolution", exit_price=float(bid)
                    )
                elif stop_loss is not None and bid <= stop_loss:
                    self.repo.close_paper_deal(
                        deal, snapshot, reason="stop_loss", exit_price=float(stop_loss)
                    )
                elif take_profit is not None and bid >= take_profit:
                    self.repo.close_paper_deal(
                        deal, snapshot, reason="take_profit", exit_price=float(take_profit)
                    )
                else:
                    continue
                closed_rule_ids.add(int(deal["live_rule_id"]))
                self.closed += 1
                result["closed"] += 1

        if snapshot.get("event_type") == "market_resolved":
            return result

        for rule in self.repo.active_paper_rules():
            decision, reason = self._entry_decision(rule, snapshot, closed_rule_ids)
            evaluation = self.repo.record_rule_evaluation(rule, snapshot, decision, reason)
            if evaluation is None:
                continue
            self.evaluations += 1
            result["evaluated"] += 1
            if decision != "OPEN":
                continue
            deal = self.repo.create_paper_deal(
                rule, snapshot, reason=reason, fee_rate=self.taker_fee_rate
            )
            if deal is not None:
                self.opened += 1
                result["opened"] += 1
        return result

    def _entry_decision(
        self, rule: dict[str, Any], snapshot: dict[str, Any], closed_rule_ids: set[int]
    ) -> tuple[str, str]:
        rule_id = int(rule["id"])
        if rule_id in closed_rule_ids:
            return "SKIP", "DEAL_CLOSED_ON_SAME_SNAPSHOT"
        event_id = str(snapshot.get("event_id") or "")
        if not event_id:
            return "SKIP", "MISSING_EVENT_ID"
        if rule.get("eligible_after_event_id") == event_id:
            return "SKIP", "WAITING_FOR_NEXT_EVENT"
        ask = self._decimal(snapshot.get("best_ask"))
        if ask is None:
            return "SKIP", "MISSING_BEST_ASK"
        entry = self._decimal(rule.get("entry_price"))
        if entry is None or ask != entry:
            return "SKIP", "ENTRY_PRICE_NOT_MATCHED"

        market = self.repo.market_for_asset(str(snapshot["asset_id"]))
        if not market:
            return "SKIP", "UNKNOWN_ASSET"
        other_asset = (
            market.get("no_token_id")
            if str(market.get("yes_token_id")) == str(snapshot["asset_id"])
            else market.get("yes_token_id")
        )
        if other_asset:
            other = self.repo.latest_market_snapshot(str(other_asset))
            other_ask = self._decimal(other.get("best_ask")) if other else None
            if other_ask == entry:
                return "SKIP", "BOTH_SIDES_MATCH"
        if any(int(deal["live_rule_id"]) == rule_id for deal in self.repo.open_paper_deals()):
            return "SKIP", "OPEN_DEAL_EXISTS"
        return "OPEN", "ENTRY_PRICE_MATCHED"

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "status": "RUNNING" if self.enabled else "DISABLED",
            "evaluations": self.evaluations,
            "opened": self.opened,
            "closed": self.closed,
            "last_error": self.last_error,
            "write_dependencies": [],
        }

    def _is_stale(self, snapshot: dict[str, Any]) -> bool:
        received_at = snapshot.get("received_at")
        if not received_at:
            return True
        try:
            received = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds()
        return age > self.max_market_age_seconds

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        return result if result.is_finite() else None
