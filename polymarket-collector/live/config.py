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
    execution_mode: str = "READ_ONLY"
    paper_trading_enabled: bool = False
    market_ws_enabled: bool = False
    market_discovery_interval_seconds: int = 5
    paper_taker_fee_rate: Decimal = Decimal("0.07")
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
    max_active_rules: int = 1
    max_daily_realized_loss_usd: Decimal = Decimal("10")
    max_consecutive_failed_orders: int = 3
    max_consecutive_losing_deals: int = 5
    entry_order_type: str = "FOK"
    partial_fills_allowed: bool = False
    max_entry_slippage: Decimal = Decimal("0.01")
    stop_loss_order_type: str = "FAK"
    stop_loss_initial_slippage: Decimal = Decimal("0.02")
    max_exit_slippage: Decimal = Decimal("0.05")
    max_stop_loss_attempts: int = 3
    stop_loss_retry_delay_ms: int = 500
    redemption_mode: str = "manual"
    adapter_scenario: str = "filled"
    operator_token: str = ""
    login_username: str = "Admin@system.com"
    login_password_hash: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = 0
    login_rate_limit_per_minute: int = 5
    public_base_url: str = "https://live-poly.dvirtechnologies.com"
    live_db_path: str = "/opt/polymarket-btc-live/poly_live.sqlite3"
    backup_dir: str = "/opt/polymarket-btc-live/backups"
    backup_retention_days: int = 7
    backup_max_total_bytes: int = 1_073_741_824
    backup_warning_threshold_percent: int = 80
    ws_raw_events_enabled: bool = False
    ws_event_retention_hours: int = 48
    snapshot_min_interval_ms: int = 1000
    snapshot_save_only_on_change: bool = True
    snapshot_raw_payload_enabled: bool = False
    snapshot_retention_days: int = 30
    technical_audit_retention_days: int = 14
    retention_interval_seconds: int = 3600
    retention_batch_size: int = 2000
    disk_warning_percent: int = 70
    disk_critical_percent: int = 80
    disk_emergency_percent: int = 90
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    user_ws_enabled: bool = True
    max_market_data_age_seconds: int = 5
    max_user_state_age_seconds: int = 15
    max_reconciliation_age_seconds: int = 30
    reconciliation_interval_seconds: int = 15
    profile_address: str = ""
    account_login_type: str = "email"
    signer_address: str = ""
    funder_address: str = ""
    signature_type: int = 1
    google_project_id: str = ""
    google_secret_prefix: str = ""

    @classmethod
    def from_env(cls) -> "LiveConfig":
        return cls(
            trading_mode=_env("TRADING_MODE", "DEMO").upper(),
            execution_mode=_env("LIVE_EXECUTION_MODE", "READ_ONLY").upper(),
            paper_trading_enabled=_bool_env("LIVE_PAPER_TRADING_ENABLED", False),
            market_ws_enabled=_bool_env("POLYMARKET_MARKET_WS_ENABLED", False),
            market_discovery_interval_seconds=int(_env("LIVE_MARKET_DISCOVERY_INTERVAL_SECONDS", "5") or "5"),
            paper_taker_fee_rate=_decimal_env("LIVE_PAPER_TAKER_FEE_RATE", "0.07"),
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
            max_active_rules=int(_env("LIVE_MAX_ACTIVE_RULES", "1") or "1"),
            max_daily_realized_loss_usd=_decimal_env("LIVE_MAX_DAILY_REALIZED_LOSS_USD", "10"),
            max_consecutive_failed_orders=int(_env("LIVE_MAX_CONSECUTIVE_FAILED_ORDERS", "3") or "3"),
            max_consecutive_losing_deals=int(_env("LIVE_MAX_CONSECUTIVE_LOSING_DEALS", "5") or "5"),
            entry_order_type=_env("LIVE_ENTRY_ORDER_TYPE", "FOK").upper(),
            partial_fills_allowed=_bool_env("LIVE_PARTIAL_FILLS_ALLOWED", False),
            max_entry_slippage=_decimal_env("LIVE_MAX_ENTRY_SLIPPAGE", "0.01"),
            stop_loss_order_type=_env("LIVE_STOP_LOSS_ORDER_TYPE", "FAK").upper(),
            stop_loss_initial_slippage=_decimal_env("LIVE_STOP_LOSS_INITIAL_SLIPPAGE", "0.02"),
            max_exit_slippage=_decimal_env("LIVE_MAX_EXIT_SLIPPAGE", "0.05"),
            max_stop_loss_attempts=int(_env("LIVE_MAX_STOP_LOSS_ATTEMPTS", "3") or "3"),
            stop_loss_retry_delay_ms=int(_env("LIVE_STOP_LOSS_RETRY_DELAY_MS", "500") or "500"),
            redemption_mode=_env("LIVE_REDEMPTION_MODE", "manual").lower(),
            adapter_scenario=_env("LIVE_MOCK_SCENARIO", "filled").lower(),
            operator_token=_env("LIVE_OPERATOR_TOKEN", ""),
            login_username=_env("LIVE_LOGIN_USERNAME", "Admin@system.com"),
            login_password_hash=_env("LIVE_LOGIN_PASSWORD_HASH", ""),
            session_secret=_env("LIVE_SESSION_SECRET", ""),
            session_ttl_seconds=int(_env("LIVE_SESSION_TTL_SECONDS", "0") or "0"),
            login_rate_limit_per_minute=int(_env("LIVE_LOGIN_RATE_LIMIT_PER_MINUTE", "5") or "5"),
            public_base_url=_env("LIVE_PUBLIC_BASE_URL", "https://live-poly.dvirtechnologies.com"),
            live_db_path=_env("LIVE_DB_PATH", "/opt/polymarket-btc-live/poly_live.sqlite3"),
            backup_dir=_env("LIVE_BACKUP_DIR", "/opt/polymarket-btc-live/backups"),
            backup_retention_days=int(_env("LIVE_BACKUP_RETENTION_DAYS", "7") or "7"),
            backup_max_total_bytes=int(_env("LIVE_BACKUP_MAX_TOTAL_BYTES", "1073741824") or "1073741824"),
            backup_warning_threshold_percent=int(_env("LIVE_BACKUP_WARNING_THRESHOLD_PERCENT", "80") or "80"),
            ws_raw_events_enabled=_bool_env("LIVE_WS_RAW_EVENTS_ENABLED", False),
            ws_event_retention_hours=int(_env("LIVE_WS_EVENT_RETENTION_HOURS", "48") or "48"),
            snapshot_min_interval_ms=int(_env("LIVE_SNAPSHOT_MIN_INTERVAL_MS", "1000") or "1000"),
            snapshot_save_only_on_change=_bool_env("LIVE_SNAPSHOT_SAVE_ONLY_ON_CHANGE", True),
            snapshot_raw_payload_enabled=_bool_env("LIVE_SNAPSHOT_RAW_PAYLOAD_ENABLED", False),
            snapshot_retention_days=int(_env("LIVE_SNAPSHOT_RETENTION_DAYS", "30") or "30"),
            technical_audit_retention_days=int(_env("LIVE_TECHNICAL_AUDIT_RETENTION_DAYS", "14") or "14"),
            retention_interval_seconds=int(_env("LIVE_RETENTION_INTERVAL_SECONDS", "3600") or "3600"),
            retention_batch_size=int(_env("LIVE_RETENTION_BATCH_SIZE", "2000") or "2000"),
            disk_warning_percent=int(_env("LIVE_DISK_WARNING_PERCENT", "70") or "70"),
            disk_critical_percent=int(_env("LIVE_DISK_CRITICAL_PERCENT", "80") or "80"),
            disk_emergency_percent=int(_env("LIVE_DISK_EMERGENCY_PERCENT", "90") or "90"),
            clob_host=_env("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
            data_api_host=_env("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com"),
            market_ws_url=_env("POLYMARKET_MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            user_ws_url=_env("POLYMARKET_USER_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/user"),
            user_ws_enabled=_bool_env("POLYMARKET_USER_WS_ENABLED", True),
            max_market_data_age_seconds=int(_env("LIVE_MARKET_DATA_STALE_AFTER_SECONDS", _env("LIVE_MAX_MARKET_DATA_AGE_SECONDS", "5")) or "5"),
            max_user_state_age_seconds=int(_env("LIVE_USER_STATE_STALE_AFTER_SECONDS", "15") or "15"),
            max_reconciliation_age_seconds=int(_env("LIVE_RECONCILIATION_MAX_AGE_SECONDS", _env("LIVE_MAX_RECONCILIATION_AGE_SECONDS", "30")) or "30"),
            reconciliation_interval_seconds=int(_env("LIVE_RECONCILIATION_INTERVAL_SECONDS", "15") or "15"),
            profile_address=_env("POLYMARKET_PROFILE_ADDRESS", ""),
            account_login_type=_env("POLYMARKET_ACCOUNT_LOGIN_TYPE", "email").lower(),
            signer_address=_env("POLYMARKET_SIGNER_ADDRESS", _env("POLY_ADDRESS", "")),
            funder_address=_env("POLYMARKET_FUNDER_ADDRESS", ""),
            signature_type=int(_env("POLYMARKET_SIGNATURE_TYPE", "1") or "1"),
            google_project_id=_env("GOOGLE_CLOUD_PROJECT", ""),
            google_secret_prefix=_env("GOOGLE_SECRET_MANAGER_PREFIX", ""),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.trading_mode not in {"DEMO", "LIVE"}:
            errors.append("TRADING_MODE must be DEMO or LIVE")
        if self.execution_mode not in {"READ_ONLY", "PAPER_TRADING"}:
            errors.append("LIVE_EXECUTION_MODE must be READ_ONLY or PAPER_TRADING; REAL_TRADING is blocked")
        if self.paper_trading_enabled and self.execution_mode != "PAPER_TRADING":
            errors.append("LIVE_PAPER_TRADING_ENABLED requires LIVE_EXECUTION_MODE=PAPER_TRADING")
        if self.paper_trading_enabled and (
            self.live_trading_enabled or self.live_order_submission_enabled
        ):
            errors.append("PAPER_TRADING cannot be combined with real trading or order submission")
        if self.paper_taker_fee_rate < 0 or self.paper_taker_fee_rate > Decimal("1"):
            errors.append("LIVE_PAPER_TAKER_FEE_RATE must be between 0 and 1")
        if self.live_adapter not in {"mock", "polymarket"}:
            errors.append("LIVE_ADAPTER must be mock or polymarket")
        if self.entry_order_type not in {"FOK", "FAK", "GTC", "GTD"}:
            errors.append("LIVE_ENTRY_ORDER_TYPE must be FOK, FAK, GTC, or GTD")
        if self.stop_loss_order_type != "FAK":
            errors.append("LIVE_STOP_LOSS_ORDER_TYPE must remain FAK for this phase")
        if self.redemption_mode not in {"manual", "approval", "automatic"}:
            errors.append("LIVE_REDEMPTION_MODE must be manual, approval, or automatic")
        if self.default_trade_amount_usd <= 0:
            errors.append("LIVE_DEFAULT_TRADE_AMOUNT_USD must be positive")
        if self.max_trade_amount_usd <= 0:
            errors.append("LIVE_MAX_TRADE_AMOUNT_USD must be positive")
        if self.default_trade_amount_usd > self.max_trade_amount_usd:
            errors.append("default trade amount exceeds max trade amount")
        if self.max_exit_slippage > Decimal("0.05"):
            errors.append("LIVE_MAX_EXIT_SLIPPAGE must be <= 0.05")
        if self.snapshot_min_interval_ms < 0:
            errors.append("LIVE_SNAPSHOT_MIN_INTERVAL_MS must be >= 0")
        if self.ws_event_retention_hours < 1 or self.snapshot_retention_days < 1:
            errors.append("LIVE WebSocket and snapshot retention must be positive")
        if self.technical_audit_retention_days < 1:
            errors.append("LIVE_TECHNICAL_AUDIT_RETENTION_DAYS must be positive")
        if self.retention_interval_seconds < 60 or self.retention_batch_size < 1:
            errors.append("LIVE retention interval/batch settings are invalid")
        if not (0 < self.disk_warning_percent < self.disk_critical_percent < self.disk_emergency_percent < 100):
            errors.append("LIVE disk thresholds must be ordered warning < critical < emergency < 100")
        return errors

    def paper_trading_active(self) -> bool:
        return (
            self.live_module_enabled
            and self.execution_mode == "PAPER_TRADING"
            and self.paper_trading_enabled
            and not self.live_trading_enabled
            and not self.live_order_submission_enabled
        )

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
            "execution_mode": self.execution_mode,
            "paper_trading_enabled": self.paper_trading_enabled,
            "paper_trading_active": self.paper_trading_active(),
            "market_ws_enabled": self.market_ws_enabled,
            "paper_taker_fee_rate": str(self.paper_taker_fee_rate),
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
            "max_active_rules": self.max_active_rules,
            "max_daily_realized_loss_usd": str(self.max_daily_realized_loss_usd),
            "max_consecutive_failed_orders": self.max_consecutive_failed_orders,
            "max_consecutive_losing_deals": self.max_consecutive_losing_deals,
            "entry_order_type": self.entry_order_type,
            "partial_fills_allowed": self.partial_fills_allowed,
            "max_entry_slippage": str(self.max_entry_slippage),
            "stop_loss_order_type": self.stop_loss_order_type,
            "stop_loss_initial_slippage": str(self.stop_loss_initial_slippage),
            "max_exit_slippage": str(self.max_exit_slippage),
            "max_stop_loss_attempts": self.max_stop_loss_attempts,
            "stop_loss_retry_delay_ms": self.stop_loss_retry_delay_ms,
            "redemption_mode": self.redemption_mode,
            "operator_auth_configured": bool(self.operator_token),
            "login_configured": bool(self.login_password_hash and self.session_secret),
            "login_username": self.login_username,
            "session_persistent_until_logout": self.session_ttl_seconds <= 0,
            "public_base_url": self.public_base_url,
            "live_db_path_configured": bool(self.live_db_path),
            "backup_dir_configured": bool(self.backup_dir),
            "profile_address_configured": bool(self.profile_address),
            "account_login_type": self.account_login_type,
            "signer_address_configured": bool(self.signer_address),
            "funder_address_configured": bool(self.funder_address),
            "signature_type": self.signature_type,
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
