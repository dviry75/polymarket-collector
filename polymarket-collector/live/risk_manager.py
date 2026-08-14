from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import LiveConfig
from .repository import LiveRepository
from .strategy_repository import StrategyRepository


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reason_code: str
    message: str


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)
    return result if result.is_finite() else Decimal(default)


def _age_seconds(iso_value: str | None) -> float | None:
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()


class RiskManager:
    def __init__(self, config: LiveConfig, repo: LiveRepository):
        self.config = config
        self.repo = repo

    def _pause_nonrecoverable(self, reason: str) -> None:
        StrategyRepository(self.repo).set_pause_entries(
            True, "risk_manager", reason, owner="MACHINE", auto_recoverable=False
        )

    def check_order(self, order: dict[str, Any]) -> RiskResult:
        errors = self.config.validation_errors()
        if errors:
            return RiskResult(False, "CONFIG_INVALID", "; ".join(errors))
        if self.repo.kill_switch_active():
            return RiskResult(False, "KILL_SWITCH_ACTIVE", "Kill switch is active")
        if self.config.live_adapter != "mock" and not self.config.real_submission_armed():
            return RiskResult(False, "REAL_SUBMISSION_NOT_ARMED", "Real adapter is not fully armed")
        if self.config.live_adapter == "mock" and not self.config.live_module_enabled:
            return RiskResult(False, "LIVE_MODULE_DISABLED", "LIVE module is disabled")

        amount = _dec(order.get("requested_amount_usd"))
        if amount <= 0:
            return RiskResult(False, "INVALID_AMOUNT", "Requested amount must be positive")
        if amount > self.config.max_trade_amount_usd:
            return RiskResult(False, "TRADE_AMOUNT_CAP", "Requested amount exceeds max trade amount")

        counts = self.repo.counts()
        if counts["open_orders"] > self.config.max_open_orders:
            return RiskResult(False, "OPEN_ORDER_CAP", "Too many open LIVE orders")
        if counts["open_deals"] > self.config.max_open_deals:
            return RiskResult(False, "OPEN_DEAL_CAP", "Too many open LIVE deals")
        if counts.get("active_rules", 0) > self.config.max_active_rules:
            return RiskResult(False, "ACTIVE_RULE_CAP", "Too many active LIVE rules")
        if self.current_exposure_usd_decimal() + amount > self.config.max_total_exposure_usd:
            return RiskResult(False, "EXPOSURE_CAP", "Requested order would exceed total LIVE exposure cap")

        daily = self.repo.current_daily_limit(self._day_key(), "Asia/Jerusalem")
        realized = _dec(daily.get("realized_pnl_usd"))
        if realized <= -abs(self.config.max_daily_realized_loss_usd):
            self._pause_nonrecoverable("DAILY_LOSS_LIMIT")
            return RiskResult(False, "DAILY_LOSS_LIMIT", "Daily realized loss limit reached")
        if int(daily.get("consecutive_failed_orders") or 0) >= self.config.max_consecutive_failed_orders:
            self._pause_nonrecoverable("CONSECUTIVE_FAILED_ORDERS")
            return RiskResult(False, "CONSECUTIVE_FAILED_ORDERS", "Too many consecutive failed orders")
        if int(daily.get("consecutive_losing_deals") or 0) >= self.config.max_consecutive_losing_deals:
            self._pause_nonrecoverable("CONSECUTIVE_LOSING_DEALS")
            return RiskResult(False, "CONSECUTIVE_LOSING_DEALS", "Too many consecutive losing deals")

        market = self.repo.latest_market(str(order.get("condition_id") or "")) if order.get("condition_id") else None
        if market:
            if market.get("token_mapping_status") not in {"matched", "unknown"}:
                return RiskResult(False, "TOKEN_MAPPING_MISMATCH", "CLOB/Gamma token mapping mismatch")
            if not market.get("accepting_orders"):
                return RiskResult(False, "MARKET_NOT_ACCEPTING", "Market is not accepting orders")
            age = _age_seconds(market.get("last_update_at"))
            if age is None or age > self.config.max_market_data_age_seconds:
                return RiskResult(False, "STALE_MARKET_DATA", "Market data is stale")
            mos = _dec(market.get("min_order_size"))
            if mos > 0 and amount < mos:
                return RiskResult(False, "MIN_ORDER_SIZE", "$1/requested amount is below market minimum")
            tick = _dec(market.get("min_tick_size"))
            price = _dec(order.get("requested_price"))
            if tick > 0 and price > 0 and (price / tick) % 1 != 0:
                return RiskResult(False, "INVALID_TICK", "Requested price does not align with minimum tick")

        if order.get("order_type") == "FAK" and not self.config.partial_fills_allowed:
            return RiskResult(False, "PARTIAL_FILLS_DISABLED", "FAK partial fills are disabled by policy")

        last_recon = self.repo.get_state("last_reconciliation_at", "")
        recon_age = _age_seconds(last_recon)
        if recon_age is None or recon_age > self.config.max_reconciliation_age_seconds:
            return RiskResult(False, "STALE_RECONCILIATION", "Reconciliation is stale or has never run")
        if self.repo.get_state("live_blocked_by_reconciliation", "false").lower() == "true":
            return RiskResult(False, "RECONCILIATION_GAP", "Unresolved reconciliation gap")
        if self.config.live_adapter == "polymarket":
            if self.repo.get_state("account_identity_status", "UNVERIFIED") != "VERIFIED":
                return RiskResult(False, "ACCOUNT_IDENTITY_UNVERIFIED", "Account identity is not verified")
            user_ws_age = _age_seconds(self.repo.get_state("user_ws_last_message_at", ""))
            if user_ws_age is None or user_ws_age > self.config.max_user_state_age_seconds:
                return RiskResult(False, "STALE_USER_STATE", "User state is stale or not configured")

        return RiskResult(True, "ALLOWED", "Order passed LIVE risk checks")

    def current_exposure_usd_decimal(self) -> Decimal:
        return self.repo.current_exposure_usd()

    def current_exposure_usd(self) -> str:
        return str(self.current_exposure_usd_decimal())

    def _day_key(self) -> str:
        try:
            tz = ZoneInfo("Asia/Jerusalem")
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        return datetime.now(tz).date().isoformat()
