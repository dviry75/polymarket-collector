from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

from .order_book import decimal_value
from .repository import now_iso
from .strategy_repository import StrategyRepository


class ExitSupervisionTracker:
    """Per-position exit heartbeat and fail-closed SLA enforcement.

    The heartbeat is intentionally in memory: persisting it at the supervisor's
    250 ms cadence would turn risk monitoring into a continuous SQLite write
    workload. Durable financial responsibility remains in the position state,
    stop latch and intents. Health exposes this tracker, and every SLA failure
    is persisted as a pause plus a CRITICAL alert.
    """

    def __init__(
        self,
        repo: StrategyRepository,
        *,
        monitor_sla_seconds: float,
        waiting_sla_seconds: float,
        first_eval_sla_seconds: float = 2.0,
        stop_to_submit_sla_seconds: float = 2.0,
    ):
        self.repo = repo
        self.monitor_sla_seconds = float(monitor_sla_seconds)
        self.waiting_sla_seconds = float(waiting_sla_seconds)
        self.first_eval_sla_seconds = float(first_eval_sla_seconds)
        self.stop_to_submit_sla_seconds = float(stop_to_submit_sla_seconds)
        self.records: dict[str, dict[str, Any]] = {}
        self.alerted: set[tuple[str, str]] = set()
        # P0-E SLA timing (monotonic seconds), keyed by position_id.
        self.detected_monotonic: dict[str, float] = {}
        self.first_eval_monotonic: dict[str, float] = {}
        self.stop_latched_monotonic: dict[str, float] = {}
        self.first_sell_submit_monotonic: dict[str, float] = {}

    def mark_detected(self, position_id: str) -> None:
        self.detected_monotonic.setdefault(str(position_id), time.monotonic())

    def mark_first_eval(self, position_id: str) -> float | None:
        pid = str(position_id)
        if pid in self.first_eval_monotonic:
            return None
        now = time.monotonic()
        self.first_eval_monotonic[pid] = now
        anchor = self.detected_monotonic.get(pid)
        return None if anchor is None else max(0.0, now - anchor)

    def mark_stop_latched(self, position_id: str) -> None:
        self.stop_latched_monotonic.setdefault(str(position_id), time.monotonic())

    def mark_sell_submitted(self, position_id: str) -> float | None:
        pid = str(position_id)
        if pid in self.first_sell_submit_monotonic:
            return None
        now = time.monotonic()
        self.first_sell_submit_monotonic[pid] = now
        anchor = self.stop_latched_monotonic.get(pid)
        return None if anchor is None else max(0.0, now - anchor)

    @staticmethod
    def iso_age_seconds(value: Any) -> float | None:
        if not value:
            return None
        try:
            observed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - observed).total_seconds(),
        )

    def note(
        self,
        position: dict[str, Any],
        *,
        update: dict[str, Any] | None,
        decision: str,
    ) -> None:
        position_id = str(position["position_id"])
        now_monotonic = time.monotonic()
        observed_at = now_iso()
        record = self.records.setdefault(
            position_id,
            {
                "position_id": position_id,
                "event_id": str(position.get("event_id") or ""),
                "token_id": str(position.get("token_id") or ""),
                "first_seen_monotonic": now_monotonic,
                "first_seen_at": observed_at,
                "last_usable_book_monotonic": None,
                "last_usable_book_at": None,
                "book_source": None,
                "book_age_ms": None,
            },
        )
        record["last_supervisor_check_at"] = observed_at
        record["last_exit_decision"] = decision
        record["position_state"] = str(position.get("state") or "")
        record["remaining_shares_text"] = str(
            position.get("remaining_shares_text") or "0"
        )
        if update is not None:
            record.update(
                {
                    "last_usable_book_monotonic": now_monotonic,
                    "last_usable_book_at": observed_at,
                    "book_source": str(
                        update.get("event_type")
                        or update.get("source")
                        or "exit_supervisor_ws"
                    ),
                    "book_age_ms": update.get("exchange_age_ms"),
                }
            )

    def monitoring_sla_exceeded(self, position_id: str) -> bool:
        record = self.records.get(str(position_id))
        if record is None:
            return False
        anchor = record.get("last_usable_book_monotonic")
        if anchor is None:
            anchor = record.get("first_seen_monotonic")
        return (
            anchor is not None
            and time.monotonic() - float(anchor) > self.monitor_sla_seconds
        )

    def waiting_sellable_sla_exceeded(
        self, position: dict[str, Any]
    ) -> bool:
        if str(
            position.get("active_exit_intent_state") or ""
        ).upper() != "WAITING_SELLABLE":
            return False
        age = self.iso_age_seconds(
            position.get("active_exit_intent_created_at")
            or position.get("active_exit_intent_updated_at")
        )
        return age is not None and age > self.waiting_sla_seconds

    @staticmethod
    def unsellable_remainder(
        position: dict[str, Any], minimum: Decimal
    ) -> bool:
        remaining = (
            decimal_value(position.get("remaining_shares_text"))
            or Decimal("0")
        )
        return remaining > 0 and remaining < minimum

    def fault(
        self,
        position: dict[str, Any],
        *,
        reason: str,
        message: str,
    ) -> None:
        position_id = str(position["position_id"])
        key = (position_id, reason)
        if key in self.alerted:
            return
        self.repo.acquire_pause(
            actor="strategy_exit_supervisor",
            reason=reason,
            owner="MACHINE",
            source_event_id=str(position.get("event_id") or ""),
            source_position_id=position_id,
        )
        self.repo.alert(
            alert_type="EXIT",
            severity="CRITICAL",
            reason_code=reason,
            message=message,
            entity_type="position",
            entity_id=position_id,
        )
        self.alerted.add(key)

    def prune(self, active_position_ids: set[str]) -> None:
        for position_id in list(self.records):
            if position_id not in active_position_ids:
                self.records.pop(position_id, None)
        for mapping in (
            self.detected_monotonic,
            self.first_eval_monotonic,
            self.stop_latched_monotonic,
            self.first_sell_submit_monotonic,
        ):
            for position_id in list(mapping):
                if position_id not in active_position_ids:
                    mapping.pop(position_id, None)

    def health(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for record in self.records.values():
            public = {
                key: value
                for key, value in record.items()
                if not key.endswith("_monotonic")
            }
            anchor = record.get("last_usable_book_monotonic")
            public["seconds_since_usable_book"] = (
                round(max(0.0, now - float(anchor)), 3)
                if anchor is not None
                else None
            )
            public["monitoring_sla_seconds"] = self.monitor_sla_seconds
            result.append(public)
        return sorted(result, key=lambda item: item["position_id"])
