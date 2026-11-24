import json
from datetime import datetime, timedelta, timezone

import pytest

from live import notification_prefs as prefs
from live.reporting import EmailService, ReportingService, SMTPSettings
from tests.test_reporting import add_event, setup_db

ADDRS = ("ops@example.com", "boss@example.com")


def test_missing_or_corrupt_file_sends_to_everyone(tmp_path):
    assert prefs.recipients_for(prefs.HOURLY_REPORT, ADDRS) == ADDRS
    prefs.prefs_file().write_text("{not json", encoding="utf-8")
    assert prefs.recipients_for(prefs.HOURLY_REPORT, ADDRS) == ADDRS


def test_save_and_filter_per_type():
    prefs.save_enabled({"boss@example.com": {prefs.HOURLY_REPORT: False}}, ADDRS)
    assert prefs.recipients_for(prefs.HOURLY_REPORT, ADDRS) == ("ops@example.com",)
    assert prefs.recipients_for(prefs.DAILY_REPORT, ADDRS) == ADDRS
    view = prefs.describe(ADDRS)
    boss = next(r for r in view["recipients"] if r["address"] == "boss@example.com")
    assert boss["enabled"] == {
        prefs.CRITICAL_ACTION: True, prefs.HOURLY_REPORT: False, prefs.DAILY_REPORT: True,
    }


def test_new_recipient_defaults_to_on():
    prefs.save_enabled({"boss@example.com": {prefs.DAILY_REPORT: False}}, ADDRS)
    grown = ADDRS + ("new@example.com",)
    assert prefs.recipients_for(prefs.DAILY_REPORT, grown) == ("ops@example.com", "new@example.com")


def test_critical_cannot_be_switched_off_for_everyone():
    with pytest.raises(prefs.NotificationPrefsError):
        prefs.save_enabled({a: {prefs.CRITICAL_ACTION: False} for a in ADDRS}, ADDRS)
    # A hand-edited file that does it anyway is ignored for critical.
    prefs.prefs_file().write_text(json.dumps({"disabled": {prefs.CRITICAL_ACTION: list(ADDRS)}}))
    assert prefs.recipients_for(prefs.CRITICAL_ACTION, ADDRS) == ADDRS


def test_unknown_address_or_type_rejected():
    with pytest.raises(prefs.NotificationPrefsError):
        prefs.save_enabled({"stranger@example.com": {prefs.HOURLY_REPORT: False}}, ADDRS)
    with pytest.raises(prefs.NotificationPrefsError):
        prefs.save_enabled({"ops@example.com": {"marketing": False}}, ADDRS)


def test_email_service_uses_filtered_recipients_and_skips_when_none():
    sent = []
    email = EmailService(SMTPSettings(sender="bot@example.com", recipients=ADDRS), sent.append)
    prefs.save_enabled({a: {prefs.HOURLY_REPORT: False} for a in ADDRS}
                       | {"ops@example.com": {prefs.HOURLY_REPORT: False, prefs.DAILY_REPORT: False}}, ADDRS)
    assert email.send("s", "t", "<p>t</p>", "<id@x>", kind=prefs.HOURLY_REPORT) is False
    assert email.send("s", "t", "<p>t</p>", "<id@x>", kind=prefs.DAILY_REPORT) is True
    assert sent[-1]["To"] == "boss@example.com"
    assert email.send("s", "t", "<p>t</p>", "<id@x>", kind=prefs.CRITICAL_ACTION) is True
    assert sent[-1]["To"] == ", ".join(ADDRS)


def test_hourly_report_marked_skipped_when_all_recipients_off(tmp_path):
    path = tmp_path / "db.sqlite"; repo = setup_db(path); start = datetime(2026, 8, 20, 0, tzinfo=timezone.utc)
    for i in range(12): add_event(repo, start + timedelta(minutes=5 * i), i)
    prefs.save_enabled({"ops@example.com": {prefs.HOURLY_REPORT: False}}, ("ops@example.com",))
    sent = []
    service = ReportingService(path, EmailService(SMTPSettings(sender="bot@example.com", recipients=("ops@example.com",)), sent.append))
    service.migrate(); service.finalize_events(start + timedelta(hours=2))
    first = service.generate_hour(start, start + timedelta(hours=1))
    again = service.generate_hour(start, start + timedelta(hours=1))
    assert first["email_status"] == "SKIPPED" and again["email_attempts"] == 1 and sent == []
