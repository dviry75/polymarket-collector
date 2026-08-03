from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import logging
import shutil
import threading
from typing import Any

from .config import LiveConfig
from .repository import LiveRepository, now_iso


@dataclass
class RetentionResult:
    status: str
    started_at: str
    finished_at: str
    deleted_websocket_events: int = 0
    deleted_market_snapshots: int = 0
    deleted_technical_audit: int = 0
    disk_used_percent_before: float = 0.0
    disk_used_percent_after: float = 0.0
    error: str = ""


class LiveRetentionManager:
    """Bounded retention for raw/technical history only.

    Business, admin, trading and unclassified audit rows are never selected.
    Rule/deal referenced snapshots are also never selected.
    """

    def __init__(self, repo: LiveRepository, config: LiveConfig):
        self.repo = repo
        self.config = config
        self._lock = threading.Lock()
        self._logger = logging.getLogger("live.retention")
        self._last_result: RetentionResult | None = None

    def _disk(self) -> dict[str, float | int | str]:
        usage = shutil.disk_usage(self.repo.db_path.parent)
        percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
        if percent >= self.config.disk_emergency_percent:
            level = "EMERGENCY"
        elif percent >= self.config.disk_critical_percent:
            level = "CRITICAL"
        elif percent >= self.config.disk_warning_percent:
            level = "WARNING"
        else:
            level = "OK"
        return {
            "used_percent": round(percent, 2),
            "free_bytes": usage.free,
            "level": level,
        }

    @staticmethod
    def _cutoff(delta: timedelta, now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        return (current - delta).astimezone(timezone.utc).isoformat()

    def preview(self, now: datetime | None = None) -> dict[str, Any]:
        ws_cutoff = self._cutoff(timedelta(hours=self.config.ws_event_retention_hours), now)
        snapshot_cutoff = self._cutoff(timedelta(days=self.config.snapshot_retention_days), now)
        audit_cutoff = self._cutoff(timedelta(days=self.config.technical_audit_retention_days), now)
        with self.repo.connect() as conn:
            websocket_events = int(conn.execute(
                "SELECT COUNT(*) FROM live_websocket_events WHERE received_at < ?", (ws_cutoff,)
            ).fetchone()[0])
            market_snapshots = int(conn.execute(
                """
                SELECT COUNT(*) FROM live_market_snapshots AS snapshot
                WHERE snapshot.received_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM live_rule_evaluations AS evaluation
                    WHERE evaluation.market_snapshot_id = snapshot.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM live_deals AS deal
                    WHERE deal.entry_snapshot_id = snapshot.id OR deal.exit_snapshot_id = snapshot.id
                  )
                """,
                (snapshot_cutoff,),
            ).fetchone()[0])
            technical_audit = int(conn.execute(
                """
                SELECT COUNT(*) FROM live_audit_log
                WHERE category = 'TECHNICAL' AND occurred_at < ?
                """,
                (audit_cutoff,),
            ).fetchone()[0])
        return {
            "websocket_events": websocket_events,
            "market_snapshots": market_snapshots,
            "technical_audit": technical_audit,
            "cutoffs": {
                "websocket_events": ws_cutoff,
                "market_snapshots": snapshot_cutoff,
                "technical_audit": audit_cutoff,
            },
            "disk": self._disk(),
        }

    def _delete_batches(self, table: str, where: str, params: tuple[Any, ...]) -> int:
        deleted = 0
        while True:
            with self.repo.connect() as conn:
                rows = conn.execute(
                    f"SELECT id FROM {table} WHERE {where} LIMIT ?",
                    (*params, self.config.retention_batch_size),
                ).fetchall()
                if not rows:
                    return deleted
                ids = [int(row[0]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
                conn.commit()
                deleted += len(ids)

    def run(self, now: datetime | None = None) -> RetentionResult:
        started_at = now_iso()
        if not self._lock.acquire(blocking=False):
            return RetentionResult("skipped_already_running", started_at, now_iso())
        before = self._disk()
        result = RetentionResult(
            "running", started_at, "", disk_used_percent_before=float(before["used_percent"])
        )
        try:
            preview = self.preview(now)
            cutoffs = preview["cutoffs"]
            self._logger.info("retention started preview=%s disk=%s", preview, before)
            result.deleted_websocket_events = self._delete_batches(
                "live_websocket_events", "received_at < ?", (cutoffs["websocket_events"],)
            )
            result.deleted_market_snapshots = self._delete_batches(
                "live_market_snapshots",
                """
                received_at < ?
                AND NOT EXISTS (
                    SELECT 1 FROM live_rule_evaluations
                    WHERE live_rule_evaluations.market_snapshot_id = live_market_snapshots.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM live_deals
                    WHERE live_deals.entry_snapshot_id = live_market_snapshots.id
                       OR live_deals.exit_snapshot_id = live_market_snapshots.id
                )
                """,
                (cutoffs["market_snapshots"],),
            )
            result.deleted_technical_audit = self._delete_batches(
                "live_audit_log", "category = 'TECHNICAL' AND occurred_at < ?",
                (cutoffs["technical_audit"],),
            )
            result.status = "ok"
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            self._logger.exception("retention failed")
        finally:
            result.finished_at = now_iso()
            after = self._disk()
            result.disk_used_percent_after = float(after["used_percent"])
            self._last_result = result
            try:
                self.repo.set_state("retention_last_status", result.status, "retention", audit_change=False)
                self.repo.set_state("retention_last_finished_at", result.finished_at, "retention", audit_change=False)
                self.repo.set_state("retention_last_error", result.error, "retention", audit_change=False)
            except Exception:
                self._logger.exception("retention status persistence failed")
            finally:
                self._lock.release()
            self._logger.info("retention finished result=%s disk=%s", asdict(result), after)
            if after["level"] != "OK":
                self._logger.warning("live disk threshold reached disk=%s", after)
        return result

    def health(self, *, public: bool = False) -> dict[str, Any]:
        result = self._last_result
        last_error = result.error if result else self.repo.get_state("retention_last_error", "")
        retention = {
            "status": result.status if result else self.repo.get_state("retention_last_status", "never_run"),
            "last_finished_at": result.finished_at if result else self.repo.get_state("retention_last_finished_at", ""),
            "last_error_present": bool(last_error),
        }
        if not public:
            retention["last_error"] = last_error
        return {
            "disk": self._disk(),
            "retention": retention,
        }
