from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from .ipc import TraderIPCClient


class TraderStatusCache:
    def __init__(self, client: TraderIPCClient, ttl_seconds: float = 0.25):
        self.client = client
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._value: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._value and now - self._loaded_at <= self.ttl_seconds:
            return self._value
        with self._lock:
            now = time.monotonic()
            if not self._value or now - self._loaded_at > self.ttl_seconds:
                value = self.client.call("STATUS")
                self._value = value if isinstance(value, dict) else {}
                self._loaded_at = now
        return self._value

    def invalidate(self) -> None:
        self._loaded_at = 0.0


class RemoteHealth:
    def __init__(self, cache: TraderStatusCache, key: str):
        self.cache = cache
        self.key = key

    def health(self) -> dict[str, Any]:
        value = self.cache.get().get(self.key) or {}
        return dict(value) if isinstance(value, dict) else {"status": "UNKNOWN"}


class RemoteReconciliation:
    def __init__(self, client: TraderIPCClient):
        self.client = client

    async def run_once(self, actor: str = "dashboard") -> Any:
        return await self.client.call_async("RECONCILIATION_RUN", {"actor": actor})


class RemoteStrategyRuntime(RemoteHealth):
    async def emergency_close_all(self, *_args: Any, actor: str = "operator") -> Any:
        return await self.cache.client.call_async(
            "EMERGENCY_CLOSE_EXECUTE", {"actor": actor}
        )


class RemoteDryRun:
    def __init__(self, client: TraderIPCClient):
        self.client = client

    def preview(self, payload: dict[str, Any], actor: str = "operator") -> Any:
        return self.client.call("DRY_RUN", {"payload": payload, "actor": actor})


def named_service(name: str) -> Any:
    return SimpleNamespace(name=name)
