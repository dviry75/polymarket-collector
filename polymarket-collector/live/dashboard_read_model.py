from __future__ import annotations

import json
import math
import re
import sqlite3
import time as monotonic_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .repository import LiveRepository, row_to_dict

DISPLAY_TIMEZONE = "Asia/Jerusalem"
VERIFIED = {"VERIFIED", "RECONCILED", "DERIVED_VERIFIED"}
ACTIVE_INTENT_STATES = {
    "RESERVED", "SUBMITTING", "SUBMITTED", "LIVE", "PARTIAL",
    "RECONCILIATION_REQUIRED", "CANCEL_REQUESTED", "CANCEL_UNCERTAIN",
}
ACTIVE_POSITION_STATES = {
    "OPEN", "TP_OPEN", "EXITING", "EXIT_PENDING", "PARTIALLY_EXITED",
    "EXIT_RECONCILIATION_REQUIRED", "DUST", "REDEEM_PENDING",
}
_EVENT_RE = re.compile(r"btc-updown-5m-(\d{10})")


class DashboardQueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DateWindow:
    start_utc: datetime
    end_utc: datetime
    key: str

    def sql(self) -> tuple[str, str]:
        return (self.start_utc.isoformat(), self.end_utc.isoformat())


def decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def number_value(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def metric(
    value: Any,
    *,
    quality: str,
    unit: str | None,
    as_of: str | None,
    source: str,
    verified: bool,
    stale: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "quality": quality,
        "unit": unit,
        "as_of": as_of,
        "source": source,
        "verified": verified,
        "stale": stale,
        "reason": reason,
    }


def masked(value: Any, *, visible: int = 6) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    if len(raw) <= visible * 2:
        return "***"
    return f"{raw[:visible]}…{raw[-visible:]}"


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quality_for_timestamp(value: Any, *, stale_after_seconds: float, now: datetime) -> tuple[bool, float | None]:
    parsed = _parse_iso(value)
    if parsed is None:
        return True, None
    age = max(0.0, (now - parsed).total_seconds())
    return age > stale_after_seconds, age


def resolve_window(
    key: str,
    *,
    now: datetime | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    max_days: int = 90,
) -> DateWindow:
    tz = ZoneInfo(DISPLAY_TIMEZONE)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = local_now.date()
    normalized = (key or "today").lower()
    if normalized == "today":
        start_day, end_day = today, today + timedelta(days=1)
    elif normalized == "yesterday":
        start_day, end_day = today - timedelta(days=1), today
    elif normalized in {"3d", "7d", "30d"}:
        days = int(normalized[:-1])
        start_day, end_day = today - timedelta(days=days - 1), today + timedelta(days=1)
    elif normalized == "custom":
        if not from_date or not to_date:
            raise DashboardQueryError("custom range requires from_date and to_date")
        try:
            start_day = date.fromisoformat(from_date)
            inclusive_end = date.fromisoformat(to_date)
        except ValueError as exc:
            raise DashboardQueryError("dates must use YYYY-MM-DD") from exc
        if inclusive_end < start_day:
            raise DashboardQueryError("to_date must not precede from_date")
        end_day = inclusive_end + timedelta(days=1)
    else:
        raise DashboardQueryError("unsupported range")
    if (end_day - start_day).days > max_days:
        raise DashboardQueryError(f"range exceeds {max_days} days")
    start_local = datetime.combine(start_day, time.min, tzinfo=tz)
    end_local = datetime.combine(end_day, time.min, tzinfo=tz)
    return DateWindow(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), normalized)


class DashboardReadModel:
    def __init__(
        self,
        repo: LiveRepository,
        *,
        environment: str = "LIVE",
        execution_mode: str = "REAL_TRADING",
        market_stale_seconds: float = 5.0,
        query_timeout_seconds: float = 2.0,
    ):
        self.repo = repo
        self.environment = environment.upper()
        self.execution_mode = execution_mode.upper()
        self.market_stale_seconds = market_stale_seconds
        self.query_timeout_seconds = max(0.1, float(query_timeout_seconds))

    def _execute(self, sql: str, params: Iterable[Any], *, one: bool) -> Any:
        deadline = monotonic_time.monotonic() + self.query_timeout_seconds
        try:
            with self.repo.connect() as conn:
                conn.set_progress_handler(lambda: 1 if monotonic_time.monotonic() > deadline else 0, 10_000)
                cursor = conn.execute(sql, tuple(params))
                result = cursor.fetchone() if one else cursor.fetchall()
                conn.set_progress_handler(None, 0)
                return result
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise DashboardQueryError("dashboard query exceeded its time budget") from exc
            raise

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        return row_to_dict(self._execute(sql, params, one=True))

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [row_to_dict(row) or {} for row in self._execute(sql, params, one=False)]

    def cutover(self) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM live_dashboard_cutovers WHERE environment=?",
            (self.environment,),
        )

    def metadata(self) -> dict[str, Any]:
        cutover = self.cutover()
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "timezone": DISPLAY_TIMEZONE,
            "environment": self.environment,
            "execution_mode": self.execution_mode,
            "cutover_at": cutover.get("cutover_at") if cutover else None,
            "source": "dashboard_read_model_v1",
            "verified": bool(cutover),
        }

    def _cutover_at(self) -> str | None:
        cutover = self.cutover()
        return str(cutover["cutover_at"]) if cutover else None

    def _latest_clean_reconciliation(self) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM live_reconciliation_runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
        )

    def account_equity(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        cutover_at = self._cutover_at()
        if not cutover_at:
            unavailable = metric(None, quality="UNAVAILABLE", unit="USD", as_of=None, source="none", verified=False, reason="cutover is not configured")
            return {"cash": unavailable, "reserved": unavailable, "positions": unavailable, "claimable": unavailable, "total_equity": unavailable, "items": []}
        account = self._one(
            """
            SELECT * FROM live_account_snapshots
            WHERE environment=? AND execution_mode=?
              AND verification_status IN ('VERIFIED','RECONCILED')
              AND ingested_at>=?
            ORDER BY id DESC LIMIT 1
            """,
            (self.environment, self.execution_mode, cutover_at),
        )
        if account and decimal_value(account.get("balance_usd")) is not None:
            account_stale, _age = quality_for_timestamp(account.get("sampled_at"), stale_after_seconds=60, now=now)
            cash = metric(
                number_value(decimal_value(account.get("balance_usd"))),
                quality="STALE" if account_stale else "REAL", unit="USD",
                as_of=account.get("sampled_at"), source="account_snapshot",
                verified=True, stale=account_stale,
                reason="account snapshot older than 60 seconds" if account_stale else None,
            )
        else:
            cash = metric(None, quality="UNAVAILABLE", unit="USD", as_of=None, source="account_snapshot", verified=False, reason="no verified post-cutover account snapshot")
        intents = self._all(
            """
            SELECT i.*,
                   COALESCE((SELECT SUM(CAST(f.shares_text AS REAL)*CAST(f.price_text AS REAL))
                             FROM live_strategy_fills f WHERE f.intent_id=i.intent_id),0) AS filled_notional
            FROM live_strategy_intents i
            WHERE i.environment=? AND i.execution_mode=?
              AND i.verification_status IN ('VERIFIED','RECONCILED')
              AND COALESCE(i.ingested_at,i.created_at)>=?
              AND i.state IN ('RESERVED','SUBMITTING','SUBMITTED','LIVE','PARTIAL',
                              'RECONCILIATION_REQUIRED','CANCEL_REQUESTED','CANCEL_UNCERTAIN')
            ORDER BY i.created_at
            """,
            (self.environment, self.execution_mode, cutover_at),
        )
        reserved_value = Decimal("0")
        for intent in intents:
            cap = decimal_value(intent.get("max_spend_text")) or decimal_value(intent.get("requested_amount_text"))
            if cap is not None:
                reserved_value += max(Decimal("0"), cap - (decimal_value(intent.get("filled_notional")) or Decimal("0")))
        reconciliation = self._latest_clean_reconciliation()
        reconciliation_after_cutover = bool(reconciliation and str(reconciliation.get("finished_at") or "") >= cutover_at)
        reserved = metric(
            number_value(reserved_value) if intents or reconciliation_after_cutover else None,
            quality="REAL" if intents or reconciliation_after_cutover else "UNAVAILABLE",
            unit="USD", as_of=reconciliation.get("finished_at") if reconciliation else None,
            source="verified_open_intents", verified=bool(intents or reconciliation_after_cutover),
            reason=None if intents or reconciliation_after_cutover else "no post-cutover reconciliation proves zero open orders",
        )
        position_rows = self.open_positions(now=now)["items"]
        values = [decimal_value(item.get("conservative_value_usd")) for item in position_rows]
        positions_available = all(value is not None for value in values) and bool(position_rows or reconciliation_after_cutover)
        positions_value = sum((value or Decimal("0") for value in values), Decimal("0")) if positions_available else None
        positions_metric = metric(
            number_value(positions_value),
            quality="REAL" if positions_available else "UNAVAILABLE",
            unit="USD", as_of=max((str(item.get("price_as_of") or "") for item in position_rows), default=reconciliation.get("finished_at") if reconciliation else None),
            source="sellable_shares_x_best_bid", verified=positions_available,
            reason=None if positions_available else "best bid missing or stale",
        )
        claimable_rows = [item for item in position_rows if item.get("state") == "REDEEM_PENDING"]
        claimable_values = [decimal_value(item.get("claimable_value_usd")) for item in claimable_rows]
        claimable_available = bool(reconciliation_after_cutover) and all(value is not None for value in claimable_values)
        claimable_value = sum((value or Decimal("0") for value in claimable_values), Decimal("0")) if claimable_available else None
        claimable = metric(
            number_value(claimable_value), quality="REAL" if claimable_available else "UNAVAILABLE",
            unit="USD", as_of=reconciliation.get("finished_at") if reconciliation else None,
            source="verified_redeem_pending_positions", verified=claimable_available,
            reason=None if claimable_available else "claimable value is not verified post-cutover",
        )
        components = [cash, reserved, positions_metric, claimable]
        if all(component["value"] is not None and component["quality"] == "REAL" for component in components):
            total = sum((Decimal(str(component["value"])) for component in components), Decimal("0"))
            total_equity = metric(number_value(total), quality="REAL", unit="USD", as_of=self.metadata()["as_of"], source="cash+reserved+positions+claimable", verified=True)
        else:
            total_equity = metric(None, quality="UNAVAILABLE", unit="USD", as_of=self.metadata()["as_of"], source="cash+reserved+positions+claimable", verified=False, reason="one or more equity components are unavailable or stale")
        return {"cash": cash, "reserved": reserved, "positions": positions_metric, "claimable": claimable, "total_equity": total_equity, "items": position_rows}

    def open_positions(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        cutover_at = self._cutover_at()
        if not cutover_at:
            return {"items": [], "quality": "UNAVAILABLE", "reason": "cutover is not configured"}
        rows = self._all(
            """
            SELECT p.*,
                   (SELECT s.best_bid FROM live_market_snapshots s
                    WHERE s.asset_id=p.token_id ORDER BY s.id DESC LIMIT 1) AS current_best_bid,
                   (SELECT COALESCE(s.market_timestamp,s.received_at) FROM live_market_snapshots s
                    WHERE s.asset_id=p.token_id ORDER BY s.id DESC LIMIT 1) AS price_as_of
            FROM live_strategy_positions p
            WHERE p.environment=? AND p.execution_mode=?
              AND p.verification_status IN ('VERIFIED','RECONCILED','DERIVED_VERIFIED')
              AND COALESCE(p.ingested_at,p.created_at)>=?
              AND p.state IN ('OPEN','TP_OPEN','EXITING','EXIT_PENDING','PARTIALLY_EXITED',
                              'EXIT_RECONCILIATION_REQUIRED','DUST','REDEEM_PENDING')
            ORDER BY p.created_at DESC
            """,
            (self.environment, self.execution_mode, cutover_at),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            sellable = decimal_value(row.get("sellable_shares_text"))
            remaining = decimal_value(row.get("remaining_shares_text"))
            acquired = decimal_value(row.get("acquired_shares_text"))
            cost = decimal_value(row.get("cost_all_in_text"))
            best_bid = decimal_value(row.get("current_best_bid"))
            stale, age = quality_for_timestamp(row.get("price_as_of"), stale_after_seconds=self.market_stale_seconds, now=now)
            claimable_position = row.get("state") == "REDEEM_PENDING" and int(row.get("resolved_winner") or 0) == 1
            value = Decimal("0") if claimable_position else (sellable * best_bid if sellable is not None and best_bid is not None and not stale else None)
            attributed_cost = cost * remaining / acquired if cost is not None and remaining is not None and acquired and acquired > 0 else None
            unrealized = value - attributed_cost if value is not None and attributed_cost is not None else None
            claimable_value = remaining if claimable_position else None
            items.append({
                "position_id": masked(row.get("position_id")), "event_id": masked(row.get("event_id")),
                "outcome": row.get("outcome"), "state": row.get("state"),
                "remaining_shares": number_value(remaining), "sellable_shares": number_value(sellable),
                "average_entry_price": number_value(decimal_value(row.get("average_entry_price_text"))),
                "remaining_attributed_cost_usd": number_value(attributed_cost),
                "best_bid": number_value(best_bid), "price_as_of": row.get("price_as_of"),
                "price_age_seconds": age, "stale": stale,
                "conservative_value_usd": number_value(value),
                "unrealized_pnl_usd": number_value(unrealized),
                "claimable_value_usd": number_value(claimable_value),
                "quality": "REAL" if claimable_position else ("STALE" if stale else ("REAL" if value is not None else "UNAVAILABLE")),
                "verified": row.get("verification_status") in VERIFIED,
                "execution_mode": row.get("execution_mode"), "source": row.get("provenance_source"),
            })
        reconciliation = self._latest_clean_reconciliation()
        proved_empty = bool(reconciliation and str(reconciliation.get("finished_at") or "") >= cutover_at)
        quality = "REAL" if items or proved_empty else "UNAVAILABLE"
        return {"items": items, "quality": quality, "reason": None if quality == "REAL" else "no post-cutover reconciliation proves zero positions"}

    def open_orders(self, *, page: int = 1, page_size: int = 25) -> dict[str, Any]:
        page_size = max(1, min(page_size, 100)); page = max(1, page)
        cutover_at = self._cutover_at()
        if not cutover_at:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "quality": "UNAVAILABLE"}
        params = (self.environment, self.execution_mode, cutover_at)
        count = self._one(
            """SELECT COUNT(*) AS total FROM live_strategy_intents
               WHERE environment=? AND execution_mode=? AND COALESCE(ingested_at,created_at)>=?
                 AND verification_status IN ('VERIFIED','RECONCILED')
                 AND state IN ('RESERVED','SUBMITTING','SUBMITTED','LIVE','PARTIAL','RECONCILIATION_REQUIRED','CANCEL_REQUESTED','CANCEL_UNCERTAIN')""",
            params,
        ) or {"total": 0}
        rows = self._all(
            """SELECT * FROM live_strategy_intents
               WHERE environment=? AND execution_mode=? AND COALESCE(ingested_at,created_at)>=?
                 AND verification_status IN ('VERIFIED','RECONCILED')
                 AND state IN ('RESERVED','SUBMITTING','SUBMITTED','LIVE','PARTIAL','RECONCILIATION_REQUIRED','CANCEL_REQUESTED','CANCEL_UNCERTAIN')
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        )
        items = [{
            "intent_id": masked(row.get("intent_id")), "order_id": masked(row.get("remote_order_id")),
            "event_id": masked(row.get("event_id")), "side": row.get("side"), "action": row.get("action"),
            "purpose": row.get("purpose"), "state": row.get("state"), "order_type": row.get("order_type"),
            "requested_amount_usd": number_value(decimal_value(row.get("requested_amount_text"))),
            "requested_shares": number_value(decimal_value(row.get("requested_shares_text"))),
            "filled_shares": number_value(decimal_value(row.get("filled_shares_text"))),
            "limit_price": number_value(decimal_value(row.get("price_limit_text"))),
            "created_at": row.get("created_at"), "quality": "REAL", "verified": True,
        } for row in rows]
        total = int(count.get("total") or 0)
        reconciliation = self._latest_clean_reconciliation()
        proved_empty = bool(reconciliation and str(reconciliation.get("finished_at") or "") >= cutover_at)
        quality = "REAL" if total > 0 or proved_empty else "UNAVAILABLE"
        return {"items": items, "page": page, "page_size": page_size, "total": total, "quality": quality, "reason": None if quality == "REAL" else "no post-cutover reconciliation proves zero open orders"}

    def pnl_summary(self, window: DateWindow) -> dict[str, Any]:
        cutover_at = self._cutover_at()
        if not cutover_at:
            return {"quality": "UNAVAILABLE", "reason": "cutover is not configured"}
        start, end = window.sql(); start = max(start, cutover_at)
        rows = self._all(
            """SELECT realized_pnl_text,total_fees_text,fee_verification_status,fee_source,state,closed_at,final_reason
               FROM live_strategy_deals
               WHERE environment=? AND execution_mode=?
                 AND verification_status IN ('VERIFIED','RECONCILED','DERIVED_VERIFIED')
                 AND closed_at>=? AND closed_at<?""",
            (self.environment, self.execution_mode, start, end),
        )
        realized_values = [decimal_value(row.get("realized_pnl_text")) for row in rows]
        fee_values = [decimal_value(row.get("total_fees_text")) for row in rows]
        fees_complete = all(value is not None and row.get("fee_verification_status") == "VERIFIED" for row, value in zip(rows, fee_values))
        realized = sum((value or Decimal("0") for value in realized_values), Decimal("0"))
        fees = sum((value or Decimal("0") for value in fee_values), Decimal("0")) if fees_complete else None
        wins = sum(1 for value in realized_values if value is not None and value > 0)
        losses = sum(1 for value in realized_values if value is not None and value < 0)
        decided = wins + losses
        gross_profit = sum((value for value in realized_values if value is not None and value > 0), Decimal("0"))
        gross_loss = sum((-value for value in realized_values if value is not None and value < 0), Decimal("0"))
        return {
            "quality": "REAL" if fees_complete else "PARTIAL", "reason": None if fees_complete else "one or more verified fees are missing",
            "realized_pnl_usd": number_value(realized), "fees_usd": number_value(fees),
            "trade_count": len(rows), "wins": wins, "losses": losses,
            "win_rate_percent": (wins / decided * 100) if decided else None,
            "average_win_usd": number_value(gross_profit / wins) if wins else None,
            "average_loss_usd": number_value(gross_loss / losses) if losses else None,
            "profit_factor": number_value(gross_profit / gross_loss) if gross_loss else None,
            "timezone": DISPLAY_TIMEZONE, "range": window.key, "from": start, "to": end,
        }

    def pnl_timeseries(self, window: DateWindow) -> dict[str, Any]:
        start, end = window.sql(); cutover_at = self._cutover_at()
        if not cutover_at:
            return {"items": [], "quality": "UNAVAILABLE"}
        start = max(start, cutover_at)
        rows = self._all(
            """SELECT closed_at,realized_pnl_text,total_fees_text
               FROM live_strategy_deals
               WHERE environment=? AND execution_mode=?
                 AND verification_status IN ('VERIFIED','RECONCILED','DERIVED_VERIFIED')
                 AND closed_at>=? AND closed_at<? ORDER BY closed_at""",
            (self.environment, self.execution_mode, start, end),
        )
        tz = ZoneInfo(DISPLAY_TIMEZONE); buckets: dict[str, dict[str, Any]] = {}; cumulative = Decimal("0")
        for row in rows:
            stamp = _parse_iso(row.get("closed_at")); pnl = decimal_value(row.get("realized_pnl_text"))
            if stamp is None or pnl is None:
                continue
            key = stamp.astimezone(tz).date().isoformat(); cumulative += pnl
            bucket = buckets.setdefault(key, {"day": key, "realized_pnl_usd": 0.0, "trades": 0, "wins": 0, "losses": 0})
            bucket["realized_pnl_usd"] += float(pnl); bucket["trades"] += 1
            bucket["wins" if pnl > 0 else "losses" if pnl < 0 else "trades"] += 1 if pnl != 0 else 0
            bucket["cumulative_pnl_usd"] = float(cumulative)
        return {"items": list(buckets.values()), "quality": "REAL", "timezone": DISPLAY_TIMEZONE}

    def trade_history(self, window: DateWindow, *, page: int = 1, page_size: int = 25) -> dict[str, Any]:
        page_size = max(1, min(page_size, 100)); page = max(1, page)
        cutover_at = self._cutover_at()
        if not cutover_at:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "quality": "UNAVAILABLE"}
        start, end = window.sql(); start = max(start, cutover_at)
        params = (self.environment, self.execution_mode, start, end)
        count = self._one(
            """SELECT COUNT(*) total FROM live_strategy_deals WHERE environment=? AND execution_mode=?
               AND verification_status IN ('VERIFIED','RECONCILED','DERIVED_VERIFIED')
               AND COALESCE(closed_at,opened_at,created_at)>=? AND COALESCE(closed_at,opened_at,created_at)<?""", params,
        ) or {"total": 0}
        rows = self._all(
            """SELECT * FROM live_strategy_deals WHERE environment=? AND execution_mode=?
               AND verification_status IN ('VERIFIED','RECONCILED','DERIVED_VERIFIED')
               AND COALESCE(closed_at,opened_at,created_at)>=? AND COALESCE(closed_at,opened_at,created_at)<?
               ORDER BY COALESCE(closed_at,opened_at,created_at) DESC LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        )
        items = [{
            "deal_id": masked(row.get("deal_id")), "event_id": masked(row.get("event_id")),
            "outcome": row.get("outcome"), "state": row.get("state"), "opened_at": row.get("opened_at"),
            "closed_at": row.get("closed_at"), "realized_pnl_usd": number_value(decimal_value(row.get("realized_pnl_text"))),
            "fees_usd": number_value(decimal_value(row.get("total_fees_text"))), "final_reason": row.get("final_reason"),
            "quality": "REAL" if row.get("fee_verification_status") == "VERIFIED" else "PARTIAL", "verified": True,
            "fee_status": row.get("fee_verification_status"), "fee_source": row.get("fee_source"),
        } for row in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": int(count.get("total") or 0), "quality": "REAL"}

    def markets(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        rows = self._all(
            """SELECT event_id,yes_best_bid,yes_best_ask,no_best_bid,no_best_ask,
                      market_timestamp,market_received_at,accepting_orders,market_resolved,
                      verification_status,environment,execution_mode,provenance_source
               FROM live_markets WHERE event_id LIKE 'btc-updown-5m-%'
               ORDER BY updated_at DESC LIMIT 250"""
        )
        candidates: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            match = _EVENT_RE.search(str(row.get("event_id") or ""))
            if match:
                candidates.append((int(match.group(1)), row))
        current_epoch = int(now.timestamp())
        current = next((item for epoch, item in candidates if epoch <= current_epoch < epoch + 300), None)
        future = sorted(((epoch, item) for epoch, item in candidates if epoch > current_epoch), key=lambda value: value[0])
        next_item = future[0][1] if future else None
        def present(row: dict[str, Any] | None) -> dict[str, Any] | None:
            if row is None:
                return None
            match = _EVENT_RE.search(str(row.get("event_id") or "")); start = int(match.group(1)) if match else None
            as_of = row.get("market_timestamp") or row.get("market_received_at")
            stale, age = quality_for_timestamp(as_of, stale_after_seconds=self.market_stale_seconds, now=now)
            verified = row.get("verification_status") in VERIFIED and row.get("environment") == self.environment
            return {
                "event_id": masked(row.get("event_id")), "starts_at": datetime.fromtimestamp(start, timezone.utc).isoformat() if start else None,
                "ends_at": datetime.fromtimestamp(start + 300, timezone.utc).isoformat() if start else None,
                "yes": {"best_bid": row.get("yes_best_bid"), "best_ask": row.get("yes_best_ask")},
                "no": {"best_bid": row.get("no_best_bid"), "best_ask": row.get("no_best_ask")},
                "accepting_orders": bool(row.get("accepting_orders")), "resolved": bool(row.get("market_resolved")),
                "as_of": as_of, "age_seconds": age, "stale": stale,
                "quality": "STALE" if stale else ("REAL" if verified else "PARTIAL"),
                "verified": verified, "source": row.get("provenance_source") or "market_store",
            }
        return {"current": present(current), "next": present(next_item), "quality": "REAL" if current else "UNAVAILABLE"}

    def recent_activity(self, *, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 100)); cutover_at = self._cutover_at()
        if not cutover_at:
            return {"items": [], "quality": "UNAVAILABLE"}
        rows = self._all(
            """SELECT occurred_at,severity,category,component,requested_action,reason_code,
                      result_status,verification_status,provenance_source
               FROM live_audit_timeline INDEXED BY idx_live_timeline_time
               WHERE environment=? AND execution_mode=? AND ingested_at>=?
               ORDER BY id DESC LIMIT ?""",
            (self.environment, self.execution_mode, cutover_at, limit),
        )
        return {"items": [{
            "occurred_at": row.get("occurred_at"), "severity": row.get("severity"),
            "category": row.get("category"), "component": row.get("component"),
            "action": row.get("requested_action"), "reason": row.get("reason_code"),
            "result": row.get("result_status"), "quality": "REAL" if row.get("verification_status") in VERIFIED else "PARTIAL",
            "source": row.get("provenance_source"),
        } for row in rows], "quality": "REAL"}

    def alerts(self) -> dict[str, Any]:
        rows = self._all(
            """SELECT id,severity,alert_type,reason_code,message,first_seen_at,last_seen_at,occurrence_count
               FROM live_alerts WHERE active=1 ORDER BY id DESC LIMIT 100"""
        )
        return {"items": rows, "quality": "REAL"}

    def health(self, trader_status: dict[str, Any] | None = None) -> dict[str, Any]:
        state_rows = self._all(
            "SELECT key,value,updated_at FROM live_system_state WHERE key IN ("
            "'kill_switch','pause_entries','strategy_readiness','strategy_block_reason',"
            "'reconciliation_readiness','reconciliation_block_reason','order_heartbeat_status',"
            "'market_ws_status','user_ws_status','last_successful_reconciliation_at',"
            "'pause_state','pause_owner','pause_cause','release_policy','pause_generation',"
            "'pause_acquired_at','recovery_status','recovery_engine_status',"
            "'recovery_blockers_json','pause_eligible_since','recovery_last_action',"
            "'recovery_last_result','last_auto_recovery_at')"
        )
        states = {str(row["key"]): {"value": row["value"], "updated_at": row["updated_at"]} for row in state_rows}
        trader_available = bool(trader_status)
        return {
            "trader_service": "RUNNING" if trader_available else "STOPPED",
            "dashboard_service": "RUNNING",
            "kill_switch": states.get("kill_switch", {}).get("value") == "true",
            "pause_entries": states.get("pause_entries", {}).get("value") == "true",
            "strategy_readiness": states.get("strategy_readiness", {}).get("value", "UNKNOWN") if trader_available else "STOPPED",
            "strategy_block_reason": states.get("strategy_block_reason", {}).get("value", ""),
            "reconciliation_readiness": states.get("reconciliation_readiness", {}).get("value", "UNKNOWN") if trader_available else "STALE",
            "reconciliation_block_reason": states.get("reconciliation_block_reason", {}).get("value", ""),
            "order_heartbeat": states.get("order_heartbeat_status", {}).get("value", "UNKNOWN") if trader_available else "STOPPED",
            "market_websocket": states.get("market_ws_status", {}).get("value", "UNKNOWN") if trader_available else "STOPPED",
            "user_websocket": states.get("user_ws_status", {}).get("value", "UNKNOWN") if trader_available else "STOPPED",
            "last_reconciliation": states.get("last_successful_reconciliation_at", {}).get("value"),
            "auto_recovery": (
                (trader_status or {}).get("recovery")
                or {
                    "auto_recovery_status": states.get("recovery_status", {}).get("value", "UNKNOWN"),
                    "engine_status": states.get("recovery_engine_status", {}).get("value", "UNKNOWN"),
                    "pause_state": states.get("pause_state", {}).get("value", "UNKNOWN"),
                    "owner": states.get("pause_owner", {}).get("value", "NONE"),
                    "cause": states.get("pause_cause", {}).get("value", ""),
                    "release_policy": states.get("release_policy", {}).get("value", "MANUAL_ONLY"),
                    "generation": int(states.get("pause_generation", {}).get("value", "0") or 0),
                    "acquired_at": states.get("pause_acquired_at", {}).get("value", ""),
                    "eligible_since": states.get("pause_eligible_since", {}).get("value", ""),
                    "current_blockers": json.loads(
                        states.get("recovery_blockers_json", {}).get("value", "[]")
                    ),
                    "last_recovery_action": states.get("recovery_last_action", {}).get("value", ""),
                    "last_recovery_result": states.get("recovery_last_result", {}).get("value", ""),
                    "last_auto_recovery_at": states.get("last_auto_recovery_at", {}).get("value", ""),
                }
            ),
            "quality": "REAL", "as_of": self.metadata()["as_of"],
        }

    def freshness(self, trader_status: dict[str, Any] | None = None) -> dict[str, Any]:
        health = self.health(trader_status)
        now = datetime.now(timezone.utc)
        sources = {
            "account": self._one("SELECT sampled_at AS as_of FROM live_account_snapshots ORDER BY id DESC LIMIT 1"),
            "market": self._one("SELECT COALESCE(market_timestamp,market_received_at,updated_at) AS as_of FROM live_markets ORDER BY updated_at DESC LIMIT 1"),
            "reconciliation": {"as_of": health.get("last_reconciliation")},
        }
        limits = {"account": 60.0, "market": self.market_stale_seconds, "reconciliation": 30.0}
        items: dict[str, Any] = {}
        for name, row in sources.items():
            as_of = (row or {}).get("as_of")
            stale, age = quality_for_timestamp(as_of, stale_after_seconds=limits[name], now=now)
            available = age is not None
            items[name] = {
                "as_of": as_of, "freshness_seconds": age,
                "stale": stale if available else True,
                "quality": "STALE" if available and stale else "REAL" if available else "UNAVAILABLE",
                "verified": available, "source": f"{name}_state",
            }
        if not trader_status:
            for name in ("market", "reconciliation"):
                items[name]["stale"] = True
                items[name]["quality"] = "STALE" if items[name]["as_of"] else "UNAVAILABLE"
        quality = (
            "REAL" if all(item["quality"] == "REAL" for item in items.values())
            else "STALE" if any(item["quality"] == "STALE" for item in items.values())
            else "UNAVAILABLE"
        )
        return {"items": items, "quality": quality, "as_of": now.isoformat(), "stale": quality != "REAL"}

    def overview(self, window: DateWindow, *, trader_status: dict[str, Any] | None = None) -> dict[str, Any]:
        account = self.account_equity(); pnl = self.pnl_summary(window)
        cutover_at = self._cutover_at()
        cumulative = self.pnl_summary(DateWindow(_parse_iso(cutover_at) or window.start_utc, datetime.now(timezone.utc) + timedelta(seconds=1), "since_cutover")) if cutover_at else {"quality": "UNAVAILABLE"}
        positions = self.open_positions(); orders = self.open_orders(page_size=10)
        return {
            "meta": self.metadata(), "account": account, "pnl": pnl, "pnl_cumulative": cumulative,
            "positions": positions, "orders": orders, "markets": self.markets(),
            "activity": self.recent_activity(limit=10), "alerts": self.alerts(),
            "health": self.health(trader_status),
        }
