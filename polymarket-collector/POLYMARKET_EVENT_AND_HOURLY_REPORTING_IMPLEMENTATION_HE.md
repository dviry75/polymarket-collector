# דוח יישום Event + Hourly Reporting

## Executive Summary

נוספה שכבת observability נפרדת מנתיב המסחר: דוח מובנה וייחודי לכל BTC 5m Event, aggregation ייחודי לכל שעה פעילה, ושליחת מייל אחת לכל היותר דרך Google Workspace SMTP Relay. כשל בדיווח נשאר בשירות oneshot נפרד ואינו משנה strategy, readiness, pause, kill switch או orders.

## Initial State

- service: `polymarket-trader.service` active, REAL_TRADING.
- kill switch: false; canary armed: false; canary consumed: true.
- pause entries: true, owner RECONCILIATION, reason RECONCILIATION_GAP, auto recoverable false.
- strategy/reconciliation: READY; Market WS/User WS: CONNECTED; heartbeat: OK.
- מצב זה לא שונה על ידי הפריסה.

## Architecture

`Trading Runtime → existing DB audit/intents/fills/positions → live_event_reports → live_hourly_reports → EmailService → smtp-relay.gmail.com`

ה־scheduler הוא systemd timer נפרד; אין SMTP, polling פרטי או I/O חדש ב־execution/WebSocket path.

## Files Changed

- `live/reporting.py` — schema, finalizer, aggregator, renderer ו־EmailService.
- `scripts/run_reporting.py` — migrate/tick/test-email בטוחים.
- `deploy/polymarket-hourly-report.service` ו־`.timer` — הרצה ב־HH:03.
- `tests/test_reporting.py` — synthetic/inactive/partial/duplicate/failure tests.

## DB Changes

- `live_event_reports`: unique `event_id`, שדות queryable למוכנות, WS, trigger/order/fill/exit/PnL/reconciliation/residuals/status, ו־JSON משלים.
- `live_hourly_reports`: unique `(period_start, period_end, timezone)`, counters, event IDs, מצב מייל, attempts, error ו־Message-ID.
- migration additive בלבד; ללא DROP/DELETE/TRUNCATE.

## Event Report ו־Timeline

כל slot קנוני של 5 דקות מקבל report, גם כש־market discovery חסר; במקרה כזה נשמר `market_metadata_missing` והסטטוס WARNING. Timeline נלקח מ־`live_audit_timeline` הקיים ואינו שומר raw WS/ticks מחדש. Intents מבדילים requested/submitted/remote order/fill/partial fill; deals/positions הם מקור PnL ו־final position.

NO TRADE כולל reason כגון OUTSIDE_TRADING_WINDOW, KILL_SWITCH, PAUSE/RECONCILIATION_GAP, STRATEGY_NOT_READY, RECONCILIATION_NOT_READY, WS_DISCONNECTED או TRIGGER_NOT_REACHED. האחרון הוא החלטת strategy בריאה ולא safety block.

## Reconciliation וסטטוס

הדוח משתמש ב־readiness ובנתוני position/order הקיימים; הוא אינו מפעיל API reconciliation חדש. residual/open order לא צפוי מעלה ERROR; pending/transient/missing metadata מעלה WARNING; NO TRADE עקב trigger שלא הגיע יכול להיות HEALTHY.

## Hourly Aggregation, Schedule ו־Timezone

חלון השעה מחושב ב־`Asia/Jerusalem` עם `ZoneInfo` ו־DST. active coverage קורא בכל דקה ל־`LiveStrategyRuntime.entry_schedule_status`, מקור האמת של האסטרטגיה. שעה inactive מחזירה ללא report/email; חלון חלקי נשלח. grace ברירת מחדל 180 שניות. catch-up מוגבל ל־24 שעות.

## SMTP ו־Failure Isolation

SMTP host/port מגיעים מה־env (ברירת מחדל relay:587), STARTTLS וללא username/password. sender/recipient נטענים מ־smtp.env, כולל הרשימה המופרדת בפסיקים. קובץ הסודות נשאר root:root 0600 ולא הועתק. SMTP failure נשמר כ־FAILED/AMBIGUOUS, עד 3 attempts; SENT לא נשלח שוב. Message-ID דטרמיניסטי לחלון.

## Tests

- Synthetic 12 events → 1 report, 3 trades + 9 no-trades: PASS.
- Duplicate scheduler → 1 DB report + 1 email attempt: PASS.
- Fully inactive hour → 0 email: PASS.
- Partial active coverage → 1 email: PASS.
- SMTP failure persisted/bounded and trading-independent: PASS.
- Application SMTP test through code → relay accepted: PASS.
- Focused suite: `4 passed`.

## Deployment ו־Runtime

- migration: COMPLETE.
- timer installed/enabled/active; oneshot finished SUCCESS.
- application test email: SENT.
- first deployed hourly record: SENT, attempts=1. הוא נוצר לפני תיקון missing-slot שנחשף בהרצת deployment ולכן שמר 9 discovered events; לא שוכתב ולא נשלח שוב כדי לשמור exactly-once. לאחר התיקון קיימים 12 event reports בחלון; החלון הפעיל הבא ישתמש במנגנון המתוקן.
- trader נשאר active; `/health` החזיר status ok ו־strategy readiness READY; safety state נשמר.

## Performance ו־DB Growth

אין API calls נוספים. tick מוגבל ל־24 שעות וההרצה לאחר התיקון הסתיימה בכ־9 שניות (CPU כ־2.2 שניות). סדר גודל: 288 event reports/day + עד 24 hourly rows/day; בהנחת 2–10KB/report מדובר בערך 0.6–3MB/day, לא GB/day. ה־timeline הקיים אינו משוכפל.

## Findings Outside Scope

- Medium: market discovery היסטורי החסיר 3 slots בשעה 10:00–11:00 Israel. reporting כעת משלים slots קנוניים כ־WARNING; מומלץ לחקור בנפרד את סיבת פערי discovery.
- Existing dirty worktree כלל שינויים קודמים ב־reconciliation/WS/runtime/tests; הם נשמרו ולא נכללו במימוש זה.

## Manual LIVE Verification

Full real-hour LIVE verification: **MANUAL VERIFICATION REQUIRED**.

```bash
DB=/opt/polymarket-btc-live/poly_live.sqlite3

# 1,2,4,5,6 — hourly report האחרון
sqlite3 -header -column "$DB" "SELECT id,period_start,period_end,event_count,overall_status,email_status,email_attempts,email_sent_at,email_last_error FROM live_hourly_reports ORDER BY period_end DESC LIMIT 1;"

# 3 — events של הדוח האחרון
sqlite3 -header -column "$DB" "SELECT e.started_at,e.ended_at,e.event_id,e.trade_outcome,e.trading_block_reason,e.reconciliation_status,e.overall_status,e.realized_pnl FROM live_event_reports e JOIN live_hourly_reports h ON e.started_at>=h.period_start AND e.started_at<h.period_end WHERE h.id=(SELECT id FROM live_hourly_reports ORDER BY period_end DESC LIMIT 1) ORDER BY e.started_at;"

# 7 — logs
journalctl -u polymarket-hourly-report.service --since '2 hours ago' --no-pager

# 8 — duplicate reports (חייב להחזיר 0 rows)
sqlite3 -header -column "$DB" "SELECT period_start,period_end,timezone,COUNT(*) copies FROM live_hourly_reports GROUP BY period_start,period_end,timezone HAVING COUNT(*)>1;"

# 9 — duplicate send evidence (לחלון רגיל SENT צריך attempts=1)
sqlite3 -header -column "$DB" "SELECT period_start,period_end,email_status,email_attempts,message_id FROM live_hourly_reports ORDER BY period_end DESC LIMIT 5;"

# runtime health
systemctl is-active polymarket-trader.service polymarket-hourly-report.timer
curl -fsS http://127.0.0.1:8002/health
```

PASS: שעה פעילה שהסתיימה = report אחד; 12 slots בשעה מלאה (או מספר ה־slots בחלון חלקי); email_status SENT; attempts=1 בהרצה רגילה; אין duplicate query rows; מייל רגיל אחד; trader/health תקינים; אין reporting exception שפגע במסחר.

FAIL: אין report בשעה פעילה, 2+ reports, email duplicate, slots חסרים, חלון Israel שגוי, HEALTHY למרות ERROR ידוע, או פגיעה בשירות המסחר.
