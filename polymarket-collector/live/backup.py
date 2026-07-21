from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import LiveConfig
from .repository import LiveRepository, now_iso


@dataclass(frozen=True)
class BackupResult:
    status: str
    path: str
    size_bytes: int
    sha256: str
    removed: list[str]
    warning: str = ""


class LiveBackupManager:
    def __init__(self, config: LiveConfig, repo: LiveRepository):
        self.config = config
        self.repo = repo
        self.backup_dir = Path(config.backup_dir)

    def create_backup(self, reason: str = "manual") -> BackupResult:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        raw_tmp = self.backup_dir / f".poly_live_{timestamp}.sqlite3.tmp"
        gzip_tmp = self.backup_dir / f".poly_live_{timestamp}.sqlite3.gz.tmp"
        final_path = self.backup_dir / f"poly_live_{timestamp}.sqlite3.gz"
        removed: list[str] = []
        try:
            source = sqlite3.connect(self.repo.db_path)
            try:
                target = sqlite3.connect(raw_tmp)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()

            with raw_tmp.open("rb") as src, gzip.open(gzip_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            gzip_tmp.replace(final_path)
            raw_tmp.unlink(missing_ok=True)

            digest = self._sha256(final_path)
            size = final_path.stat().st_size
            removed = self.cleanup(exclude={final_path})
            warning = self.storage_warning()
            self.repo.record_backup(str(final_path), "ok", size, digest, reason)
            return BackupResult("ok", str(final_path), size, digest, removed, warning)
        except Exception as exc:
            raw_tmp.unlink(missing_ok=True)
            gzip_tmp.unlink(missing_ok=True)
            self.repo.record_backup(str(final_path), "failed", 0, "", reason, f"{type(exc).__name__}: {exc}")
            raise

    def cleanup(self, exclude: set[Path] | None = None) -> list[str]:
        exclude = {path.resolve() for path in (exclude or set())}
        backups = sorted(self.backup_dir.glob("poly_live_*.sqlite3.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
        if len(backups) <= 1:
            return []
        newest_valid = backups[0].resolve()
        removed: list[str] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.backup_retention_days)

        def can_remove(path: Path) -> bool:
            resolved = path.resolve()
            return resolved not in exclude and resolved != newest_valid

        for path in list(backups):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if mtime < cutoff and can_remove(path):
                removed.append(str(path))
                path.unlink(missing_ok=True)

        backups = sorted(self.backup_dir.glob("poly_live_*.sqlite3.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
        while len(backups) > 1 and self._total_bytes(backups) > self.config.backup_max_total_bytes:
            candidate = backups[-1]
            if not can_remove(candidate):
                break
            removed.append(str(candidate))
            candidate.unlink(missing_ok=True)
            backups = sorted(self.backup_dir.glob("poly_live_*.sqlite3.gz"), key=lambda path: path.stat().st_mtime, reverse=True)

        if removed:
            self.repo.audit("system", "backup_cleanup", "ok", details={"removed": removed})
        return removed

    def storage_warning(self) -> str:
        total = self._total_bytes(list(self.backup_dir.glob("poly_live_*.sqlite3.gz")))
        if self.config.backup_max_total_bytes <= 0:
            return ""
        percent = int((total / self.config.backup_max_total_bytes) * 100)
        if percent >= self.config.backup_warning_threshold_percent:
            return f"backup storage at {percent}%"
        return ""

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _total_bytes(paths: list[Path]) -> int:
        return sum(path.stat().st_size for path in paths if path.exists())
