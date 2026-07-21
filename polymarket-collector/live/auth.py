from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from .config import LiveConfig


COOKIE_NAME = "live_session"


@dataclass
class LoginAttempt:
    attempts: list[float]


class LiveAuthManager:
    def __init__(self, config: LiveConfig):
        self.config = config
        self._attempts: dict[str, LoginAttempt] = {}

    def configured(self) -> bool:
        return bool(self.config.login_password_hash and self.config.session_secret)

    def verify_password(self, password: str) -> bool:
        expected = self.config.login_password_hash
        if not expected:
            return False
        if expected.startswith("sha256:"):
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return hmac.compare_digest(expected, f"sha256:{digest}")
        return hmac.compare_digest(expected, password)

    def rate_limited(self, key: str) -> bool:
        now = time.time()
        attempt = self._attempts.setdefault(key, LoginAttempt([]))
        attempt.attempts = [ts for ts in attempt.attempts if now - ts < 60]
        return len(attempt.attempts) >= self.config.login_rate_limit_per_minute

    def record_failure(self, key: str) -> None:
        attempt = self._attempts.setdefault(key, LoginAttempt([]))
        attempt.attempts.append(time.time())

    def create_session(self, username: str) -> str:
        issued = int(time.time())
        payload = f"{username}:{issued}"
        signature = hmac.new(self.config.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")

    def verify_session(self, token: str | None) -> bool:
        if not token or not self.config.session_secret:
            return False
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            username, issued_raw, signature = decoded.rsplit(":", 2)
            issued = int(issued_raw)
        except Exception:
            return False
        if username != self.config.login_username:
            return False
        if time.time() - issued > self.config.session_ttl_seconds:
            return False
        payload = f"{username}:{issued}"
        expected = hmac.new(self.config.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "username_configured": bool(self.config.login_username),
            "ttl_seconds": self.config.session_ttl_seconds,
            "rate_limit_per_minute": self.config.login_rate_limit_per_minute,
        }
