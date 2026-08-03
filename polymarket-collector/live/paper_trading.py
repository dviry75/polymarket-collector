from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import json
from zoneinfo import ZoneInfo

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
        self._latest_by_asset: dict[str, dict[str, Any]] = {}

    def process_snapshot(self, snapshot: dict[str, Any]) -> dict[str, int]:
        result = {"evaluated": 0, "opened": 0, "closed": 0}
        if not self.enabled:
            return result
        asset_id = str(snapshot.get("asset_id") or "")
        if asset_id:
            self._latest_by_asset[asset_id] = dict(snapshot)
        if self._is_stale(snapshot):
            self.repo.audit(
                "paper_engine", "paper_snapshot_rejected", "blocked", "STALE_MARKET_DATA",
                {"snapshot_id": snapshot.get("id")},
            )
            return result

        closed_rule_ids: set[int] = set()
        bid = self._valid_probability(snapshot.get("best_bid"))
        open_deals = self.repo.open_paper_deals(asset_id=str(snapshot["asset_id"]))
        if bid is None:
            for deal in open_deals:
                self.repo.audit(
                    "paper_engine", "paper_exit_not_filled", "blocked",
                    "NO_VALID_FRESH_BID", {
                        "deal_id": deal["id"],
                        "snapshot_id": snapshot.get("id"),
                        "best_bid": snapshot.get("best_bid"),
                    },
                )
        else:
            for deal in open_deals:
                stop_loss = self._decimal(deal.get("stop_loss_price"))
                take_profit = self._decimal(deal.get("take_profit_price"))
                if snapshot.get("event_type") == "market_resolved":
                    reason = "event_resolution"
                    trigger_price = bid
                elif stop_loss is not None and bid <= stop_loss:
                    reason = "stop_loss"
                    trigger_price = stop_loss
                elif take_profit is not None and bid >= take_profit:
                    reason = "take_profit"
                    trigger_price = take_profit
                else:
                    continue

                execution_price, fill_method = self._executable_exit_price(deal, snapshot)
                if execution_price is None:
                    self.repo.audit(
                        "paper_engine", "paper_exit_not_filled", "blocked",
                        "NO_VALID_FRESH_BID", {
                            "deal_id": deal["id"],
                            "snapshot_id": snapshot.get("id"),
                            "trigger_price": str(trigger_price),
                        },
                    )
                    continue
                snapshot = self._ensure_persisted(snapshot)
                if snapshot.get("id") is None:
                    self.last_error = "BUSINESS_SNAPSHOT_PERSIST_FAILED"
                    continue
                self.repo.close_paper_deal(
                    deal,
                    snapshot,
                    reason=reason,
                    trigger_price=trigger_price,
                    exit_price=execution_price,
                    fill_method=fill_method,
                )
                closed_rule_ids.add(int(deal["live_rule_id"]))
                self.closed += 1
                result["closed"] += 1

        if snapshot.get("event_type") == "market_resolved":
            return result

        for rule in self.repo.active_paper_rules():
            decision, reason = self._entry_decision(rule, snapshot, closed_rule_ids)
            self.evaluations += 1
            result["evaluated"] += 1
            if decision == "OPEN" and snapshot.get("id") is None:
                snapshot = self._ensure_persisted(snapshot)
            if snapshot.get("id") is not None:
                self.repo.record_rule_evaluation(rule, snapshot, decision, reason)
            if decision != "OPEN" or snapshot.get("id") is None:
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
        if self._in_inactive_window(rule, snapshot):
            return "SKIP", "RULE_IN_INACTIVE_SCHEDULE"
        window_reason = self._entry_window_reason(rule, snapshot, event_id)
        if window_reason:
            return "SKIP", window_reason
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
            other = self._latest_by_asset.get(str(other_asset)) or self.repo.latest_market_snapshot(str(other_asset))
            other_ask = self._decimal(other.get("best_ask")) if other else None
            if other_ask == entry:
                return "SKIP", "BOTH_SIDES_MATCH"
        outcome = str(snapshot.get("outcome") or "").upper()
        quota_key = (
            "max_yes_entries_per_event" if outcome == "YES"
            else "max_no_entries_per_event" if outcome == "NO"
            else ""
        )
        if not quota_key:
            return "SKIP", "UNKNOWN_OUTCOME"
        quota = int(rule.get(quota_key) or 0)
        if self.repo.count_paper_entries(rule_id, event_id, outcome) >= quota:
            return "SKIP", "ENTRY_QUOTA_REACHED"
        if any(int(deal["live_rule_id"]) == rule_id for deal in self.repo.open_paper_deals()):
            return "SKIP", "OPEN_DEAL_EXISTS"
        return "OPEN", "ENTRY_PRICE_MATCHED"

    def _ensure_persisted(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("id") is not None:
            return snapshot
        stored = self.repo.store_market_snapshot(snapshot, force=True)
        if stored is None:
            return snapshot
        merged = dict(snapshot)
        merged["id"] = stored["id"]
        merged["_persisted"] = True
        return merged

    @staticmethod
    def _snapshot_datetime(snapshot: dict[str, Any]) -> datetime | None:
        raw = snapshot.get("received_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    def _entry_window_reason(
        self, rule: dict[str, Any], snapshot: dict[str, Any], event_id: str
    ) -> str | None:
        start = rule.get("entry_window_start_seconds_before_end")
        end = rule.get("entry_window_end_seconds_before_end")
        if start is None and end is None:
            return None
        sampled_at = self._snapshot_datetime(snapshot)
        try:
            event_start = int(event_id.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return "MISSING_EVENT_END_TIME"
        if sampled_at is None:
            return "MISSING_EVENT_END_TIME"
        remaining = Decimal(str(event_start + 300)) - Decimal(str(sampled_at.timestamp()))
        if remaining <= 0:
            return "EVENT_ENDED"
        if start is not None and remaining > Decimal(str(start)):
            return "BEFORE_ENTRY_WINDOW"
        if end is not None and remaining < Decimal(str(end)):
            return "AFTER_ENTRY_WINDOW"
        return None

    def _in_inactive_window(
        self, rule: dict[str, Any], snapshot: dict[str, Any]
    ) -> bool:
        raw = rule.get("inactive_windows_json") or "[]"
        try:
            windows = json.loads(str(raw)) if isinstance(raw, str) else raw
            local_now = self._snapshot_datetime(snapshot).astimezone(
                ZoneInfo(str(rule.get("schedule_timezone") or "Asia/Jerusalem"))
            )
        except (TypeError, ValueError, KeyError):
            return True
        current_second = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        for window in windows if isinstance(windows, list) else []:
            if not isinstance(window, dict) or str(window.get("status", "active")) != "active":
                continue
            try:
                day = int(window["day_of_week"])
                start = self._time_seconds(str(window["start_time"]))
                end = self._time_seconds(str(window["end_time"]))
            except (KeyError, TypeError, ValueError):
                return True
            if start <= end:
                if local_now.weekday() == day and start <= current_second < end:
                    return True
            elif (
                (local_now.weekday() == day and current_second >= start)
                or (local_now.weekday() == ((day + 1) % 7) and current_second < end)
            ):
                return True
        return False

    @staticmethod
    def _time_seconds(value: str) -> int:
        parts = [int(part) for part in value.split(":")]
        if len(parts) not in {2, 3}:
            raise ValueError(value)
        hour, minute = parts[:2]
        second = parts[2] if len(parts) == 3 else 0
        if hour not in range(24) or minute not in range(60) or second not in range(60):
            raise ValueError(value)
        return hour * 3600 + minute * 60 + second

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

    def _executable_exit_price(
        self, deal: dict[str, Any], snapshot: dict[str, Any]
    ) -> tuple[Decimal | None, str]:
        best_bid = self._valid_probability(snapshot.get("best_bid"))
        if best_bid is None:
            return None, "none"

        requested_size = self._decimal(deal.get("filled_size"))
        if requested_size is None or requested_size <= 0:
            return None, "none"

        raw_levels = snapshot.get("bids")
        if raw_levels is None and snapshot.get("bids_json"):
            try:
                raw_levels = json.loads(str(snapshot["bids_json"]))
            except (TypeError, ValueError):
                raw_levels = []

        levels: list[tuple[Decimal, Decimal]] = []
        for level in raw_levels or []:
            if not isinstance(level, dict):
                continue
            price = self._valid_probability(level.get("price"))
            size = self._decimal(level.get("size"))
            if price is not None and size is not None and size > 0:
                levels.append((price, size))

        if levels:
            remaining = requested_size
            notional = Decimal("0")
            for price, available in sorted(levels, key=lambda item: item[0], reverse=True):
                filled = min(remaining, available)
                notional += filled * price
                remaining -= filled
                if remaining <= 0:
                    return notional / requested_size, "ORDER_BOOK_VWAP"

        return best_bid, "BEST_BID_FALLBACK"

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        return result if result.is_finite() else None

    @classmethod
    def _valid_probability(cls, value: Any) -> Decimal | None:
        result = cls._decimal(value)
        return result if result is not None and Decimal("0") <= result <= Decimal("1") else None
