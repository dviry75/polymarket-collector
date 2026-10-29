"""DEMO-only settings. No dotenv loader, trading credentials or LIVE fallback."""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class DemoConfig:
    data_dir: Path = Path(__file__).resolve().parent / "var"
    db_path: Path | None = None
    login_username: str = field(default="", repr=False)
    login_password_hash: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    session_cookie_name: str = "demo_v2_session"
    session_cookie_path: str = "/demo/"
    session_ttl_seconds: int = 86400
    observation_max_age_seconds: float = 5.0
    decision_queue_capacity: int = 1024
    telemetry_capacity: int = 64
    public_data_enabled: bool = False
    recovery_stability_seconds: float = 2.0
    session_cookie_secure: bool = True

    def __post_init__(self) -> None:
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
            session_secret=env.get("DEMO_SESSION_SECRET", ""),
            session_ttl_seconds=int(env.get("DEMO_SESSION_TTL_SECONDS", "86400")),
            observation_max_age_seconds=float(env.get("DEMO_OBSERVATION_MAX_AGE_SECONDS", "5")),
            decision_queue_capacity=int(env.get("DEMO_DECISION_QUEUE_CAPACITY", "1024")),
            telemetry_capacity=int(env.get("DEMO_TELEMETRY_CAPACITY", "64")),
            public_data_enabled=env.get("DEMO_PUBLIC_DATA_ENABLED", "false").lower() == "true",
            recovery_stability_seconds=float(env.get("DEMO_RECOVERY_STABILITY_SECONDS", "2")),
            session_cookie_secure=env.get("DEMO_COOKIE_SECURE", "true").lower() != "false",
        )
