import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


SECRET_MARKERS = (
    "PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "AUTHORIZATION",
    "SIGNATURE", "OPERATOR_TOKEN", "SESSION_SECRET", "COOKIE", "CSRF_TOKEN",
)


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
    environment: str = "LIVE"
    strategy_id: str = "btc-updown-5m"
    strategy_version: str = "strategy-v1"
    provenance_source: str = "TRADER"
    paper_trading_enabled: bool = False
    market_ws_enabled: bool = False
    market_discovery_interval_seconds: int = 5
    paper_taker_fee_rate: Decimal = Decimal("0.07")
    raw_market_ws_payloads_enabled: bool = False
    snapshot_min_interval_seconds: Decimal = Decimal("0.5")
    market_ws_ingress_queue_capacity: int = 32
    snapshot_retention_days: int = 30
    archive_retention_days: int = 365
    archive_bucket: str = ""
    archive_prefix: str = "polymarket-live/snapshots"
    live_module_enabled: bool = False
    live_trading_enabled: bool = False
    live_order_submission_enabled: bool = False
    live_adapter: str = "mock"
    live_kill_switch_default: bool = True
    pause_entries_default: bool = True
    canary_armed: bool = False
    continuous_trading_enabled: bool = False
    canary_event_limit: int = 1
    default_trade_amount_usd: Decimal = Decimal("5")
    max_trade_amount_usd: Decimal = Decimal("5")
    max_total_exposure_usd: Decimal = Decimal("5")
    max_trade_tokens: Decimal = Decimal("5")
    max_open_deals: int = 1
    max_open_orders: int = 2
    max_active_rules: int = 1
    max_daily_realized_loss_usd: Decimal = Decimal("10")
    max_consecutive_failed_orders: int = 3
    max_consecutive_losing_deals: int = 5
    entry_order_type: str = "FAK"
    partial_fills_allowed: bool = True
    strategy_entry_price: Decimal = Decimal("0.74")
    strategy_entry_max_price: Decimal = Decimal("0.76")
    strategy_take_profit_price: Decimal = Decimal("0.96")
    strategy_stop_price: Decimal = Decimal("0.66")
    strategy_emergency_price: Decimal = Decimal("0.60")
    strategy_stop_min_price: Decimal = Decimal("0.01")
    strategy_emergency_min_price: Decimal = Decimal("0.01")
    strategy_entry_window_seconds: int = 120
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
    trader_socket_path: str = "/run/polymarket/trader.sock"
    backup_dir: str = "/opt/polymarket-btc-live/backups"
    backup_retention_days: int = 7
    backup_max_total_bytes: int = 1_073_741_824
    backup_warning_threshold_percent: int = 80
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    user_ws_enabled: bool = True
    max_market_data_age_seconds: int = 1
    max_user_state_age_seconds: int = 15
    max_reconciliation_age_seconds: int = 30
    reconciliation_interval_seconds: int = 15
    reconciliation_active_interval_seconds: int = 3
    order_heartbeat_interval_seconds: int = 5
    profile_address: str = ""
    account_login_type: str = "email"
    signer_address: str = ""
    google_private_key_secret_version: str = "1"
    private_signing_readiness_enabled: bool = False
    funder_address: str = ""
    signature_type: int = 1
    google_project_id: str = ""
    google_secret_prefix: str = ""

    @classmethod
    def from_env(cls) -> "LiveConfig":
        return cls(
            trading_mode=_env("TRADING_MODE", "DEMO").upper(),
            execution_mode=_env("LIVE_EXECUTION_MODE", "READ_ONLY").upper(),
            environment=_env("LIVE_ENVIRONMENT", "LIVE").upper(),
            strategy_id=_env("LIVE_STRATEGY_ID", "btc-updown-5m"),
            strategy_version=_env("LIVE_STRATEGY_VERSION", "strategy-v1"),
            provenance_source=_env("LIVE_PROVENANCE_SOURCE", "TRADER").upper(),
            paper_trading_enabled=_bool_env("LIVE_PAPER_TRADING_ENABLED", False),
            market_ws_enabled=_bool_env("POLYMARKET_MARKET_WS_ENABLED", False),
            market_discovery_interval_seconds=int(_env("LIVE_MARKET_DISCOVERY_INTERVAL_SECONDS", "5") or "5"),
            paper_taker_fee_rate=_decimal_env("LIVE_PAPER_TAKER_FEE_RATE", "0.07"),
            raw_market_ws_payloads_enabled=_bool_env("LIVE_RAW_MARKET_WS_PAYLOADS_ENABLED", False),
            snapshot_min_interval_seconds=_decimal_env("LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS", "0.5"),
            market_ws_ingress_queue_capacity=int(
                _env("LIVE_MARKET_WS_INGRESS_QUEUE_CAPACITY", "32") or "32"
            ),
            snapshot_retention_days=int(_env("LIVE_SNAPSHOT_RETENTION_DAYS", "30") or "30"),
            archive_retention_days=int(_env("LIVE_ARCHIVE_RETENTION_DAYS", "365") or "365"),
            archive_bucket=_env("LIVE_ARCHIVE_GCS_BUCKET", ""),
            archive_prefix=_env("LIVE_ARCHIVE_GCS_PREFIX", "polymarket-live/snapshots"),
            live_module_enabled=_bool_env("LIVE_MODULE_ENABLED", False),
            live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
            live_order_submission_enabled=_bool_env("LIVE_ORDER_SUBMISSION_ENABLED", False),
            live_adapter=_env("LIVE_ADAPTER", "mock").lower(),
            live_kill_switch_default=_bool_env("LIVE_KILL_SWITCH", True),
            pause_entries_default=_bool_env("LIVE_PAUSE_ENTRIES", True),
            canary_armed=_bool_env("LIVE_CANARY_ARMED", False),
            continuous_trading_enabled=_bool_env("LIVE_CONTINUOUS_TRADING_ENABLED", False),
            canary_event_limit=int(_env("LIVE_CANARY_EVENT_LIMIT", "1") or "1"),
            default_trade_amount_usd=_decimal_env("LIVE_DEFAULT_TRADE_AMOUNT_USD", "5"),
            max_trade_amount_usd=_decimal_env("LIVE_MAX_TRADE_AMOUNT_USD", "5"),
            max_total_exposure_usd=_decimal_env("LIVE_MAX_TOTAL_EXPOSURE_USD", "5"),
            max_open_deals=int(_env("LIVE_MAX_OPEN_DEALS", "1") or "1"),
            max_trade_tokens=_decimal_env("LIVE_MAX_TRADE_TOKENS", "5"),
            max_open_orders=int(_env("LIVE_MAX_OPEN_ORDERS", "2") or "2"),
            max_active_rules=int(_env("LIVE_MAX_ACTIVE_RULES", "1") or "1"),
            max_daily_realized_loss_usd=_decimal_env("LIVE_MAX_DAILY_REALIZED_LOSS_USD", "10"),
            max_consecutive_failed_orders=int(_env("LIVE_MAX_CONSECUTIVE_FAILED_ORDERS", "3") or "3"),
            max_consecutive_losing_deals=int(_env("LIVE_MAX_CONSECUTIVE_LOSING_DEALS", "5") or "5"),
            entry_order_type=_env("LIVE_ENTRY_ORDER_TYPE", "FAK").upper(),
            partial_fills_allowed=_bool_env("LIVE_PARTIAL_FILLS_ALLOWED", True),
            strategy_entry_price=_decimal_env("LIVE_STRATEGY_ENTRY_PRICE", "0.74"),
            strategy_entry_max_price=_decimal_env("LIVE_STRATEGY_ENTRY_MAX_PRICE", "0.76"),
            strategy_take_profit_price=_decimal_env("LIVE_STRATEGY_TAKE_PROFIT_PRICE", "0.96"),
            strategy_stop_price=_decimal_env("LIVE_STRATEGY_STOP_PRICE", "0.66"),
            strategy_emergency_price=_decimal_env("LIVE_STRATEGY_EMERGENCY_PRICE", "0.60"),
            strategy_stop_min_price=_decimal_env("LIVE_STRATEGY_STOP_MIN_PRICE", "0.01"),
            strategy_emergency_min_price=_decimal_env("LIVE_STRATEGY_EMERGENCY_MIN_PRICE", "0.01"),
            strategy_entry_window_seconds=int(_env("LIVE_STRATEGY_ENTRY_WINDOW_SECONDS", "120") or "120"),
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
            trader_socket_path=_env("LIVE_TRADER_SOCKET_PATH", "/run/polymarket/trader.sock"),
            backup_dir=_env("LIVE_BACKUP_DIR", "/opt/polymarket-btc-live/backups"),
            backup_retention_days=int(_env("LIVE_BACKUP_RETENTION_DAYS", "7") or "7"),
            backup_max_total_bytes=int(_env("LIVE_BACKUP_MAX_TOTAL_BYTES", "1073741824") or "1073741824"),
            backup_warning_threshold_percent=int(_env("LIVE_BACKUP_WARNING_THRESHOLD_PERCENT", "80") or "80"),
            clob_host=_env("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
            data_api_host=_env("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com"),
            market_ws_url=_env("POLYMARKET_MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            user_ws_url=_env("POLYMARKET_USER_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/user"),
            user_ws_enabled=_bool_env("POLYMARKET_USER_WS_ENABLED", True),
            max_market_data_age_seconds=int(_env("LIVE_MARKET_DATA_STALE_AFTER_SECONDS", _env("LIVE_MAX_MARKET_DATA_AGE_SECONDS", "1")) or "1"),
            max_user_state_age_seconds=int(_env("LIVE_USER_STATE_STALE_AFTER_SECONDS", "15") or "15"),
            max_reconciliation_age_seconds=int(_env("LIVE_RECONCILIATION_MAX_AGE_SECONDS", _env("LIVE_MAX_RECONCILIATION_AGE_SECONDS", "30")) or "30"),
            reconciliation_interval_seconds=int(_env("LIVE_RECONCILIATION_INTERVAL_SECONDS", "15") or "15"),
            reconciliation_active_interval_seconds=int(_env("LIVE_RECONCILIATION_ACTIVE_INTERVAL_SECONDS", "3") or "3"),
            order_heartbeat_interval_seconds=int(_env("LIVE_ORDER_HEARTBEAT_INTERVAL_SECONDS", "5") or "5"),
            profile_address=_env("POLYMARKET_PROFILE_ADDRESS", ""),
            account_login_type=_env("POLYMARKET_ACCOUNT_LOGIN_TYPE", "email").lower(),
            signer_address=_env("POLYMARKET_SIGNER_ADDRESS", _env("POLY_ADDRESS", "")),
            funder_address=_env("POLYMARKET_FUNDER_ADDRESS", ""),
            signature_type=int(_env("POLYMARKET_SIGNATURE_TYPE", "1") or "1"),
            google_project_id=_env("GOOGLE_CLOUD_PROJECT", ""),
            google_secret_prefix=_env("GOOGLE_SECRET_MANAGER_PREFIX", ""),
            google_private_key_secret_version=_env(
                "POLYMARKET_PRIVATE_KEY_SECRET_VERSION", "1"
            ),
            private_signing_readiness_enabled=_bool_env(
                "LIVE_PRIVATE_SIGNING_READINESS_ENABLED", False
            ),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.trading_mode not in {"DEMO", "LIVE"}:
            errors.append("TRADING_MODE must be DEMO or LIVE")
        if self.execution_mode not in {"READ_ONLY", "PAPER_TRADING", "REAL_TRADING"}:
            errors.append("LIVE_EXECUTION_MODE must be READ_ONLY, PAPER_TRADING or REAL_TRADING")
        if self.environment not in {"LIVE", "STAGING", "TEST", "DEMO"}:
            errors.append("LIVE_ENVIRONMENT must be LIVE, STAGING, TEST or DEMO")
        if not self.strategy_id or not self.strategy_version or not self.provenance_source:
            errors.append("dashboard provenance identifiers must be configured")
        if self.paper_trading_enabled and self.execution_mode != "PAPER_TRADING":
            errors.append("LIVE_PAPER_TRADING_ENABLED requires LIVE_EXECUTION_MODE=PAPER_TRADING")
        if self.paper_trading_enabled and (
            self.live_trading_enabled or self.live_order_submission_enabled
        ):
            errors.append("PAPER_TRADING cannot be combined with real trading or order submission")
        if self.paper_taker_fee_rate < 0 or self.paper_taker_fee_rate > Decimal("1"):
            errors.append("LIVE_PAPER_TAKER_FEE_RATE must be between 0 and 1")
        if self.raw_market_ws_payloads_enabled and self.trading_mode == "LIVE":
            errors.append("raw Market WebSocket payload persistence must remain disabled in LIVE")
        if self.snapshot_min_interval_seconds < Decimal("0.5"):
            errors.append("LIVE_SNAPSHOT_MIN_INTERVAL_SECONDS must be >= 0.5")
        if self.market_ws_ingress_queue_capacity < 2:
            errors.append("LIVE_MARKET_WS_INGRESS_QUEUE_CAPACITY must be >= 2")
        if self.snapshot_retention_days != 30:
            errors.append("LIVE_SNAPSHOT_RETENTION_DAYS must remain 30")
        if self.archive_retention_days != 365:
            errors.append("LIVE_ARCHIVE_RETENTION_DAYS must remain 365")
        if self.live_adapter not in {"mock", "polymarket"}:
            errors.append("LIVE_ADAPTER must be mock or polymarket")
        if self.entry_order_type != "FAK":
            errors.append("LIVE_ENTRY_ORDER_TYPE must remain FAK")
        if not self.partial_fills_allowed:
            errors.append("LIVE_PARTIAL_FILLS_ALLOWED must remain true for FAK lifecycle")
        if self.stop_loss_order_type != "FAK":
            errors.append("LIVE_STOP_LOSS_ORDER_TYPE must remain FAK for the latched 0.66 exit")
        if self.redemption_mode not in {"manual", "approval", "automatic"}:
            errors.append("LIVE_REDEMPTION_MODE must be manual, approval, or automatic")
        if self.default_trade_amount_usd <= 0:
            errors.append("LIVE_DEFAULT_TRADE_AMOUNT_USD must be positive")
        if self.max_trade_amount_usd <= 0:
            errors.append("LIVE_MAX_TRADE_AMOUNT_USD must be positive")
        if self.default_trade_amount_usd > self.max_trade_amount_usd:
            errors.append("default trade amount exceeds max trade amount")
        if self.default_trade_amount_usd != Decimal("5") or self.max_trade_amount_usd != Decimal("5"):
            errors.append("entry amount and cap must remain exactly $5 All-In")
        if self.max_total_exposure_usd != Decimal("5"):
            errors.append("LIVE_MAX_TOTAL_EXPOSURE_USD must remain exactly 5")
        if self.max_trade_tokens != Decimal("5"):
            errors.append("LIVE_MAX_TRADE_TOKENS must remain exactly 5")
        if self.max_open_deals != 1:
            errors.append("LIVE_MAX_OPEN_DEALS must remain exactly 1")
        if self.max_active_rules != 1:
            errors.append("LIVE_MAX_ACTIVE_RULES must remain exactly 1")
        if self.max_daily_realized_loss_usd != Decimal("10"):
            errors.append("LIVE_MAX_DAILY_REALIZED_LOSS_USD must remain exactly 10")
        if self.google_project_id and self.google_private_key_secret_version != "1":
            errors.append("POLYMARKET_PRIVATE_KEY_SECRET_VERSION must remain pinned to 1")
        expected_strategy = (
            (self.strategy_entry_price, Decimal("0.74")),
            (self.strategy_entry_max_price, Decimal("0.76")),
            (self.strategy_take_profit_price, Decimal("0.96")),
            (self.strategy_stop_price, Decimal("0.66")),
            (self.strategy_emergency_price, Decimal("0.60")),
            (self.strategy_stop_min_price, Decimal("0.01")),
            (self.strategy_emergency_min_price, Decimal("0.01")),
        )
        if any(actual != expected for actual, expected in expected_strategy):
            errors.append("strategy prices are immutable for this LIVE build")
        if self.strategy_entry_window_seconds != 120:
            errors.append("entry window must remain exactly 120 seconds")
        if self.order_heartbeat_interval_seconds != 5:
            errors.append("order heartbeat interval must remain exactly 5 seconds")
        if self.canary_event_limit != 1:
            errors.append("LIVE_CANARY_EVENT_LIMIT must remain exactly 1")
        if self.max_exit_slippage > Decimal("0.05"):
            errors.append("LIVE_MAX_EXIT_SLIPPAGE must be <= 0.05")
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
            and self.execution_mode == "REAL_TRADING"
            and self.live_module_enabled
            and self.live_trading_enabled
            and self.live_order_submission_enabled
            and self.live_adapter == "polymarket"
            and (self.canary_armed or self.continuous_trading_enabled)
            and not self.pause_entries_default
            and not self.live_kill_switch_default
        )

    def safe_public_dict(self) -> dict[str, object]:
        return {
            "trading_mode": self.trading_mode,
            "execution_mode": self.execution_mode,
            "environment": self.environment,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "provenance_source": self.provenance_source,
            "paper_trading_enabled": self.paper_trading_enabled,
            "paper_trading_active": self.paper_trading_active(),
            "market_ws_enabled": self.market_ws_enabled,
            "paper_taker_fee_rate": str(self.paper_taker_fee_rate),
            "raw_market_ws_payloads_enabled": self.raw_market_ws_payloads_enabled,
            "snapshot_min_interval_seconds": str(self.snapshot_min_interval_seconds),
            "snapshot_retention_days": self.snapshot_retention_days,
            "archive_retention_days": self.archive_retention_days,
            "archive_configured": bool(self.archive_bucket),
            "live_module_enabled": self.live_module_enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "live_order_submission_enabled": self.live_order_submission_enabled,
            "live_adapter": self.live_adapter,
            "live_kill_switch_default": self.live_kill_switch_default,
            "pause_entries_default": self.pause_entries_default,
            "canary_armed": self.canary_armed,
            "continuous_trading_enabled": self.continuous_trading_enabled,
            "canary_event_limit": self.canary_event_limit,
            "default_trade_amount_usd": str(self.default_trade_amount_usd),
            "max_trade_amount_usd": str(self.max_trade_amount_usd),
            "max_total_exposure_usd": str(self.max_total_exposure_usd),
            "max_open_deals": self.max_open_deals,
            "max_open_orders": self.max_open_orders,
            "max_trade_tokens": str(self.max_trade_tokens),
            "max_active_rules": self.max_active_rules,
            "max_daily_realized_loss_usd": str(self.max_daily_realized_loss_usd),
            "max_consecutive_failed_orders": self.max_consecutive_failed_orders,
            "max_consecutive_losing_deals": self.max_consecutive_losing_deals,
            "entry_order_type": self.entry_order_type,
            "partial_fills_allowed": self.partial_fills_allowed,
            "strategy_entry_price": str(self.strategy_entry_price),
            "strategy_entry_max_price": str(self.strategy_entry_max_price),
            "strategy_take_profit_price": str(self.strategy_take_profit_price),
            "strategy_stop_price": str(self.strategy_stop_price),
            "strategy_emergency_price": str(self.strategy_emergency_price),
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
            "trader_socket_configured": bool(self.trader_socket_path),
            "backup_dir_configured": bool(self.backup_dir),
            "profile_address_configured": bool(self.profile_address),
            "account_login_type": self.account_login_type,
            "signer_address_configured": bool(self.signer_address),
            "funder_address_configured": bool(self.funder_address),
            "signature_type": self.signature_type,
            "private_key_secret_version": self.google_private_key_secret_version,
            "private_signing_readiness_enabled": self.private_signing_readiness_enabled,
            "real_submission_armed": self.real_submission_armed(),
        }


def redact(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    return "[CONFIGURED]"


def redact_mapping(mapping: dict[str, object], secret_markers: Iterable[str] = SECRET_MARKERS) -> dict[str, object]:
    redacted: dict[str, object] = {}
    markers = tuple(marker.upper() for marker in secret_markers)
    for key, value in mapping.items():
        redacted[key] = redact(value) if any(marker in key.upper() for marker in markers) else value
    return redacted
