"""Daily 20:00 (Israel) P&L email.

Observability only: reads Polymarket deal records and the audit log, writes only
its own ``live_daily_reports`` ledger. The window is 20:00 yesterday -> 20:00
today, so every deal lands in exactly one report. Totals come from
``DashboardReadModel.external_summary`` so the email always matches the
dashboard's numbers.
"""
from __future__ import annotations

import html
import logging
import smtplib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .dashboard_read_model import DashboardReadModel, DateWindow
from .notification_prefs import DAILY_REPORT
from .reporting import EmailService, SMTPSettings
from .repository import LiveRepository

LOG = logging.getLogger(__name__)
ISRAEL = ZoneInfo("Asia/Jerusalem")
UTC = timezone.utc
REPORT_HOUR = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_daily_reports (
    report_date TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total_usd REAL,
    deal_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    email_status TEXT NOT NULL DEFAULT 'PENDING',
    email_attempts INTEGER NOT NULL DEFAULT 0,
    email_sent_at TEXT,
    email_last_error TEXT,
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def report_window(now: datetime) -> tuple[date, datetime, datetime]:
    """The latest completed 20:00 -> 20:00 Israel window at ``now``."""
    local = now.astimezone(ISRAEL)
    end_day = local.date() if local.hour >= REPORT_HOUR else local.date() - timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, REPORT_HOUR, tzinfo=ISRAEL)
    prev = end_day - timedelta(days=1)
    start = datetime(prev.year, prev.month, prev.day, REPORT_HOUR, tzinfo=ISRAEL)
    return end_day, start.astimezone(UTC), end.astimezone(UTC)


def _money(value: Any) -> str:
    amount = float(value or 0)
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


class DailyReportService:
    def __init__(self, db_path: str | Path, email: EmailService | None = None):
        self.db_path = str(db_path)
        self.repo = LiveRepository(self.db_path)
        self.model = DashboardReadModel(self.repo, query_timeout_seconds=30.0)
        self.email = email or EmailService(SMTPSettings.from_env())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)

    # -- data -------------------------------------------------------------

    def _summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        return self.model.external_summary(DateWindow(start, end, "daily_report"))

    def _extremes(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Best and worst deal, using the same cash-flow rule as the summary."""
        with self.connect() as conn:
            has_redemptions = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_redemptions'"
            ).fetchone())
            redeemed = (
                "COALESCE((SELECT SUM(payout_usd) FROM external_redemptions r "
                "WHERE r.condition_id=ef.condition_id),0)"
                if has_redemptions else "0"
            )
            floor = self.model._external_floor()
            lower = max(_iso(start), floor) if floor else _iso(start)
            rows = conn.execute(
                f"""SELECT MAX(ef.market_title) AS title,
                           SUM(CASE WHEN ef.side='SELL' THEN ef.notional_usd ELSE -ef.notional_usd END)
                               - SUM(COALESCE(ef.fee_usd,0)) + MAX({redeemed}) AS cash_flow_usd
                    FROM external_fills ef
                    WHERE ef.matched_at>=? AND ef.matched_at<?
                    GROUP BY ef.condition_id""",
                (lower, _iso(end)),
            ).fetchall() if self.model._external_ready() else []
        if not rows:
            return {"best": None, "worst": None}
        ordered = sorted(rows, key=lambda row: float(row["cash_flow_usd"] or 0))
        as_dict = lambda row: {"title": row["title"] or "—", "cash_flow_usd": float(row["cash_flow_usd"] or 0)}
        return {"best": as_dict(ordered[-1]), "worst": as_dict(ordered[0])}

    def _operations(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Minutes entries were paused and [CRITICAL ACTION] emails in the window."""
        lo, hi = _iso(start), _iso(end)
        with self.connect() as conn:
            before = conn.execute(
                "SELECT details_json FROM live_audit_log WHERE action='set_pause_entries' "
                "AND occurred_at<? ORDER BY occurred_at DESC LIMIT 1", (lo,),
            ).fetchone()
            changes = conn.execute(
                "SELECT occurred_at,details_json FROM live_audit_log WHERE action='set_pause_entries' "
                "AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at", (lo, hi),
            ).fetchall()
            critical = conn.execute(
                "SELECT COUNT(*) FROM live_audit_log WHERE action='critical_alert_notification' "
                "AND status='ok' AND occurred_at>=? AND occurred_at<?", (lo, hi),
            ).fetchone()[0]
        paused = '"true"' in str(before["details_json"] if before else "")
        since, total = start, 0.0
        for row in changes:
            at = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
            if paused:
                total += (at - since).total_seconds()
            paused, since = '"true"' in str(row["details_json"]), at
        if paused:
            total += (end - since).total_seconds()
        return {"paused_minutes": int(round(total / 60)), "critical_emails": int(critical)}

    def build(self, report_date: date, start: datetime, end: datetime) -> dict[str, Any]:
        local_end = end.astimezone(ISRAEL)
        month_start = datetime(local_end.year, local_end.month, 1, tzinfo=ISRAEL).astimezone(UTC)
        return {
            "report_date": report_date.isoformat(),
            "window_start": _iso(start),
            "window_end": _iso(end),
            "today": self._summary(start, end),
            "previous": self._summary(start - timedelta(days=1), start),
            "month": self._summary(month_start, end),
            **self._extremes(start, end),
            **self._operations(start, end),
        }

    # -- rendering --------------------------------------------------------

    @staticmethod
    def render(data: dict[str, Any]) -> tuple[str, str, str]:
        today, prev, month = data["today"], data["previous"], data["month"]
        total = float(today.get("total_cash_flow_usd") or 0)
        wins, losses = int(today.get("wins") or 0), int(today.get("losses") or 0)
        gross_profit = float(today.get("average_win_usd") or 0) * wins
        gross_loss = float(today.get("average_loss_usd") or 0) * losses
        start = datetime.fromisoformat(data["window_start"]).astimezone(ISRAEL)
        end = datetime.fromisoformat(data["window_end"]).astimezone(ISRAEL)
        verb = "הרווחנו" if total >= 0 else "הפסדנו"
        subject = f"{'✅' if total >= 0 else '🔻'} Polymarket — דוח יומי {end:%d.%m} — {verb} {_money(abs(total))}"
        lines = [
            f"היום {verb} {_money(abs(total))}",
            f"{wins} עסקאות הצליחו והרוויחו {_money(gross_profit)}",
            f"{losses} עסקאות נכשלו והפסידו {_money(gross_loss)}",
            "",
            f"אחוז הצלחה: {today['win_rate_percent']:.0f}%" if today.get("win_rate_percent") is not None else "אחוז הצלחה: —",
            f"עמלות ששולמו: {_money(today.get('fees_usd'))}",
        ]
        if data.get("best"):
            lines.append(f"העסקה הטובה ביותר: {_money(data['best']['cash_flow_usd'])} · {data['best']['title']}")
            lines.append(f"העסקה הגרועה ביותר: {_money(data['worst']['cash_flow_usd'])} · {data['worst']['title']}")
        lines += [
            "",
            f"אתמול: {_money(prev.get('total_cash_flow_usd'))} ({int(prev.get('deal_count') or 0)} עסקאות)",
            f"מתחילת החודש: {_money(month.get('total_cash_flow_usd'))} ({int(month.get('deal_count') or 0)} עסקאות)",
            "",
            f"המסחר היה עצור: {data['paused_minutes']} דקות",
            f"התראות CRITICAL ACTION שנשלחו: {data['critical_emails']}",
            "",
            f"תקופה: {start:%d.%m %H:%M} – {end:%d.%m %H:%M} (שעון ישראל)",
        ]
        if today.get("quality") != "REAL":
            lines.append(f"הערה: הנתונים חלקיים ({today.get('reason') or today.get('quality')})")
        text = "\n".join(lines)
        body = ("<html><body dir='rtl'><pre style='font-family:system-ui;white-space:pre-wrap'>"
                + html.escape(text) + "</pre></body></html>")
        return subject, text, body

    # -- send once --------------------------------------------------------

    def tick(self, now: datetime | None = None, *, send: bool = True) -> dict[str, Any]:
        report_date, start, end = report_window((now or datetime.now(UTC)).astimezone(UTC))
        self.migrate()
        stamp = _iso(datetime.now(UTC))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM live_daily_reports WHERE report_date=?", (report_date.isoformat(),)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO live_daily_reports(report_date,window_start,window_end,message_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (report_date.isoformat(), _iso(start), _iso(end),
                     f"<daily-{report_date.isoformat()}@polymarket-live>", stamp, stamp),
                )
                row = conn.execute(
                    "SELECT * FROM live_daily_reports WHERE report_date=?", (report_date.isoformat(),)
                ).fetchone()
        report = dict(row)
        if not send or report["email_status"] in ("SENT", "SKIPPED") or int(report["email_attempts"]) >= 3:
            return report
        data = self.build(report_date, start, end)
        today = data["today"]
        status, error, sent_at = "SENT", None, None
        try:
            subject, text, body = self.render(data)
            delivered = self.email.send(subject, text, body, report["message_id"], kind=DAILY_REPORT)
            if delivered:
                sent_at = _iso(datetime.now(UTC))
            else:
                status = "SKIPPED"  # every recipient switched this type off
        except Exception as exc:
            LOG.exception("daily report email failed; trading is unaffected")
            status = "AMBIGUOUS" if isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError)) else "FAILED"
            error = f"{type(exc).__name__}: {exc}"[:500]
        with self.connect() as conn:
            conn.execute(
                "UPDATE live_daily_reports SET total_usd=?,deal_count=?,wins=?,losses=?,email_status=?,"
                "email_attempts=email_attempts+1,email_sent_at=?,email_last_error=?,updated_at=? WHERE report_date=?",
                (today.get("total_cash_flow_usd"), int(today.get("deal_count") or 0),
                 int(today.get("wins") or 0), int(today.get("losses") or 0),
                 status, sent_at, error, _iso(datetime.now(UTC)), report_date.isoformat()),
            )
            return dict(conn.execute(
                "SELECT * FROM live_daily_reports WHERE report_date=?", (report_date.isoformat(),)
            ).fetchone())
