"""Immutable STOP obligations passed from market data to the latch worker."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decimal_text(value: Any) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format(parsed, "f") if parsed.is_finite() else ""


@dataclass(frozen=True, slots=True)
class StopObligation:
    position_id: str
    event_id: str
    condition_id: str
    token_id: str
    trigger_id: str
    frame_hash: str
    best_bid: str
    best_bid_size: str
    generation: int
    update_number: int
    exchange_timestamp_ms: int
    readiness_reason: str
    created_monotonic_ns: int

    @classmethod
    def create(
        cls,
        *,
        position: Mapping[str, Any],
        update: Mapping[str, Any],
        readiness: Mapping[str, Any],
        frame_hash: str,
        stop_price: Decimal,
    ) -> "StopObligation | None":
        position_id = str(position.get("position_id") or "")
        token_id = str(position.get("token_id") or update.get("asset_id") or "")
        update_token = str(update.get("asset_id") or "")
        bid_text = _decimal_text(update.get("best_bid"))
        bid = Decimal(bid_text) if bid_text else None
        generation = _int(update.get("generation"))
        update_number = _int(update.get("update_number"))
        ready = bool(readiness.get("ready"))
        reason = str(readiness.get("reason") or ("READY" if ready else "NOT_READY"))
        book_ready = update.get("book_ready")
        if (
            not position_id
            or not token_id
            or token_id != update_token
            or bid is None
            or bid > stop_price
            or not ready
            or reason != "READY"
            or book_ready is False
            or generation <= 0
            or update_number <= 0
        ):
            return None
        return cls(
            position_id=position_id,
            event_id=str(position.get("event_id") or update.get("event_id") or ""),
            condition_id=str(
                position.get("condition_id") or update.get("condition_id") or ""
            ),
            token_id=token_id,
            trigger_id=str(update.get("_critical_trigger_id") or ""),
            frame_hash=str(frame_hash or update.get("message_hash") or ""),
            best_bid=bid_text,
            best_bid_size=_decimal_text(update.get("best_bid_size")),
            generation=generation,
            update_number=update_number,
            exchange_timestamp_ms=_int(update.get("exchange_timestamp_ms")),
            readiness_reason=reason,
            created_monotonic_ns=time.perf_counter_ns(),
        )

    def as_update(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "condition_id": self.condition_id,
            "asset_id": self.token_id,
            "best_bid": self.best_bid,
            "best_bid_size": self.best_bid_size,
            "generation": self.generation,
            "update_number": self.update_number,
            "exchange_timestamp_ms": self.exchange_timestamp_ms,
            "message_hash": self.frame_hash,
            "book_ready": True,
            "_critical_stop_latched": True,
            "_critical_trigger_id": self.trigger_id,
            "_stop_forensic_integrity_status": self.readiness_reason,
        }
