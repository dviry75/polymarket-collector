import json
from datetime import date, datetime, timezone

from live import notification_prefs as prefs
from live.daily_report import DailyReportService, report_window
from live.reporting import EmailService, SMTPSettings
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from scripts.migrate_external_fills import DDL

UTC = timezone.utc


def build(tmp_path, sent):
    path = tmp_path / "db.sqlite"
    repo = LiveRepository(path); repo.migrate(False)
    StrategyRepository(repo).migrate(pause_entries_default=False)
    with repo.connect() as conn:
        conn.executescript(DDL)
    email = EmailService(SMTPSettings(sender="bot@example.com", recipients=("ops@example.com",)), sent.append)
    return repo, DailyReportService(path, email)


def fill(repo, trade_id, condition, side, notional, at, fee=0.0, title="BTC Up or Down"):
    with repo.connect() as conn:
        conn.execute(
            "INSERT INTO external_fills(trade_id,condition_id,market_title,side,price,size,notional_usd,fee_usd,matched_at,raw_json,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, condition, title, side, 0.5, notional * 2, notional, fee, at, "{}", at),
        )


def test_window_is_20_to_20_israel_time():
    # 2026-11-04 is winter time (UTC+2): 20:00 Israel = 18:00 UTC.
    day, start, end = report_window(datetime(2026, 11, 4, 18, 5, tzinfo=UTC))
    assert day == date(2026, 11, 4)
    assert (start, end) == (datetime(2026, 11, 3, 18, tzinfo=UTC), datetime(2026, 11, 4, 18, tzinfo=UTC))
    day, _start, end = report_window(datetime(2026, 11, 4, 17, 59, tzinfo=UTC))
    assert day == date(2026, 11, 3) and end == datetime(2026, 11, 3, 18, tzinfo=UTC)


def test_daily_report_numbers_and_send_once(tmp_path):
    sent = []
    repo, service = build(tmp_path, sent)
    # Inside the window: one winner (+3), one loser (-2 after fee).
    fill(repo, "a1", "win", "BUY", 5.0, "2026-11-04T08:00:00+00:00")
    fill(repo, "a2", "win", "SELL", 8.0, "2026-11-04T08:05:00+00:00", title="Winner market")
    fill(repo, "b1", "lose", "BUY", 5.0, "2026-11-04T09:00:00+00:00", fee=0.5)
    fill(repo, "b2", "lose", "SELL", 3.5, "2026-11-04T09:05:00+00:00", title="Loser market")
    # Outside the window (after 20:00 Israel) - belongs to the next report.
    fill(repo, "c1", "late", "BUY", 5.0, "2026-11-04T18:30:00+00:00")
    report = service.tick(datetime(2026, 11, 4, 18, 1, tzinfo=UTC))
    assert report["email_status"] == "SENT" and len(sent) == 1
    assert (report["deal_count"], report["wins"], report["losses"]) == (2, 1, 1)
    assert round(report["total_usd"], 2) == 1.0
    text = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "היום הרווחנו $1.00" in text
    assert "1 עסקאות הצליחו והרוויחו $3.00" in text
    assert "1 עסקאות נכשלו והפסידו $2.00" in text
    assert "Winner market" in text and "Loser market" in text

    again = service.tick(datetime(2026, 11, 4, 18, 20, tzinfo=UTC))
    assert again["email_status"] == "SENT" and again["email_attempts"] == 1 and len(sent) == 1


def test_daily_report_skipped_when_switched_off(tmp_path):
    sent = []
    _repo, service = build(tmp_path, sent)
    prefs.save_enabled({"ops@example.com": {prefs.DAILY_REPORT: False}}, ("ops@example.com",))
    report = service.tick(datetime(2026, 11, 4, 18, 1, tzinfo=UTC))
    assert report["email_status"] == "SKIPPED" and sent == []


def test_paused_minutes_counted_inside_window(tmp_path):
    sent = []
    repo, service = build(tmp_path, sent)
    with repo.connect() as conn:
        for at, value in (("2026-11-04T10:00:00+00:00", "true"), ("2026-11-04T10:30:00+00:00", "false")):
            conn.execute(
                "INSERT INTO live_audit_log(occurred_at,actor,action,status,reason,details_json) VALUES(?,?,?,?,?,?)",
                (at, "test", "set_pause_entries", "ok", "", json.dumps({"value": value})),
            )
    ops = service._operations(datetime(2026, 11, 3, 18, tzinfo=UTC), datetime(2026, 11, 4, 18, tzinfo=UTC))
    assert ops["paused_minutes"] == 30
