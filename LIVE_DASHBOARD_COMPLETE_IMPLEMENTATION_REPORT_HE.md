# דוח יישום מלא — Dashboard LIVE Real Data

תאריך: 2026-08-12
כתובת: https://live-poly.dvirtechnologies.com/live-status/
סטטוס מסירה: **חלקי — היישום והפריסה הושלמו; חסר אימות דפדפן אוטומטי אמיתי מפני שאין דפדפן מותקן בשרת.**

## 1. תקציר מנהלים

הדאשבורד הסטטי הקיים חובר ל־API ייעודי, מאומת ו־read-only. נתונים פיננסיים נכללים רק לאחר cutover ורק כאשר `environment=LIVE`, `execution_mode=REAL_TRADING` ו־verification מתאים. מידע legacy לא קיבל backfill מומצא. כאשר אין הוכחת מקור, ה־UI מציג `UNAVAILABLE` ולא 0.

ה־trader הושבת בצורה מבוקרת ונשאר `inactive` ו־`disabled`. ארבעת flags התפעוליים בסיום הם: trading=false, submission=false, kill=true, pause=true. לא נשלחה או בוטלה פקודה, לא נסגרה או נפדתה פוזיציה ולא בוצעה עסקת בדיקה.

הפריסה עברה 212 בדיקות Backend ועוד 9 subtests, 5 בדיקות Frontend, lint ו־production build. בדיקת עומס של 20 טאבים השלימה 100/100 requests ב־200 עם p95 של 956.62ms וללא שגיאת lock/busy שנצפתה. `PRAGMA quick_check` לאחר migrations החזיר `ok`.

## 2. מצב לפני העבודה

- Backend: `/opt/polymarket-btc-live/repo`, branch התחלתי `architecture/trading-core-refactor-20260808`, HEAD התחלתי `222fcc0`.
- Frontend: `/home/dvir/polymarket-dashboard-preview`, branch התחלתי `feature/live-dashboard-real-data-20260811`, HEAD התחלתי `208d919`.
- build פעיל: `/var/www/live-status`.
- DB: `/opt/polymarket-btc-live/poly_live.sqlite3`, SQLite WAL, כ־1.42GB לפני migration.
- הדאשבורד הישן קרא `/live/health` ו־`/live/strategy/status`; לא היה read model פיננסי post-cutover.
- היו שינויים מקומיים רבים ב־Backend ושני שינויים ב־Frontend לפני העבודה. הם לא נמחקו ולא נדרסו; commits נבנו באופן סלקטיבי.

## 3. repositories, branches ו־commits

| Repository | Remote | Branch | Commit |
|---|---|---|---|
| Backend | `dviry75/polymarket-collector` | `feature/complete-live-dashboard-real-data-20260812` | `dfa57c8` — API, provenance, tests, deployment; `e030a16` — rollout report |
| Frontend | `dviry75/polymarket-dashboard` | `feature/complete-live-dashboard-real-data-20260812` | `cdbb7ad` — real-data UI, tests, static export |

## 4. הארכיטקטורה הסופית

`Browser → nginx auth_request → static /live-status → same-origin /live/dashboard/v1/* → FastAPI dashboard process → SQLite mode=ro/query_only`.

- תהליך dashboard נפרד על `127.0.0.1:8001`.
- תהליך trader נפרד על 8002, אך כבוי.
- אין תלות systemd של dashboard ב־trader.
- אין browser-to-Polymarket.
- נתוני תשתית cached ל־15 שניות; endpoints כבדים משתמשים ב־cache של 2 שניות עם single-flight.
- evidence: `dashboard_api.py:48,107,134`; `dashboard_read_model.py:158-177`; `polymarket-dashboard.service:3,14,37-38`.

## 5. החלטות ארכיטקטוניות

1. SQLite נשאר מקור האמת; WAL מאפשר readers בלי מעבר DB מאולתר.
2. read model נפרד כדי למנוע חשיפת payloads/IDs ולעבוד fail-closed.
3. polling 10 שניות בטאב גלוי ו־60 שניות בטאב מוסתר; אין requests חופפות (`page.tsx:47-52`).
4. auth זהה במבנה session הקיים, אך entrypoint dashboard עצמאי ואינו תלוי ב־IPC לצורך login.
5. controls נשארו UI disabled; לא נוצר command endpoint.
6. היסטוריה לפני cutover מוחרגת במקום להיות מסומנת LIVE בלי הוכחה.

## 6. קבצים ששונו

Backend commit:

- `polymarket-collector/dashboard_app.py`
- `polymarket-collector/live/dashboard_api.py`
- `polymarket-collector/live/dashboard_read_model.py`
- `polymarket-collector/live/dashboard_schema.py`
- `polymarket-collector/live/dashboard_infrastructure.py`
- `polymarket-collector/live/ipc.py`
- `polymarket-collector/live/config.py`
- hooks מינימליים ב־`live/router.py` וב־`live/reconciliation.py`
- `scripts/migrate_dashboard_schema.py`
- `tests/test_dashboard_v1.py`
- `deploy/dashboard.env.example`
- `deploy/polymarket-dashboard.service`
- `LIVE_DASHBOARD_EXECUTION_LOG_HE.md`

Frontend commit:

- `app/page.tsx`, `app/live-data.ts`, `app/live.css`, `app/layout.tsx`
- `next.config.ts`, `package.json`
- `tests/frontend-contract.test.mjs`

שרת:

- `/etc/polymarket-live/{trader,dashboard,live}.env` — flags/provenance/TTL בלבד.
- `/etc/systemd/system/polymarket-dashboard.service`.
- `/etc/nginx/sites-available/live-poly`.
- `/var/www/live-status` בפריסה אטומית.

## 7. migrations ו־schema

| Version | שם | תוכן |
|---|---|---|
| 1 | `dashboard_provenance_v1` | עמודות provenance, cutover/history tables, INSERT triggers, indexes |
| 2 | `dashboard_provenance_update_triggers_v2` | provenance אוטומטי גם ב־upsert/UPDATE עתידי |
| 3 | `dashboard_fee_verification_v3` | `fee_verification_status`, `fee_source`; legacy נשאר UNKNOWN |
| 4 | `dashboard_query_indexes_v4` | indexes ל־markets, alerts ו־reconciliation |

ה־migration additive ו־idempotent (`dashboard_schema.py:170`). CLI דורש `--allow-live` מפורש ל־LIVE. קיימים 28 provenance triggers ב־production. `quick_check=ok`.

## 8. provenance ו־LIVE isolation

הישויות המכוסות: orders, order fills, positions, account snapshots, websocket events, market snapshots/markets, event states, strategy intents, order attempts, strategy fills, strategy positions, strategy deals ו־audit timeline (`dashboard_schema.py:27-57`).

השדות: execution mode, environment, run ID, strategy ID/version, source, source timestamp, ingestion timestamp, reconciliation status ו־verification status. חסר context מקבל `UNKNOWN`; הוא אינו מקבל LIVE כברירת מחדל. Reconciliation נקי מקדם רק rows post-cutover עם remote evidence (`dashboard_schema.py:338`).

Isolation נבדק באמצעות שורות LIVE/PAPER/UNKNOWN באותו fixture: רק LIVE/REAL_TRADING נכלל.

## 9. cutover ואימות היסטוריה

- cutover: `2026-08-12T17:19:24.026757+00:00`.
- run: `dashboard-cutover-20260812`.
- היסטוריה שאומתה וסווגה מחדש: 0.
- היסטוריה שהוחרגה: 5 deals, 4 positions, 5 strategy fills; כולן `UNKNOWN`.
- לא בוצע backfill לפי תאריך בלבד.
- authenticated read-only מול Polymarket החזיר: available cash `44.326404`, 0 open orders, 5 remote trades ו־1 position; `write_methods_called=0`.
- הנתונים תואמים snapshot DB אחרון, אך הם קודמים ל־cutover ולכן אינם מוצגים כ־LIVE רשמי.

## 10. Dashboard API

כל 14 endpoints הבאים הם GET ומחייבים session:

| Path | תכלית | bounds/cache |
|---|---|---|
| `/overview` | snapshot מרוכז | range ≤90d; cache/single-flight 2s |
| `/equity` | cash/reserved/positions/claimable/equity | fail-closed |
| `/pnl/summary` | lifecycle stats | range ≤90d |
| `/pnl/timeseries` | daily Jerusalem buckets | cache 2s |
| `/trades` | history | page 1..1M, page size 1..100, cache 2s |
| `/positions` | open verified positions | masked IDs |
| `/orders` | open verified intents | pagination 1..100 |
| `/markets` | current/next, YES/NO | no raw payload |
| `/activity` | sanitized activity | limit 1..100 |
| `/alerts` | active alerts | max 100 |
| `/infrastructure` | CPU/RAM/disk/DB/services | cache 15s |
| `/health` | detailed authenticated health | trader-off aware |
| `/session` | auth/CSRF for logout | no-store |
| `/filters` | ranges/page sizes/qualities | metadata |

Stable errors: `AUTHENTICATION_REQUIRED`, `FORBIDDEN`, `INVALID_QUERY`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE`, `INTERNAL_ERROR`; אין stack trace. ראיה: `dashboard_api.py:175-291`.

Public `/health` מחזיר רק `{"status":"ok|degraded"}`.

## 11. מיפוי UI למקור נתונים

52 fields/controls חוברו או קיבלו מצב מפורש, בקבוצות הבאות:

| קבוצת UI | שדות | מקור |
|---|---:|---|
| account/equity | 5 | verified account snapshot + intents + positions + redemption |
| current/next market | 10 | `live_markets`, post-cutover provenance/freshness |
| open positions | 8 | verified strategy position + latest best bid |
| P&L/chart | 8 | verified deal lifecycle, Jerusalem daily bucket |
| trade history | 7 | verified deals, server pagination |
| open orders | 5 | verified strategy intents |
| operational safety | 7 | system state + trader IPC availability |
| infrastructure | 8 | `/proc`, `statvfs`, DB stat, `systemctl show` |
| controls | 6 | disabled בלבד; ללא handler מסחרי |
| auth/refresh | 4 | session, server time, API as_of, logout |

חלק מהשדות חופפים בין כרטיסים; ספירת רכיבי display ייחודיים היא 52. מקור Production היחיד מוגדר ב־`live-data.ts:14`; אין Mock fallback.

## 12. נוסחאות פיננסיות

- remaining attributed cost: `cost_all_in × remaining_shares / acquired_shares`.
- conservative position value: `sellable_shares × fresh current_best_bid`.
- unrealized P&L: `conservative value - remaining attributed cost`.
- realized P&L: ערך lifecycle שנבנה מ־executed proceeds פחות attributed cost ופחות fees; הסיכום אינו סופר orders.
- reserved: `max(0, max_spend/requested_amount - executed fill notional)` עבור intent פעיל מאומת.
- claimable: remaining winning shares רק ב־`REDEEM_PENDING` מאומת.
- total equity: `cash + reserved + conservative positions + claimable`, עם buckets בלעדיים. Winning redeem-pending מקבל position value=0 כדי למנוע double count.
- trade count/win rate/profit factor: closed verified lifecycle, לא orders/fills בודדים.
- fee חסרה/UNKNOWN: `fees_usd=null`, quality=`PARTIAL`; לעולם לא 0 מומצא.
- stale/missing bid: value ו־unrealized null, quality `STALE/UNAVAILABLE`.

ראיה: `dashboard_read_model.py:213-447`.

## 13. partial fills ו־double-count prevention

- quantity נלקחת מ־fills בלבד.
- open remainder משויך יחסית לעלות weighted average.
- reserved מפחית notional שכבר מולא.
- remote trade ID ייחודי במבנה הקיים; בדיקות baseline מכסות duplicate/multiple/partial fills.
- partial exit מפחית remaining shares והעלות המיוחסת.
- claimable ופוזיציה הם buckets בלעדיים.
- canceled unfilled order אינו deal/trade.

## 14. זמן, timezone ו־DST

אחסון מקור ב־UTC. פילטר “היום” הוא `[00:00, 00:00 next)` ב־`Asia/Jerusalem`. בדיקות מאמתות יום אביב של 23 שעות ויום סתיו של 25 שעות. טווח custom כולל את תאריך הסיום ומוגבל ל־90 יום.

## 15. Authentication ואבטחה

- nginx `auth_request` מגן גם על ה־HTML/assets; ללא session מתקבל 302 ל־`/live/login?next=/live-status/`.
- כל endpoint פיננסי/תפעולי מחזיר 401 ללא session.
- cookie: Secure, HttpOnly, SameSite=Strict, Path=/, TTL 8 שעות.
- logout דורש same-origin + session + CSRF.
- login rate limit נבדק: `401×5`, לאחר מכן `429`.
- API rate limit: 600 requests/min/session.
- CSP, HSTS, X-Frame-Options DENY, nosniff, no-referrer, Permissions-Policy.
- `Cache-Control: no-store, private`.
- dashboard env אינו מכיל API key/secret/passphrase/private key/operator token.
- כתובות ו־IDs אינם מוצגים במלואם; raw payload אינו מוחזר.
- login/logout/auth evidence: `dashboard_app.py:108-174`.

## 16. nginx ו־systemd

- nginx syntax נבדק לפני reload והצליח.
- build הועבר אטומית; build קודם נשמר.
- dashboard unit אינו `Requires=trader`.
- service sandbox: code/venv/backups/DB file read-only; parent writable רק כדי לאפשר SQLite WAL/SHM. ה־repository עצמו `mode=ro` + `PRAGMA query_only`.
- dashboard: active/running, PID משתנה, NRestarts=0 לאחר העלייה האחרונה.
- trader: inactive/dead, disabled, PID=0, port 8002 סגור.
- nginx: reloaded בלבד; לא נעצר.

## 17. הטיפול בפרונט הישן

לא היה process/service Frontend נפרד; ה־UI הישן היה HTML מתוך Backend. ה־router הישן אינו נטען עוד ב־dashboard process. נשמרו login/logout/session בלבד. `/live` מפנה ל־`/live-status/` לאחר auth או ל־login ללא auth. קוד המקור לא נמחק.

## 18. Frontend states והתנהגות

ממומשים: loading, auth expiry, error עם data-age, empty, stale, partial, estimated ו־unavailable. נתון null מוצג `—`, לא 0. polling אינו מוכפל עם render, request קודם מבוטל ב־AbortController, timeout 8s, tab hidden 60s. mobile CSS נשמר ומורחב. controls: 6 disabled, ללא state mutation שנראה כפקודה.

## 19. בדיקות

Backend סופי: `212 passed, 9 subtests passed`, 7 warnings deprecation בלבד. 14 בדיקות dashboard ממוקדות: provenance, idempotency, UPDATE/upsert, isolation, partial fill, stale bid, missing fee, claimable exclusivity, false-zero, DST, auth, GET-only, pagination, rate/query bounds ו־single-flight.

Frontend:

- `npm test`: 5/5.
- `npm run lint`: pass.
- `npm run build`: pass; static export כולל `/` ו־`/_not-found` תחת basePath `/live-status`.
- production: HTML 200 עם session; 10/10 assets 200; no-session 302; API no-session 401; logout 200 ומוחק cookie.

## 20. אינטגרציה UI/API/DB/Polymarket

- UI קורא רק 5 requests מרוכזים: overview, timeseries, trades, infrastructure, session.
- API meta: LIVE / REAL_TRADING / cutover נכון.
- DB: 4 migrations, 28 triggers, legacy counts תואמים את ההחרגה.
- Polymarket read-only balance `44.326404` תואם DB latest snapshot.
- Polymarket open orders=0 תואם reconciliation האחרון, אבל API post-cutover נשאר UNAVAILABLE עד reconciliation חדש — fail-closed.
- positions remote=1 תואם public_positions_count=1; הרשומה המקומית legacy מוחרגת.
- trade count remote=5 תואם מספר fills/deals היסטורי, אך 0 מוצג רשמית post-cutover.
- אין best bid post-cutover כי trader/WS כבויים; current market מוצג UNAVAILABLE.

## 21. ביצועים

לפני single-flight:

- 1 tab p50/p95 38.92/67.77ms.
- 5 tabs 233.39/1184.70ms.
- 20 tabs 857.63/5169.80ms.

אחרי index + single-flight:

- 1 tab, 5 requests: p50 40.31ms, p95 66.92ms.
- 5 tabs, 25 requests: p50 104.26ms, p95 148.33ms.
- 20 tabs, 100 requests: p50 414.08ms, p95 956.62ms, max 1458ms.
- HTTP success: 130/130.
- RSS בסבב 20 tabs: 53,633,024 → 73,506,816 bytes, delta 19,873,792.
- לא נצפו `database is locked/busy` או HTTP errors.
- DB size: 1,418,924,032 לפני; 1,446,281,216 לאחר indexes, גידול 27,357,184 bytes.
- CPU current מוצג דרך endpoint; מדידת trading-loop/WS לפני־אחרי אינה אפשרית כשה־trader כבוי.

## 22. Quality snapshot ב־production

הספירה הבאה מתייחסת ל־13 containers ראשיים בעלי quality מפורש, לא לכל label פנימי:

- REAL: 5 — P&L range, P&L cumulative, activity, alerts, health.
- STALE: 0 quality containers; reconciliation מוצג טקסטואלית `STALE` בנפרד.
- UNAVAILABLE: 8 — cash, reserved, positions value, claimable, total equity, positions list, orders list, markets.
- PARTIAL: 0.
- ESTIMATED: 0.

אין נתון post-cutover כי trader נשאר כבוי. ערכי legacy אינם מוצגים.

## 23. גיבויים ו־rollback

- config backup: `/opt/polymarket-btc-live/deployment-backups/live-dashboard-complete-20260812-preflight`.
- DB backup: `/opt/polymarket-btc-live/backups/live-dashboard-complete-20260812/poly_live.pre-dashboard.sqlite3`.
- checksum: `0fb36dd91366dab50b91b8bab7d67c9c76cb67d1dd401fe27e05a324cb4864cc`.
- static backups: `/opt/polymarket-btc-live/backups/live-dashboard-complete-20260812/live-status.pre-dashboard` ו־`/var/www/live-status.rollback-20260812`.

Rollback summary: stop dashboard; restore nginx/unit/env from protected backup; atomically restore prior static directory; אם נדרש schema rollback מלא, restore DB backup רק כשה־trader וה־dashboard כבויים. אין לנסות DROP columns ידני. לאחר restore יש `daemon-reload`, `nginx -t`, reload nginx והפעלת dashboard בלבד. הוראות אלו תיעודיות; לא בוצע rollback.

## 24. מצב מערכת בסיום

- Dashboard service: active, enabled, read-only.
- Trader service: inactive, disabled, לא הופעל מחדש.
- `LIVE_TRADING_ENABLED=false`.
- `LIVE_ORDER_SUBMISSION_ENABLED=false`.
- `LIVE_KILL_SWITCH=true`.
- `LIVE_PAUSE_ENTRIES=true`.
- Market WS/User WS: STOPPED.
- Dashboard health: RUNNING.
- אין open command endpoint בדאשבורד.

## 25. פערים וחסמים שנותרו

1. אין Chromium, Firefox, Playwright או Puppeteer מותקנים; לפי איסור התקנת חבילות לא בוצעו console inspection, screenshot, viewport mobile/desktop או click-through בדפדפן אמיתי.
2. אין snapshot/reconciliation post-cutover; הכספים והפוזיציות מוצגים UNAVAILABLE עד להפעלה עתידית מאושרת של המנוע וביצוע reconciliation read-only נקי.
3. אין current/next market post-cutover כאשר discovery/WS כבויים.
4. לא נמדדו trading-loop ו־WebSocket latency לאחר הפריסה כי trader כבוי.
5. fees היסטוריות הן UNKNOWN; אינן מוצגות כאפס ואינן נכללות במדדים רשמיים.

הפעולה המינימלית להשלמת חסם 1: לספק דפדפן headless שכבר מאושר/מותקן או לבצע ידנית login, desktop/mobile, filters, pagination ו־console inspection. אין צורך להפעיל trader לצורך זה.

## 26. תנאי הצלחה — מסקנה

מוכן: API ייעודי, auth, static protection, provenance עתידי, cutover, isolation, financial formulas, filters/charts/history, infrastructure, disabled controls, backups, migrations, tests, performance, production HTTP verification ו־read-only Polymarket comparison.

דורש נתוני runtime עתידיים: cash/equity/positions/orders/current market post-cutover. המערכת מציגה אותם כ־UNAVAILABLE עד שתתקבל הוכחה חדשה.

חסר לחלוטין בסביבה: browser automation binary. לכן אין להגדיר את המשימה “מלאה” למרות שהיישום והפריסה עצמם הושלמו.

## 27. אישורי בטיחות

- לא נשלחה שום הוראת מסחר.
- לא בוטלה שום הוראה.
- לא נסגרה ולא נפדתה פוזיציה לצורך הבדיקה.
- לא נחשף secret, private key, token, session או כתובת מלאה.
- כפתורי שליטה לא חוברו.
- מערכת המסחר לא הופעלה מחדש ונשארה כבויה/חסומה.
