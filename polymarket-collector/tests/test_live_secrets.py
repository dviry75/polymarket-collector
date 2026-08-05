from types import SimpleNamespace

import google_crc32c
import pytest
from google.cloud import secretmanager

from live.secrets import (
    EnvSecretProvider,
    GoogleSecretManagerProvider,
    PrivateKeySecretProvider,
)


def _response(data: bytes):
    checksum = google_crc32c.Checksum()
    checksum.update(data)
    return SimpleNamespace(
        payload=SimpleNamespace(
            data=data,
            data_crc32c=int(checksum.hexdigest(), 16),
        )
    )


def test_secret_manager_uses_exact_version_one_and_validates_crc(monkeypatch):
    seen = {}

    class Client:
        def access_secret_version(self, request):
            seen.update(request)
            return _response(b"fake-unit-test-secret")

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", Client)
    provider = GoogleSecretManagerProvider("project-id", "polymarket-live", "1")
    assert provider.get_secret("POLYMARKET_PRIVATE_KEY") == "fake-unit-test-secret"
    assert seen["name"] == (
        "projects/project-id/secrets/"
        "polymarket-live-POLYMARKET_PRIVATE_KEY/versions/1"
    )
    assert "latest" not in seen["name"]


def test_secret_manager_rejects_unpinned_version_before_access(monkeypatch):
    monkeypatch.setattr(
        secretmanager,
        "SecretManagerServiceClient",
        lambda: pytest.fail("client must not be created"),
    )
    with pytest.raises(ValueError, match="pinned positive integer"):
        GoogleSecretManagerProvider("project-id", "prefix", "latest").get_secret(
            "POLYMARKET_PRIVATE_KEY"
        )


def test_secret_manager_checksum_and_utf8_fail_closed(monkeypatch):
    class BadChecksumClient:
        def access_secret_version(self, request):
            response = _response(b"fake")
            response.payload.data_crc32c += 1
            return response

    monkeypatch.setattr(
        secretmanager, "SecretManagerServiceClient", BadChecksumClient
    )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        GoogleSecretManagerProvider("project-id").get_secret("POLYMARKET_PRIVATE_KEY")

    class BadUtf8Client:
        def access_secret_version(self, request):
            return _response(b"\xff")

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", BadUtf8Client)
    with pytest.raises(RuntimeError, match="valid UTF-8"):
        GoogleSecretManagerProvider("project-id").get_secret("POLYMARKET_PRIVATE_KEY")


def test_private_key_provider_never_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "must-not-be-used")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-from-env")

    class MemoryProvider:
        def get_secret(self, name):
            return "key-from-memory"

    provider = PrivateKeySecretProvider(MemoryProvider(), EnvSecretProvider())
    assert provider.get_secret("POLYMARKET_PRIVATE_KEY") == "key-from-memory"
    assert provider.get_secret("POLYMARKET_API_KEY") == "api-from-env"
