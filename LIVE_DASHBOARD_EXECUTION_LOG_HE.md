# יומן ביצוע — Dashboard LIVE Real Data

עודכן: 2026-08-12 17:40 UTC. אין בקובץ secrets או מזהים גולמיים.

## מצב נוכחי בטוח

- `polymarket-trader.service`: inactive, disabled; פורט 8002 סגור.
- flags בכל קובצי LIVE: `LIVE_TRADING_ENABLED=false`, `LIVE_ORDER_SUBMISSION_ENABLED=false`, `LIVE_KILL_SWITCH=true`, `LIVE_PAUSE_ENTRIES=true`.
- לא נשלחה/בוטלה פקודה, לא נסגרה/נפדתה פוזיציה ולא בוצעה עסקת בדיקה.
- `polymarket-dashboard.service`: active על 127.0.0.1:8001, query-only.
- `/live-status/`: מוגן ב־nginx `auth_request`, ללא session מחזיר 302 למסך login.

## נקודות שחזור

- config: `/opt/polymarket-btc-live/deployment-backups/live-dashboard-complete-20260812-preflight`.
- DB: `/opt/polymarket-btc-live/backups/live-dashboard-complete-20260812/poly_live.pre-dashboard.sqlite3`.
- DB SHA-256: `0fb36dd91366dab50b91b8bab7d67c9c76cb67d1dd401fe27e05a324cb4864cc`.
- static build: `/opt/polymarket-btc-live/backups/live-dashboard-complete-20260812/live-status.pre-dashboard` וגם `/var/www/live-status.rollback-20260812`.
- `PRAGMA quick_check` production לאחר migrations: `ok`.

## שלבים שבוצעו

1. preflight מלא, baseline tests: 199 passed + 9 subtests.
2. reconciliation read-only אחרון נקי; drain; trader stopped+disabled.
3. backup עקבי ו־staging DB; integrity תקין.
4. migrations v1–v4 על staging ואז production.
5. cutover LIVE: `2026-08-12T17:19:24.026757+00:00`.
6. provenance triggers INSERT/UPDATE; legacy נשאר UNKNOWN.
7. Dashboard API v1: 14 GET endpoints, auth, pagination, query bounds/deadline, stable errors, short single-flight cache.
8. Frontend חובר ל־API בלבד; אין Mock; כל controls מסחריים disabled.
9. nginx protected static; legacy `/live` מפנה לדאשבורד הקנוני לאחר auth.
10. בדיקות: Backend 212 passed + 9 subtests; Frontend 5/5, lint, build.
11. Polymarket authenticated read-only: cash 44.326404, open orders 0, trades 5, positions 1; 0 write methods.
12. load: 1/5/20 tabs; 130/130 HTTP 200; p95 66.92/148.33/956.62 ms; no observed DB lock/busy.

## חסמים שנותרו

- אין Chromium/Firefox/Playwright/Puppeteer מותקנים, ולכן לא בוצע browser automation אמיתי, console inspection או screenshot viewport. לא הותקנה חבילה.
- אין נתונים פיננסיים post-cutover כשה־trader כבוי; בהתאם מוצג `UNAVAILABLE`, לא 0 ולא legacy.
- Market/User WS ו־trading-loop latency אינם ניתנים למדידה כשה־trader כבוי.

המשך בטוח: אין להפעיל את trader. יש להשלים Git/report, ולאחר מכן audit סופי של services/flags/status.
