from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


def build_repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate(pause_entries_default=False)
    return temporary, base, strategy


def test_alert_dedup_resolve_recurrence_and_acknowledge():
    temporary, _base, strategy = build_repo()
    try:
        first = strategy.alert(
            alert_type="TEST",
            severity="WARNING",
            reason_code="SAME_INCIDENT",
            message="first",
            entity_type="POSITION",
            entity_id="p1",
        )
        second = strategy.alert(
            alert_type="TEST",
            severity="ERROR",
            reason_code="SAME_INCIDENT",
            message="second",
            entity_type="POSITION",
            entity_id="p1",
        )
        assert second == first
        opened = strategy.active_alerts()[0]
        assert opened["status"] == "OPEN"
        assert opened["occurrence_count"] == 2
        assert opened["recurrence_count"] == 0

        resolved = strategy.resolve_alert(
            alert_type="TEST",
            reason_code="SAME_INCIDENT",
            entity_type="POSITION",
            entity_id="p1",
            actor="test",
            resolution_reason="EVIDENCE_CLEAN",
        )
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_reason"] == "EVIDENCE_CLEAN"
        assert strategy.active_alerts() == []

        reopened = strategy.alert(
            alert_type="TEST",
            severity="CRITICAL",
            reason_code="SAME_INCIDENT",
            message="recurred",
            entity_type="POSITION",
            entity_id="p1",
        )
        assert reopened == first
        recurrent = strategy.active_alerts()[0]
        assert recurrent["status"] == "OPEN"
        assert recurrent["recurrence_count"] == 1
        assert recurrent["reopened_at"]
        assert recurrent["notification_status"] == "PENDING"

        acknowledged = strategy.acknowledge_alert(first, "operator")
        assert acknowledged["status"] == "ACKNOWLEDGED"
        assert acknowledged["acknowledged_by"] == "operator"
        assert strategy.active_alerts() == []
    finally:
        temporary.cleanup()


def test_operator_watchdog_opens_email_outbox_after_five_minutes_and_resolves():
    temporary, base, strategy = build_repo()
    try:
        strategy.acquire_pause(
            actor="operator", reason="OPERATOR_PAUSE", owner="OPERATOR"
        )
        acquired = datetime.now(timezone.utc) - timedelta(minutes=6)
        base.set_state("pause_acquired_at", acquired.isoformat(), "test")

        alert = strategy.watchdog_operator_action(
            actor="test", now=datetime.now(timezone.utc)
        )
        assert alert["status"] == "OPEN"
        assert alert["severity"] == "CRITICAL"
        assert alert["reason_code"] == "OPERATOR_ACTION_REQUIRED_OVER_5M"
        assert alert["message"].startswith("[CRITICAL ACTION]")
        repeated = strategy.watchdog_operator_action(
            actor="test", now=datetime.now(timezone.utc)
        )
        assert repeated["id"] == alert["id"]
        assert repeated["occurrence_count"] == 1
        outbox = strategy.critical_email_outbox()
        assert len(outbox) == 1
        assert outbox[0]["subject"].startswith("[CRITICAL ACTION]")
        assert outbox[0]["notification_status"] == "PENDING"

        sent = strategy.record_alert_notification_result(
            int(alert["id"]), sent=True, actor="authorized_sender"
        )
        assert sent["notification_status"] == "SENT"
        assert strategy.critical_email_outbox() == []

        base.set_states({
            "pause_entries": "false",
            "pause_state": "TRADING",
            "pause_owner": "NONE",
            "pause_reason": "",
            "pause_cause": "",
            "release_policy": "AUTO_WHEN_CLEAN",
            "operator_action_required": "false",
        }, "test")
        resolved = strategy.watchdog_operator_action(actor="test")
        assert resolved["status"] == "RESOLVED"
        assert strategy.active_alerts() == []
    finally:
        temporary.cleanup()


def test_orphaned_reconciliation_runs_are_terminalized_once_with_audit():
    temporary, base, _strategy = build_repo()
    try:
        old_id = base.start_reconciliation()
        recent_id = base.start_reconciliation()
        now = datetime.now(timezone.utc)
        with base.connect() as conn:
            conn.execute(
                "UPDATE live_reconciliation_runs SET started_at=? WHERE id=?",
                ((now - timedelta(minutes=10)).isoformat(), old_id),
            )
            conn.commit()

        result = base.finalize_orphaned_reconciliations(
            actor="startup", now=now
        )
        assert result["count"] == 1
        with base.connect() as conn:
            old = conn.execute(
                "SELECT * FROM live_reconciliation_runs WHERE id=?", (old_id,)
            ).fetchone()
            recent = conn.execute(
                "SELECT * FROM live_reconciliation_runs WHERE id=?", (recent_id,)
            ).fetchone()
            audit = conn.execute(
                "SELECT * FROM live_audit_log WHERE "
                "action='finalize_orphaned_reconciliations'"
            ).fetchone()
        assert old["status"] == "failed"
        assert old["error"] == "ORPHANED_PREVIOUS_PROCESS"
        assert old["finished_at"]
        assert recent["status"] == "running"
        assert audit["reason"] == "ORPHANED_PREVIOUS_PROCESS"
        assert base.finalize_orphaned_reconciliations(
            actor="startup", now=now
        )["count"] == 0
    finally:
        temporary.cleanup()
