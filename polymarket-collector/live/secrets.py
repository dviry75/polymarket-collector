from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import google_crc32c

from .config import redact


REQUIRED_SECRET_NAMES = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "LIVE_LOGIN_PASSWORD_HASH",
    "LIVE_SESSION_SECRET",
    "LIVE_OPERATOR_TOKEN",
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
    version: str = "1"

    def get_secret(self, name: str) -> str | None:
        if not self.version.isdigit() or int(self.version) < 1:
            raise ValueError("Secret Manager version must be a pinned positive integer")
        if not self.project_id:
            return None
        try:
            from google.cloud import secretmanager  # type: ignore
        except Exception as exc:
            raise RuntimeError("Secret Manager client is unavailable") from exc
        client = secretmanager.SecretManagerServiceClient()
        normalized_prefix = (
            self.prefix if not self.prefix or self.prefix.endswith(("-", "_"))
            else f"{self.prefix}-"
        )
        secret_id = f"{normalized_prefix}{name}" if normalized_prefix else name
        resource = f"projects/{self.project_id}/secrets/{secret_id}/versions/{self.version}"
        response = client.access_secret_version(request={"name": resource})
        checksum = google_crc32c.Checksum()
        checksum.update(response.payload.data)
        if response.payload.data_crc32c != int(checksum.hexdigest(), 16):
            raise RuntimeError("Secret Manager payload checksum mismatch")
        try:
            return response.payload.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Secret Manager payload is not valid UTF-8") from exc


@dataclass
class PrivateKeySecretProvider:
    """Read only the signing key from GSM and never copy it into os.environ."""

    private_key_provider: SecretProvider
    fallback: SecretProvider

    def get_secret(self, name: str) -> str | None:
        if name == "POLYMARKET_PRIVATE_KEY":
            return self.private_key_provider.get_secret(name)
        return self.fallback.get_secret(name)


def secret_readiness(provider: SecretProvider) -> dict[str, object]:
    values: dict[str, str | None] = {}
    inaccessible: list[str] = []
    for name in REQUIRED_SECRET_NAMES:
        try:
            values[name] = provider.get_secret(name)
        except Exception:
            values[name] = None
            inaccessible.append(name)
    return {
        "configured_count": sum(1 for value in values.values() if value),
        "missing": [name for name, value in values.items() if not value],
        "inaccessible": inaccessible,
        "redacted": {name: redact(value) for name, value in values.items() if value},
        "signer_configured": bool(values["POLYMARKET_PRIVATE_KEY"]),
        "user_ws_credentials_configured": all(
            values[name] for name in (
                "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"
            )
        ),
    }


def load_runtime_secrets(provider: SecretProvider) -> dict[str, object]:
    """Legacy helper for non-signing secrets; private keys are never exported."""
    loaded: list[str] = []
    missing: list[str] = []
    inaccessible: list[str] = []
    for name in REQUIRED_SECRET_NAMES:
        if name == "POLYMARKET_PRIVATE_KEY":
            missing.append(name)
            continue
        try:
            value = provider.get_secret(name)
        except Exception:
            inaccessible.append(name)
            continue
        if value:
            os.environ[name] = value.strip()
            loaded.append(name)
        else:
            missing.append(name)
    return {"loaded": loaded, "missing": missing, "inaccessible": inaccessible}
