from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class InfrastructureSampler:
    def __init__(self, db_path: str | Path, *, cache_seconds: float = 15.0):
        self.db_path = Path(db_path)
        self.cache_seconds = max(1.0, float(cache_seconds))
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] = {}
        self._previous_cpu: tuple[int, int] | None = None
        self.started_monotonic = time.monotonic()

    @staticmethod
    def _cpu_ticks() -> tuple[int, int] | None:
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
        except (OSError, ValueError, IndexError):
            return None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    def _cpu_percent(self) -> float | None:
        current = self._cpu_ticks()
        previous = self._previous_cpu
        self._previous_cpu = current
        if current is None or previous is None:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)

    @staticmethod
    def _memory() -> dict[str, int | float | None]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {"total_bytes": None, "used_bytes": None, "available_bytes": None, "used_percent": None}
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        used = total - available if total is not None and available is not None else None
        percent = round(used / total * 100, 2) if used is not None and total else None
        return {"total_bytes": total, "used_bytes": used, "available_bytes": available, "used_percent": percent}

    @staticmethod
    def _disk(path: Path) -> dict[str, int | float | None]:
        try:
            stat = os.statvfs(path)
        except OSError:
            return {"total_bytes": None, "used_bytes": None, "free_bytes": None, "used_percent": None}
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        return {"total_bytes": total, "used_bytes": used, "free_bytes": free, "used_percent": round(used / total * 100, 2) if total else None}

    @staticmethod
    def _host_uptime() -> float | None:
        try:
            return float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _service(name: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["systemctl", "show", name, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "NRestarts", "--no-pager"],
                check=False, capture_output=True, text=True, timeout=1.5,
            )
        except (OSError, subprocess.SubprocessError):
            return {"state": "UNKNOWN", "substate": "UNKNOWN", "pid": None, "restart_count": None}
        data: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1); data[key] = value
        return {
            "state": data.get("ActiveState", "UNKNOWN").upper(),
            "substate": data.get("SubState", "UNKNOWN").upper(),
            "pid": int(data.get("MainPID", "0") or 0),
            "restart_count": int(data.get("NRestarts", "0") or 0),
        }

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cached and now - self._cached_at < self.cache_seconds:
                return {**self._cached, "cache": {"hit": True, "ttl_seconds": self.cache_seconds}}
            db_size = None
            try:
                db_size = self.db_path.stat().st_size
            except OSError:
                pass
            payload = {
                "cpu": {"used_percent": self._cpu_percent()},
                "ram": self._memory(),
                "disk": self._disk(self.db_path.parent),
                "database": {"size_bytes": db_size},
                "uptime": {
                    "host_seconds": self._host_uptime(),
                    "dashboard_process_seconds": round(now - self.started_monotonic, 3),
                },
                "services": {
                    "trader": self._service("polymarket-trader.service"),
                    "dashboard": self._service("polymarket-dashboard.service"),
                    "legacy_live": self._service("polymarket-live.service"),
                    "nginx": self._service("nginx.service"),
                },
                "sampled_at_epoch": time.time(),
            }
            self._cached = payload
            self._cached_at = now
            return {**payload, "cache": {"hit": False, "ttl_seconds": self.cache_seconds}}
