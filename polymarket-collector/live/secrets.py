from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .config import redact


REQUIRED_SECRET_NAMES = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "LIVE_LOGIN_PASSWORD_HASH",
    "LIVE_SESSION_SECRET",
)


class SecretProvider(Protocol):
    def get_secret(self, name: str) -> str | None:
        ...


@dataclass
class EnvSecretProvider:
    def get_secret(self, name: str) -> str | None:
        return os.getenv(name)


@dataclass
class GoogleSecretManagerProvider:
    project_id: str
    prefix: str = ""

    def get_secret(self, name: str) -> str | None:
        if not self.project_id:
            return None
        try:
            from google.cloud import secretmanager  # type: ignore
        except Exception:
            return None
        client = secretmanager.SecretManagerServiceClient()
        secret_id = f"{self.prefix}{name}" if self.prefix else name
        resource = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": resource})
        return response.payload.data.decode("utf-8")


def secret_readiness(provider: SecretProvider) -> dict[str, object]:
    values = {name: provider.get_secret(name) for name in REQUIRED_SECRET_NAMES}
    return {
        "configured_count": sum(1 for value in values.values() if value),
        "missing": [name for name, value in values.items() if not value],
        "redacted": {name: redact(value) for name, value in values.items() if value},
        "real_credentials_configured": all(values[name] for name in REQUIRED_SECRET_NAMES[:4]),
    }
