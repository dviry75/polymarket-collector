"""DEMO-only settings. No dotenv loader, trading credentials or LIVE fallback."""

from dataclasses import dataclass, field
from decimal import Decimal
import math
import json
import os
import re
from pathlib import Path
from typing import Mapping

from .domain.fees import DEFAULT_FLAT_FEE, FlatFeePolicy
from .notifications import MailConfig


def encoded_password_hash(value: str) -> bool:
    return isinstance(value, str) and bool(value.startswith("$argon2id$") or re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def parse_additional_users(value: str) -> tuple[tuple[str, str], ...]:
    def unique(pairs):
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Duplicate DEMO user")
        return dict(pairs)
    try:
        users = json.loads(value, object_pairs_hook=unique)
        if not isinstance(users, dict):
            raise ValueError()
        return tuple(users.items())
    except (ValueError, TypeError):
        raise ValueError("DEMO_AUTH_USERS_JSON must map unique usernames to password hashes") from None


@dataclass(frozen=True)
class DemoConfig:
    data_dir: Path = Path(__file__).resolve().parent / "var"
    db_path: Path | None = None
    login_username: str = field(default="", repr=False)
    login_password_hash: str = field(default="", repr=False)
    additional_users: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    session_secret: str = field(default="", repr=False)
    session_cookie_name: str = "demo_v2_session"
    session_cookie_path: str = "/demo/"
    session_ttl_seconds: int = 86400
    observation_max_age_seconds: float = 5.0
    decision_queue_capacity: int = 1024
    telemetry_capacity: int = 64
    decision_max_queue_delay_seconds: float = 1.0
    transport_idle_seconds: float = 30.0
    snapshot_timeout_seconds: float = 10.0
    public_data_enabled: bool = False
    recovery_stability_seconds: float = 2.0
    session_cookie_secure: bool = True
    flat_fee_per_action: Decimal = DEFAULT_FLAT_FEE
    mail: MailConfig = field(default_factory=MailConfig, repr=False)

    def __post_init__(self) -> None:
        users = tuple(tuple(item) for item in self.additional_users)
        seen = {self.login_username}
        if len(users) > 20:
            raise ValueError("At most 20 additional DEMO users")
        for item in users:
            if len(item) != 2:
                raise ValueError("Invalid additional DEMO user")
            username, password_hash = item
            if (not isinstance(username, str) or not username.strip() or len(username) > 120
                    or any(ord(c) < 32 or ord(c) == 127 for c in username)
                    or username in seen or not encoded_password_hash(password_hash)):
                raise ValueError("Invalid or duplicate additional DEMO credentials")
            seen.add(username)
        object.__setattr__(self, "additional_users", users)
        object.__setattr__(self, "flat_fee_per_action", FlatFeePolicy(self.flat_fee_per_action).per_action)
        root = Path(self.data_dir).expanduser().resolve()
        candidate = Path(self.db_path) if self.db_path else root / "demo_v2.sqlite3"
        target = candidate.expanduser().resolve()
        # A positive ownership rule also rejects symlink escapes and arbitrary
        # existing database names, without knowing any production DB location.
        if target.parent != root or target.name != "demo_v2.sqlite3":
            raise ValueError("DEMO_DB_PATH must be DEMO_DATA_DIR/demo_v2.sqlite3")
        if target.exists() and target.stat().st_nlink != 1:
            raise ValueError("DEMO database must not be a hard-link alias")
        if self.session_cookie_name != "demo_v2_session":
            raise ValueError("DEMO requires its dedicated session cookie name")
        if self.session_cookie_path != "/demo/":
            raise ValueError("DEMO session cookie must be scoped to /demo/")
        if self.session_ttl_seconds <= 0:
            raise ValueError("DEMO_SESSION_TTL_SECONDS must be positive")
        if not math.isfinite(self.observation_max_age_seconds) or self.observation_max_age_seconds <= 0:
            raise ValueError("DEMO_OBSERVATION_MAX_AGE_SECONDS must be finite and positive")
        for name in ("decision_max_queue_delay_seconds","transport_idle_seconds","snapshot_timeout_seconds"):
            if not math.isfinite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError(name+" must be finite and positive")
        if self.decision_queue_capacity <= 0 or self.telemetry_capacity <= 0:
            raise ValueError("DEMO queue capacities must be positive")
        if not math.isfinite(self.recovery_stability_seconds) or self.recovery_stability_seconds < 0:
            raise ValueError("recovery stability must be finite and nonnegative")
        object.__setattr__(self, "data_dir", root)
        object.__setattr__(self, "db_path", target)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DemoConfig":
        env = os.environ if environ is None else environ
        default_root = Path(__file__).resolve().parent / "var"
        root = Path(env.get("DEMO_DATA_DIR", str(default_root)))
        path = env.get("DEMO_DB_PATH", "")
        return cls(
            data_dir=root,
            db_path=Path(path) if path else None,
            login_username=env.get("DEMO_AUTH_USERNAME", env.get("DEMO_LOGIN_USERNAME", "")),
            login_password_hash=env.get("DEMO_AUTH_PASSWORD_HASH", env.get("DEMO_LOGIN_PASSWORD_HASH", "")),
            additional_users=parse_additional_users(env.get("DEMO_AUTH_USERS_JSON", "{}")),
            session_secret=env.get("DEMO_SESSION_SECRET", ""),
            session_ttl_seconds=int(env.get("DEMO_SESSION_TTL_SECONDS", "86400")),
            observation_max_age_seconds=float(env.get("DEMO_OBSERVATION_MAX_AGE_SECONDS", "5")),
            decision_queue_capacity=int(env.get("DEMO_DECISION_QUEUE_CAPACITY", "1024")),
            telemetry_capacity=int(env.get("DEMO_TELEMETRY_CAPACITY", "64")),
            decision_max_queue_delay_seconds=float(env.get("DEMO_DECISION_MAX_QUEUE_DELAY_SECONDS","1")),
            transport_idle_seconds=float(env.get("DEMO_TRANSPORT_IDLE_SECONDS","30")),
            snapshot_timeout_seconds=float(env.get("DEMO_SNAPSHOT_TIMEOUT_SECONDS","10")),
            public_data_enabled=env.get("DEMO_PUBLIC_DATA_ENABLED", "false").lower() == "true",
            recovery_stability_seconds=float(env.get("DEMO_RECOVERY_STABILITY_SECONDS", "2")),
            session_cookie_secure=env.get("DEMO_COOKIE_SECURE", "true").lower() != "false",
            flat_fee_per_action=env.get("DEMO_FLAT_FEE_PER_ACTION", "0.068"),
            mail=MailConfig.from_env(env),
        )
