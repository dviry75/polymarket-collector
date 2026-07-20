import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


SECRET_MARKERS = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "AUTH", "SIGNATURE")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _decimal_env(name: str, default: str) -> Decimal:
    raw = _env(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal(default)
    return value if value.is_finite() else Decimal(default)


@dataclass(frozen=True)
class LiveConfig:
    trading_mode: str = "DEMO"
    live_module_enabled: bool = False
    live_trading_enabled: bool = False
    live_order_submission_enabled: bool = False
    live_adapter: str = "mock"
    live_kill_switch_default: bool = True
    default_trade_amount_usd: Decimal = Decimal("1")
    max_trade_amount_usd: Decimal = Decimal("1")
    max_total_exposure_usd: Decimal = Decimal("3")
    max_open_deals: int = 3
    max_open_orders: int = 3
    entry_order_type: str = "FOK"
    partial_fills_allowed: bool = False
    max_entry_slippage: Decimal = Decimal("0.01")
    max_exit_slippage: Decimal = Decimal("0.02")
    redemption_mode: str = "manual"
    adapter_scenario: str = "filled"
    operator_token: str = ""
    clob_host: str = "https://clob.polymarket.com"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    max_market_data_age_seconds: int = 15
    max_reconciliation_age_seconds: int = 300

    @classmethod
    def from_env(cls) -> "LiveConfig":
        return cls(
            trading_mode=_env("TRADING_MODE", "DEMO").upper(),
            live_module_enabled=_bool_env("LIVE_MODULE_ENABLED", False),
            live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
            live_order_submission_enabled=_bool_env("LIVE_ORDER_SUBMISSION_ENABLED", False),
            live_adapter=_env("LIVE_ADAPTER", "mock").lower(),
            live_kill_switch_default=_bool_env("LIVE_KILL_SWITCH", True),
            default_trade_amount_usd=_decimal_env("LIVE_DEFAULT_TRADE_AMOUNT_USD", "1"),
            max_trade_amount_usd=_decimal_env("LIVE_MAX_TRADE_AMOUNT_USD", "1"),
            max_total_exposure_usd=_decimal_env("LIVE_MAX_TOTAL_EXPOSURE_USD", "3"),
            max_open_deals=int(_env("LIVE_MAX_OPEN_DEALS", "3") or "3"),
            max_open_orders=int(_env("LIVE_MAX_OPEN_ORDERS", "3") or "3"),
            entry_order_type=_env("LIVE_ENTRY_ORDER_TYPE", "FOK").upper(),
            partial_fills_allowed=_bool_env("LIVE_PARTIAL_FILLS_ALLOWED", False),
            max_entry_slippage=_decimal_env("LIVE_MAX_ENTRY_SLIPPAGE", "0.01"),
            max_exit_slippage=_decimal_env("LIVE_MAX_EXIT_SLIPPAGE", "0.02"),
            redemption_mode=_env("LIVE_REDEMPTION_MODE", "manual").lower(),
            adapter_scenario=_env("LIVE_MOCK_SCENARIO", "filled").lower(),
            operator_token=_env("LIVE_OPERATOR_TOKEN", ""),
            clob_host=_env("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
            market_ws_url=_env("POLYMARKET_MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            user_ws_url=_env("POLYMARKET_USER_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/user"),
            max_market_data_age_seconds=int(_env("LIVE_MAX_MARKET_DATA_AGE_SECONDS", "15") or "15"),
            max_reconciliation_age_seconds=int(_env("LIVE_MAX_RECONCILIATION_AGE_SECONDS", "300") or "300"),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.trading_mode not in {"DEMO", "LIVE"}:
            errors.append("TRADING_MODE must be DEMO or LIVE")
        if self.live_adapter not in {"mock", "polymarket"}:
            errors.append("LIVE_ADAPTER must be mock or polymarket")
        if self.entry_order_type not in {"FOK", "FAK", "GTC", "GTD"}:
            errors.append("LIVE_ENTRY_ORDER_TYPE must be FOK, FAK, GTC, or GTD")
        if self.redemption_mode not in {"manual", "approval", "automatic"}:
            errors.append("LIVE_REDEMPTION_MODE must be manual, approval, or automatic")
        if self.default_trade_amount_usd <= 0:
            errors.append("LIVE_DEFAULT_TRADE_AMOUNT_USD must be positive")
        if self.max_trade_amount_usd <= 0:
            errors.append("LIVE_MAX_TRADE_AMOUNT_USD must be positive")
        if self.default_trade_amount_usd > self.max_trade_amount_usd:
            errors.append("default trade amount exceeds max trade amount")
        return errors

    def real_submission_armed(self) -> bool:
        return (
            self.trading_mode == "LIVE"
            and self.live_module_enabled
            and self.live_trading_enabled
            and self.live_order_submission_enabled
            and self.live_adapter == "polymarket"
        )

    def safe_public_dict(self) -> dict[str, object]:
        return {
            "trading_mode": self.trading_mode,
            "live_module_enabled": self.live_module_enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "live_order_submission_enabled": self.live_order_submission_enabled,
            "live_adapter": self.live_adapter,
            "live_kill_switch_default": self.live_kill_switch_default,
            "default_trade_amount_usd": str(self.default_trade_amount_usd),
            "max_trade_amount_usd": str(self.max_trade_amount_usd),
            "max_total_exposure_usd": str(self.max_total_exposure_usd),
            "max_open_deals": self.max_open_deals,
            "max_open_orders": self.max_open_orders,
            "entry_order_type": self.entry_order_type,
            "partial_fills_allowed": self.partial_fills_allowed,
            "max_entry_slippage": str(self.max_entry_slippage),
            "max_exit_slippage": str(self.max_exit_slippage),
            "redemption_mode": self.redemption_mode,
            "operator_auth_configured": bool(self.operator_token),
            "real_submission_armed": self.real_submission_armed(),
        }


def redact(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def redact_mapping(mapping: dict[str, object], secret_markers: Iterable[str] = SECRET_MARKERS) -> dict[str, object]:
    redacted: dict[str, object] = {}
    markers = tuple(marker.upper() for marker in secret_markers)
    for key, value in mapping.items():
        redacted[key] = redact(value) if any(marker in key.upper() for marker in markers) else value
    return redacted

