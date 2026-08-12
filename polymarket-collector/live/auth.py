from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import LiveConfig

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, VerificationError
except ImportError:  # pragma: no cover - dependency exists in production requirements
    PasswordHasher = None
    VerifyMismatchError = VerificationError = Exception


COOKIE_NAME = "live_session"


@dataclass
class LoginAttempt:
    attempts: list[float]


class LiveAuthManager:
    def __init__(self, config: LiveConfig, session_version_getter=None):
        self.config = config
        self._attempts: dict[str, LoginAttempt] = {}
        self._session_version_getter = session_version_getter or (lambda: "1")

    def configured(self) -> bool:
        return bool(self.config.login_password_hash and self.config.session_secret)

    def verify_password(self, password: str) -> bool:
        expected = self.config.login_password_hash
        if not expected:
            return False
        if expected.startswith("$argon2id$"):
            if PasswordHasher is None:
                return False
            try:
                return bool(PasswordHasher().verify(expected, password))
            except (VerifyMismatchError, VerificationError, Exception):
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
        version = str(self._session_version_getter())
        nonce = secrets.token_urlsafe(18)
        payload = f"{username}:{issued}:{version}:{nonce}"
        signature = hmac.new(self.config.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")

    def verify_session(self, token: str | None) -> bool:
        if not token or not self.config.session_secret:
            return False
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            fields = decoded.rsplit(":", 4)
            if len(fields) == 5:
                username, issued_raw, version, nonce, signature = fields
                payload = f"{username}:{issued_raw}:{version}:{nonce}"
            elif len(fields) == 4:  # Backward compatibility during rollout.
                username, issued_raw, version, signature = fields
                payload = f"{username}:{issued_raw}:{version}"
            else:
                return False
            issued = int(issued_raw)
        except Exception:
            return False
        if username != self.config.login_username:
            return False
        if self.config.session_ttl_seconds > 0 and time.time() - issued > self.config.session_ttl_seconds:
            return False
        if version != str(self._session_version_getter()):
            return False
        expected = hmac.new(self.config.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def csrf_token(self, session_token: str | None) -> str:
        if not session_token or not self.config.session_secret:
            return ""
        return hmac.new(
            self.config.session_secret.encode("utf-8"),
            f"csrf:{session_token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_csrf(self, session_token: str | None, csrf_token: str | None) -> bool:
        expected = self.csrf_token(session_token)
        return bool(expected and csrf_token and hmac.compare_digest(expected, csrf_token))

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "username_configured": bool(self.config.login_username),
            "ttl_seconds": self.config.session_ttl_seconds,
            "persistent_until_logout": self.config.session_ttl_seconds <= 0,
            "rate_limit_per_minute": self.config.login_rate_limit_per_minute,
        }
