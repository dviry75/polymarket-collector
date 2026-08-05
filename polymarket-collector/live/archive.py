from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from .config import LiveConfig
from .repository import LiveRepository, now_iso, row_to_dict
from .strategy_repository import StrategyRepository


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    archive_day: str | None
    row_count: int
    compressed_bytes: int
    sha256: str
    object_name: str
    readback_verified: bool
    local_rows_deleted: int
    error: str = ""


class SnapshotArchiveManager:
    def __init__(self, config: LiveConfig, repo: LiveRepository, strategy: StrategyRepository):
        self.config = config
        self.repo = repo
        self.strategy = strategy

    def sample_db_growth(self) -> dict[str, Any]:
        size = self.repo.db_path.stat().st_size if self.repo.db_path.exists() else 0
        timestamp = datetime.now(timezone.utc)
        previous_size = int(self.repo.get_state("db_growth_previous_size_bytes", str(size)) or size)
        previous_at_raw = self.repo.get_state("db_growth_previous_sample_at", "")
        projected = None
        if previous_at_raw:
            try:
                previous_at = datetime.fromisoformat(previous_at_raw.replace("Z", "+00:00"))
                seconds = max(1.0, (timestamp - previous_at.astimezone(timezone.utc)).total_seconds())
                projected = max(0.0, (size - previous_size) * 86400 / seconds / 1_000_000)
            except ValueError:
                projected = None
        self.repo.set_state("db_growth_previous_size_bytes", str(size), "archive_metrics")
        self.repo.set_state("db_growth_previous_sample_at", timestamp.isoformat(), "archive_metrics")
        if projected is not None:
            self.repo.set_state("db_growth_projected_mb_day", f"{projected:.3f}", "archive_metrics")
            if projected >= 1000:
                self.strategy.alert(
                    alert_type="DB_GROWTH", severity="CRITICAL", reason_code="DB_GROWTH_GB_DAY",
                    message=f"Projected database growth is {projected:.1f} MB/day",
                    entity_type="database", entity_id=str(self.repo.db_path),
                )
        return {
            "size_bytes": size,
            "size_mb": round(size / 1_000_000, 3),
            "projected_mb_day": round(projected, 3) if projected is not None else None,
            "sampled_at": timestamp.isoformat(),
        }

    def run_once(self) -> ArchiveResult:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.config.snapshot_retention_days)).isoformat()
        with self.repo.connect() as conn:
            day_row = conn.execute(
                """
                SELECT substr(received_at,1,10) archive_day, COUNT(*) row_count
                FROM live_market_snapshots WHERE received_at < ?
                GROUP BY substr(received_at,1,10) ORDER BY archive_day LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
        if day_row is None:
            self.sample_db_growth()
            return ArchiveResult("no_data", None, 0, 0, "", "", False, 0)
        archive_day = str(day_row["archive_day"])
        run_id = self._start_run(archive_day)
        if not self.config.archive_bucket:
            error = "LIVE_ARCHIVE_GCS_BUCKET is not configured"
            self._finish_run(run_id, "blocked", error=error)
            self.strategy.alert(
                alert_type="ARCHIVE", severity="CRITICAL", reason_code="ARCHIVE_BUCKET_MISSING",
                message=error, entity_type="archive_day", entity_id=archive_day,
            )
            return ArchiveResult("blocked", archive_day, int(day_row["row_count"]), 0, "", "", False, 0, error)
        try:
            return self._archive_day(run_id, archive_day)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            self._finish_run(run_id, "failed", error=error)
            self.strategy.alert(
                alert_type="ARCHIVE", severity="CRITICAL", reason_code="ARCHIVE_FAILED",
                message=error, entity_type="archive_day", entity_id=archive_day,
            )
            return ArchiveResult("failed", archive_day, int(day_row["row_count"]), 0, "", "", False, 0, error)

    def _archive_day(self, run_id: int, archive_day: str) -> ArchiveResult:
        from google.cloud import storage

        client = storage.Client(project=self.config.google_project_id or None)
        bucket = client.get_bucket(self.config.archive_bucket)
        lifecycle = bucket.lifecycle_rules or []
        has_365_delete = any(
            str(rule.get("action", {}).get("type", "")).lower() == "delete"
            and int(rule.get("condition", {}).get("age", 0) or 0) == self.config.archive_retention_days
            for rule in lifecycle
        )
        if not has_365_delete:
            raise RuntimeError("GCS bucket lifecycle delete-after-365-days is not verified")
        prefix = self.config.archive_prefix.strip("/")
        object_name = f"{prefix}/{archive_day}/market_snapshots.ndjson.gz"
        manifest_name = f"{prefix}/{archive_day}/manifest.json"
        with tempfile.TemporaryDirectory(prefix="poly-live-archive-") as temp_dir:
            archive_path = Path(temp_dir) / "market_snapshots.ndjson.gz"
            digest = hashlib.sha256()
            row_count = 0
            with gzip.open(archive_path, "wt", encoding="utf-8", newline="\n") as handle:
                with self.repo.connect() as conn:
                    cursor = conn.execute(
                        "SELECT * FROM live_market_snapshots WHERE substr(received_at,1,10)=? ORDER BY id",
                        (archive_day,),
                    )
                    for row in cursor:
                        handle.write(json.dumps(row_to_dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                        row_count += 1
            with archive_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            checksum = digest.hexdigest()
            size = archive_path.stat().st_size
            blob = bucket.blob(object_name)
            blob.upload_from_filename(
                str(archive_path), content_type="application/gzip", if_generation_match=0,
            )
            remote_bytes = blob.download_as_bytes(if_generation_match=blob.generation)
            if hashlib.sha256(remote_bytes).hexdigest() != checksum:
                raise RuntimeError("archive checksum read-back mismatch")
            manifest = {
                "schema": "live_market_snapshots/v1",
                "archive_day": archive_day,
                "created_at": now_iso(),
                "row_count": row_count,
                "compressed_bytes": size,
                "sha256": checksum,
                "object_name": object_name,
                "retention_days": self.config.archive_retention_days,
            }
            manifest_blob = bucket.blob(manifest_name)
            manifest_blob.upload_from_string(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                content_type="application/json", if_generation_match=0,
            )
            remote_manifest = json.loads(manifest_blob.download_as_text(if_generation_match=manifest_blob.generation))
            if remote_manifest != manifest:
                raise RuntimeError("archive manifest read-back mismatch")
            with self.repo.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                deleted = conn.execute(
                    "DELETE FROM live_market_snapshots WHERE substr(received_at,1,10)=?",
                    (archive_day,),
                ).rowcount
                if deleted != row_count:
                    conn.rollback()
                    raise RuntimeError("local archive deletion count mismatch")
                conn.commit()
            self._finish_run(
                run_id, "verified", object_name=object_name, row_count=row_count,
                compressed_bytes=size, sha256=checksum,
                generation=str(blob.generation or ""), readback=True, deleted=deleted,
            )
            self.repo.set_state("last_archive_at", now_iso(), "archive")
            self.sample_db_growth()
            return ArchiveResult("verified", archive_day, row_count, size, checksum, object_name, True, deleted)

    def _start_run(self, archive_day: str) -> int:
        with self.repo.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO live_archive_runs(archive_day,status,started_at) VALUES(?,'running',?)",
                (archive_day, now_iso()),
            )
            conn.commit()
        return int(cursor.lastrowid)

    def _finish_run(
        self, run_id: int, status: str, *, object_name: str = "", row_count: int = 0,
        compressed_bytes: int = 0, sha256: str = "", generation: str = "",
        readback: bool = False, deleted: int = 0, error: str = "",
    ) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                UPDATE live_archive_runs SET object_name=?,row_count=?,compressed_bytes=?,
                    sha256=?,upload_generation=?,readback_verified=?,local_rows_deleted=?,
                    status=?,error=?,finished_at=? WHERE id=?
                """,
                (object_name,row_count,compressed_bytes,sha256,generation,1 if readback else 0,
                 deleted,status,error,now_iso(),run_id),
            )
            conn.commit()
