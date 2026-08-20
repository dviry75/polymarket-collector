from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .strategy_runtime import LiveStrategyRuntime


LOG = logging.getLogger(__name__)
ISRAEL = ZoneInfo("Asia/Jerusalem")
UTC = timezone.utc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def event_window(event_id: str) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.fromtimestamp(int(event_id.rsplit("-", 1)[1]), UTC)
    except (IndexError, TypeError, ValueError, OSError):
        return None
    return start, start + timedelta(minutes=5)


def hour_window(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(ISRAEL)
    end_local = local.replace(minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(hours=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def active_coverage(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    # Use the exact strategy source of truth at one-minute resolution. This also
    # handles partial windows and ZoneInfo DST transitions without copied rules.
    result: list[tuple[datetime, datetime]] = []
    cursor = start
    opened: datetime | None = None
    while cursor < end:
        allowed = bool(LiveStrategyRuntime.entry_schedule_status(cursor)["allowed"])
        if allowed and opened is None:
            opened = cursor
        if not allowed and opened is not None:
            result.append((opened, cursor))
            opened = None
        cursor += timedelta(minutes=1)
    if opened is not None:
        result.append((opened, end))
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE, condition_id TEXT, slug TEXT,
 started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
 scheduled_trading_active INTEGER NOT NULL, scheduled_active_from TEXT,
 scheduled_active_until TEXT, actual_trading_allowed INTEGER NOT NULL,
 trading_block_reason TEXT, strategy_readiness TEXT NOT NULL,
 reconciliation_readiness TEXT NOT NULL, market_ws_state TEXT NOT NULL,
 user_ws_state TEXT NOT NULL, entry_triggered INTEGER NOT NULL DEFAULT 0,
 entry_side TEXT, entry_trigger_price REAL, entry_trigger_at TEXT,
 entry_requested_price REAL, entry_requested_amount REAL, entry_order_id TEXT,
 filled_quantity REAL NOT NULL DEFAULT 0, average_entry_price REAL,
 entry_fees REAL NOT NULL DEFAULT 0, exit_type TEXT, exit_price REAL,
 exit_quantity REAL NOT NULL DEFAULT 0, exit_fees REAL NOT NULL DEFAULT 0,
 realized_pnl REAL, expected_position REAL, actual_position REAL,
 expected_open_orders INTEGER, actual_open_orders INTEGER,
 reconciliation_status TEXT NOT NULL, market_ws_disconnect_count INTEGER NOT NULL DEFAULT 0,
 market_ws_reconnect_count INTEGER NOT NULL DEFAULT 0,
 user_ws_disconnect_count INTEGER NOT NULL DEFAULT 0,
 user_ws_reconnect_count INTEGER NOT NULL DEFAULT 0,
 stale_data_incidents INTEGER NOT NULL DEFAULT 0, warning_count INTEGER NOT NULL DEFAULT 0,
 error_count INTEGER NOT NULL DEFAULT 0, trade_outcome TEXT NOT NULL,
 overall_status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}',
 report_finalized_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_event_reports_window ON live_event_reports(started_at,ended_at);
CREATE TABLE IF NOT EXISTS live_hourly_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT, period_start TEXT NOT NULL,
 period_end TEXT NOT NULL, timezone TEXT NOT NULL,
 scheduled_active INTEGER NOT NULL, active_coverage TEXT NOT NULL,
 event_count INTEGER NOT NULL, trade_count INTEGER NOT NULL,
 no_trade_count INTEGER NOT NULL, tp_count INTEGER NOT NULL, sl_count INTEGER NOT NULL,
 realized_pnl REAL, healthy_event_count INTEGER NOT NULL,
 warning_event_count INTEGER NOT NULL, error_event_count INTEGER NOT NULL,
 reconciliation_pass_count INTEGER NOT NULL, reconciliation_warning_count INTEGER NOT NULL,
 reconciliation_fail_count INTEGER NOT NULL, reconciliation_pending_count INTEGER NOT NULL,
 market_ws_disconnect_count INTEGER NOT NULL, user_ws_disconnect_count INTEGER NOT NULL,
 stale_data_incidents INTEGER NOT NULL, unexpected_residual_positions INTEGER NOT NULL,
 unexpected_open_orders INTEGER NOT NULL, overall_status TEXT NOT NULL,
 event_ids_json TEXT NOT NULL, generated_at TEXT NOT NULL,
 email_status TEXT NOT NULL DEFAULT 'PENDING', email_attempts INTEGER NOT NULL DEFAULT 0,
 email_sent_at TEXT, email_last_error TEXT, message_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(period_start,period_end,timezone)
);
"""


@dataclass(frozen=True)
class SMTPSettings:
    host: str = "smtp-relay.gmail.com"
    port: int = 587
    sender: str = ""
    recipients: tuple[str, ...] = ()
    timeout_seconds: int = 15

    @classmethod
    def from_env(cls) -> "SMTPSettings":
        recipients = os.getenv("HOURLY_REPORT_RECIPIENTS", os.getenv("SMTP_RECIPIENTS", os.getenv("SMTP_TO", os.getenv("REPORT_EMAIL_TO", os.getenv("SMTP_ALERT_TO", "")))))
        return cls(
            host=os.getenv("SMTP_HOST", "smtp-relay.gmail.com"),
            port=int(os.getenv("SMTP_PORT", "587")),
            sender=os.getenv("SMTP_FROM", os.getenv("HOURLY_REPORT_FROM", os.getenv("SMTP_FROM_EMAIL", ""))),
            recipients=tuple(x.strip() for x in recipients.split(",") if x.strip()),
            timeout_seconds=int(os.getenv("SMTP_TIMEOUT_SECONDS", "15")),
        )


class EmailService:
    def __init__(self, settings: SMTPSettings, transport: Callable[[EmailMessage], None] | None = None):
        self.settings = settings
        self.transport = transport or self._smtp_send

    def _smtp_send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.settings.host, self.settings.port, timeout=self.settings.timeout_seconds) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.send_message(message)

    def send(self, subject: str, text: str, html_body: str, message_id: str) -> None:
        if not self.settings.sender or not self.settings.recipients:
            raise RuntimeError("SMTP_FROM and HOURLY_REPORT_RECIPIENTS are required")
        msg = EmailMessage()
        msg["From"] = self.settings.sender
        msg["To"] = ", ".join(self.settings.recipients)
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        self.transport(msg)

    def send_test(self) -> None:
        self.send("Polymarket Application SMTP Test", "Application SMTP path is operational.",
                  "<p>Application SMTP path is operational.</p>",
                  f"<smtp-test-{int(datetime.now(UTC).timestamp())}@polymarket-live>")


class ReportingService:
    def __init__(self, db_path: str | Path, email: EmailService | None = None):
        self.db_path = str(db_path)
        self.email = email or EmailService(SMTPSettings.from_env())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def _states(conn: sqlite3.Connection) -> dict[str, str]:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM live_system_state")}

    def finalize_events(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        finalized = 0
        with self.connect() as conn:
            states = self._states(conn)
            cutoff = int((now - timedelta(hours=int(os.getenv("REPORTING_CATCHUP_HOURS", "24")))).timestamp())
            markets = list(conn.execute("SELECT event_id,condition_id,raw_market_info FROM live_markets WHERE event_id LIKE 'btc-updown-5m-%' AND CAST(REPLACE(event_id,'btc-updown-5m-','') AS INTEGER) >= ?", (cutoff,)).fetchall())
            known = {row["event_id"] for row in markets}
            slot = cutoff - (cutoff % 300)
            while slot + 300 <= int(now.timestamp()):
                event_id = f"btc-updown-5m-{slot}"
                if event_id not in known:
                    markets.append({"event_id": event_id, "condition_id": None, "raw_market_info": json.dumps({"slug": event_id, "reporting_synthetic_slot": True})})
                slot += 300
            for market in markets:
                window = event_window(market["event_id"])
                if not window or window[1] + timedelta(seconds=30) > now:
                    continue
                if conn.execute("SELECT 1 FROM live_event_reports WHERE event_id=?", (market["event_id"],)).fetchone():
                    continue
                self._finalize_one(conn, market, states, window, now)
                finalized += 1
        return finalized

    def _finalize_one(self, conn: sqlite3.Connection, market: sqlite3.Row,
                      states: dict[str, str], window: tuple[datetime, datetime], now: datetime) -> None:
        event_id, condition_id = market["event_id"], market["condition_id"]
        coverage = active_coverage(*window)
        timeline = conn.execute("SELECT * FROM live_audit_timeline WHERE event_id=? ORDER BY occurred_at", (event_id,)).fetchall()
        intents = conn.execute("SELECT * FROM live_strategy_intents WHERE event_id=? ORDER BY created_at", (event_id,)).fetchall()
        deal = conn.execute("SELECT * FROM live_strategy_deals WHERE event_id=?", (event_id,)).fetchone()
        entry = next((x for x in intents if x["action"] == "ENTRY"), None)
        exits = [x for x in intents if x["action"] == "EXIT"]
        severity = [str(x["severity"]).upper() for x in timeline]
        warnings, errors = severity.count("WARNING"), severity.count("ERROR") + severity.count("CRITICAL")
        reasons = [str(x["reason_code"]) for x in reversed(timeline) if x["reason_code"]]
        scheduled = bool(coverage)
        strategy = states.get("strategy_readiness", "UNKNOWN")
        recon = states.get("reconciliation_readiness", "UNKNOWN")
        market_ws, user_ws = states.get("market_ws_status", "UNKNOWN"), states.get("user_ws_status", "UNKNOWN")
        block = ""
        if not scheduled: block = "OUTSIDE_TRADING_WINDOW"
        elif states.get("kill_switch") == "true": block = "KILL_SWITCH"
        elif states.get("pause_entries") == "true": block = states.get("pause_reason") or "PAUSE_ENTRIES"
        elif strategy != "READY": block = "STRATEGY_NOT_READY"
        elif recon != "READY": block = "RECONCILIATION_NOT_READY"
        elif market_ws != "CONNECTED": block = "MARKET_WS_DISCONNECTED"
        elif user_ws not in ("CONNECTED", "NOT_CONFIGURED"): block = "USER_WS_DISCONNECTED"
        elif not entry: block = reasons[0] if reasons else "TRIGGER_NOT_REACHED"
        actual_allowed = scheduled and (not block or block == "TRIGGER_NOT_REACHED")
        trade = entry is not None
        fill = float(entry["filled_shares_text"] or 0) if entry else 0.0
        exit_fill = sum(float(x["filled_shares_text"] or 0) for x in exits)
        exit_type = (deal["final_reason"] if deal else None) or (exits[-1]["purpose"] if exits else None)
        recon_status = "PASS" if recon == "READY" else "PENDING"
        residual = conn.execute("SELECT COALESCE(SUM(CAST(remaining_shares_text AS REAL)),0) FROM live_strategy_positions WHERE event_id=? AND state NOT IN ('CLOSED','SETTLED','REDEEMED')", (event_id,)).fetchone()[0]
        open_orders = conn.execute("SELECT COUNT(*) FROM live_strategy_intents WHERE event_id=? AND state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED')", (event_id,)).fetchone()[0]
        if residual or open_orders: errors += 1
        overall = "ERROR" if errors else "WARNING" if warnings or recon_status == "PENDING" or market_ws != "CONNECTED" else "HEALTHY"
        disconnects = lambda source: sum(1 for x in timeline if source in str(x["component"]).upper() and "DISCONNECT" in str(x["reason_code"] or x["requested_action"]).upper())
        reconnects = lambda source: sum(1 for x in timeline if source in str(x["component"]).upper() and "RECONNECT" in str(x["reason_code"] or x["requested_action"]).upper())
        stale = sum(1 for x in timeline if "STALE" in str(x["reason_code"] or "").upper())
        meta = {"timeline_rows": len(timeline), "intent_states": [dict(x) for x in intents], "no_trade_reason": block if not trade else None}
        raw = json.loads(market["raw_market_info"] or "{}")
        if raw.get("reporting_synthetic_slot"):
            warnings += 1
            overall = "WARNING" if overall == "HEALTHY" else overall
            meta["market_metadata_missing"] = True
        values = (event_id, condition_id, raw.get("slug") or event_id, _iso(window[0]), _iso(window[1]), int(scheduled),
                  _iso(coverage[0][0]) if coverage else None, _iso(coverage[-1][1]) if coverage else None, int(actual_allowed), block or None,
                  strategy, recon, market_ws, user_ws, int(trade), entry["side"] if entry else None,
                  float((deal["trigger_price_text"] if deal else None) or 0) or None, entry["created_at"] if entry else None,
                  float(entry["price_limit_text"] or 0) if entry else None, float(entry["requested_amount_text"] or 0) if entry else None,
                  entry["remote_order_id"] if entry else None, fill, float(entry["average_price_text"] or 0) if entry else None,
                  float(entry["fee_text"] or 0) if entry else 0, exit_type, float(exits[-1]["average_price_text"] or 0) if exits else None,
                  exit_fill, sum(float(x["fee_text"] or 0) for x in exits), float(deal["realized_pnl_text"] or 0) if deal else None,
                  0.0, float(residual), 0, int(open_orders), recon_status, disconnects("MARKET"), reconnects("MARKET"),
                  disconnects("USER"), reconnects("USER"), stale, warnings, errors, "TRADE" if trade else "NO_TRADE", overall,
                  json.dumps(meta, default=str, sort_keys=True), _iso(now), _iso(now), _iso(now))
        conn.execute("INSERT INTO live_event_reports(event_id,condition_id,slug,started_at,ended_at,scheduled_trading_active,scheduled_active_from,scheduled_active_until,actual_trading_allowed,trading_block_reason,strategy_readiness,reconciliation_readiness,market_ws_state,user_ws_state,entry_triggered,entry_side,entry_trigger_price,entry_trigger_at,entry_requested_price,entry_requested_amount,entry_order_id,filled_quantity,average_entry_price,entry_fees,exit_type,exit_price,exit_quantity,exit_fees,realized_pnl,expected_position,actual_position,expected_open_orders,actual_open_orders,reconciliation_status,market_ws_disconnect_count,market_ws_reconnect_count,user_ws_disconnect_count,user_ws_reconnect_count,stale_data_incidents,warning_count,error_count,trade_outcome,overall_status,summary_json,report_finalized_at,created_at,updated_at) VALUES (" + ",".join("?" for _ in values) + ")", values)

    def generate_hour(self, start: datetime, end: datetime, *, send: bool = True, delayed: bool = False) -> dict[str, Any] | None:
        coverage = active_coverage(start, end)
        if not coverage:
            return None
        now = datetime.now(UTC)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM live_event_reports WHERE started_at>=? AND started_at<? ORDER BY started_at", (_iso(start), _iso(end))).fetchall()
            existing = conn.execute("SELECT * FROM live_hourly_reports WHERE period_start=? AND period_end=? AND timezone='Asia/Jerusalem'", (_iso(start), _iso(end))).fetchone()
            if existing:
                report = dict(existing)
            else:
                report = self._insert_hour(conn, rows, start, end, coverage, now, delayed)
            if send and report["email_status"] != "SENT" and int(report["email_attempts"]) < 3:
                report = self._send_hour(conn, report, rows, delayed)
            return report

    def _insert_hour(self, conn: sqlite3.Connection, rows: list[sqlite3.Row], start: datetime, end: datetime,
                     coverage: list[tuple[datetime, datetime]], now: datetime, delayed: bool) -> dict[str, Any]:
        count = lambda key, value: sum(1 for r in rows if r[key] == value)
        sums = lambda key: sum(float(r[key] or 0) for r in rows)
        recon = {s: count("reconciliation_status", s) for s in ("PASS", "WARNING", "FAIL", "PENDING")}
        overall = "ERROR" if count("overall_status", "ERROR") else "WARNING" if delayed or count("overall_status", "WARNING") or not rows else "HEALTHY"
        cov = json.dumps([[ _iso(a), _iso(b)] for a,b in coverage])
        ids = json.dumps([r["event_id"] for r in rows])
        msgid = f"<hourly-{int(start.timestamp())}-{hashlib.sha256(_iso(start).encode()).hexdigest()[:12]}@polymarket-live>"
        vals = (_iso(start),_iso(end),"Asia/Jerusalem",1,cov,len(rows),count("trade_outcome","TRADE"),count("trade_outcome","NO_TRADE"),
                sum(1 for r in rows if str(r["exit_type"] or "").upper().startswith("TP")),sum(1 for r in rows if "SL" in str(r["exit_type"] or "").upper()),
                sums("realized_pnl"),count("overall_status","HEALTHY"),count("overall_status","WARNING"),count("overall_status","ERROR"),
                recon["PASS"],recon["WARNING"],recon["FAIL"],recon["PENDING"],sums("market_ws_disconnect_count"),sums("user_ws_disconnect_count"),
                sums("stale_data_incidents"),sum(1 for r in rows if float(r["actual_position"] or 0)!=0),sum(1 for r in rows if int(r["actual_open_orders"] or 0)!=0),
                overall,ids,_iso(now),"PENDING",0,msgid,_iso(now),_iso(now))
        conn.execute("INSERT INTO live_hourly_reports(period_start,period_end,timezone,scheduled_active,active_coverage,event_count,trade_count,no_trade_count,tp_count,sl_count,realized_pnl,healthy_event_count,warning_event_count,error_event_count,reconciliation_pass_count,reconciliation_warning_count,reconciliation_fail_count,reconciliation_pending_count,market_ws_disconnect_count,user_ws_disconnect_count,stale_data_incidents,unexpected_residual_positions,unexpected_open_orders,overall_status,event_ids_json,generated_at,email_status,email_attempts,message_id,created_at,updated_at) VALUES ("+",".join("?" for _ in vals)+")", vals)
        return dict(conn.execute("SELECT * FROM live_hourly_reports WHERE period_start=? AND period_end=? AND timezone='Asia/Jerusalem'", (_iso(start),_iso(end))).fetchone())

    def _render(self, report: dict[str, Any], rows: list[sqlite3.Row], delayed: bool) -> tuple[str,str,str]:
        start, end = _dt(report["period_start"]).astimezone(ISRAEL), _dt(report["period_end"]).astimezone(ISRAEL)
        label = f"{start:%H:%M}–{end:%H:%M}"
        status = report["overall_status"]
        subject = (f"❌ Polymarket Hourly Report — {label} — ERROR" if status == "ERROR" else
                   f"⚠️ Polymarket Hourly Report — {label} — {'DELAYED' if delayed else str(report['warning_event_count'])+' WARNINGS'}" if status == "WARNING" else
                   f"✅ Polymarket Hourly Report — {label} — HEALTHY")
        lines = ["POLYMARKET HOURLY REPORT",f"Period: {label} Israel",f"Overall Status: {status}","",
                 f"Events: {report['event_count']}",f"Trades: {report['trade_count']}",f"No Trades: {report['no_trade_count']}",
                 f"Take Profits: {report['tp_count']}",f"Stop Losses: {report['sl_count']}",f"Realized PnL: ${float(report['realized_pnl'] or 0):+.2f}",
                 f"Warnings: {report['warning_event_count']}",f"Errors: {report['error_event_count']}","", "EVENTS"]
        for row in rows:
            a,b=_dt(row["started_at"]).astimezone(ISRAEL),_dt(row["ended_at"]).astimezone(ISRAEL)
            detail = row["trade_outcome"] + ((f" — {row['trading_block_reason']}") if row["trade_outcome"]=="NO_TRADE" else f" — {row['entry_side'] or ''} — PnL ${float(row['realized_pnl'] or 0):+.2f}")
            lines.append(f"{a:%H:%M}–{b:%H:%M} {row['overall_status']} {detail}")
        text = "\n".join(lines)
        body = "<html><body><pre style='font-family:system-ui;white-space:pre-wrap'>"+html.escape(text)+"</pre></body></html>"
        return subject,text,body

    def _send_hour(self, conn: sqlite3.Connection, report: dict[str, Any], rows: list[sqlite3.Row], delayed: bool) -> dict[str, Any]:
        attempts=int(report["email_attempts"])+1
        conn.execute("UPDATE live_hourly_reports SET email_status='SENDING',email_attempts=?,updated_at=? WHERE id=?",(attempts,_iso(datetime.now(UTC)),report["id"]))
        try:
            subject,text,body=self._render(report,rows,delayed)
            self.email.send(subject,text,body,report["message_id"])
        except Exception as exc:
            LOG.exception("hourly reporting email failed; trading is unaffected")
            status="AMBIGUOUS" if isinstance(exc,(smtplib.SMTPServerDisconnected,TimeoutError)) else "FAILED"
            conn.execute("UPDATE live_hourly_reports SET email_status=?,email_last_error=?,updated_at=? WHERE id=?",(status,f"{type(exc).__name__}: {exc}"[:500],_iso(datetime.now(UTC)),report["id"]))
        else:
            sent=_iso(datetime.now(UTC))
            conn.execute("UPDATE live_hourly_reports SET email_status='SENT',email_sent_at=?,email_last_error=NULL,updated_at=? WHERE id=?",(sent,sent,report["id"]))
        return dict(conn.execute("SELECT * FROM live_hourly_reports WHERE id=?",(report["id"],)).fetchone())

    def tick(self, now: datetime | None = None, *, send: bool = True) -> dict[str, Any] | None:
        now=(now or datetime.now(UTC)).astimezone(UTC)
        self.finalize_events(now)
        grace=int(os.getenv("HOURLY_REPORT_GRACE_SECONDS","180"))
        start,end=hour_window(now-timedelta(seconds=grace))
        return self.generate_hour(start,end,send=send,delayed=now>end+timedelta(seconds=grace*2))
