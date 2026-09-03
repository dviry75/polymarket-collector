# אבחון שילוב Dashboard חדש ב־Polymarket LIVE

תאריך הבדיקה: 6 באוגוסט 2026 (UTC)  
סוג העבודה: מחקר ואבחון בלבד  
שרת שנבדק: `polymarket-live-fi`  
ריפו Dashboard: [dviry75/polymarket-dashboard](https://github.com/dviry75/polymarket-dashboard)  
ריפו LIVE: [dviry75/polymarket-collector](https://github.com/dviry75/polymarket-collector)

> לא בוצעו שינויי קוד, migrations, שינויי nginx/systemd/env, restart, deployment, פקודות מסחר, commit או push. הקובץ הזה הוא התוצר היחיד שנוצר.

## 1. תקציר מנהלים

אפשר לשלב את ה־Dashboard החדש, אך לא נכון לפרוס אותו עדיין. הריפו שהיה ריק בבדיקה המוקדמת כבר מכיל אב־טיפוס Next.js מלא, אבל כל המידע בו עדיין מגיע מ־`app/mock-data.ts`, כל פעולות השליטה הן שינויי state מקומיים בדפדפן, ואין בו API, WebSocket או Authentication.

במערכת LIVE כבר קיימים רוב מקורות האמת הבסיסיים: FastAPI, אימות session, CSRF, health פרטי, מצב Market/User WebSocket, SQLite עם orders/fills/positions/deals/audit/alerts/account snapshots, וחיבור CLOB לצורך reconciliation. עם זאת, ה־API הקיים אינו API מתאים לדאשבורד: חסרים endpoints מאוחדים ומסוננים להיסטוריה, equity/PnL, פוזיציות ה־strategy, מדדי מכונה וגרפים; חלק מ־endpoints הרשימה מצביעים לטבלאות legacy ריקות ולא לטבלאות ה־strategy הפעילות.

המלצה מרכזית: להוסיף את המסך בנתיב `/operations`, כ־Next.js static export בתוך אותו ריפו ואותו origin, ולהגיש את מסמך ה־HTML דרך FastAPI לאחר `require_live_session`. כך nginx הקיים יכול להמשיך להעביר את כל הנתיבים ל־`127.0.0.1:8001`, ואין צורך בשירות Node נוסף או בשינוי nginx. יש לשמור את קוד המקור בתיקייה עצמאית, למשל `operations-dashboard/`, ואת build הסטטי כ־artifact אטומי.

המלצה לנתונים: REST מדורג בלבד בשלב הראשון—summary cache כל 1–2 שניות, wallet/reconciliation כל 10–15 שניות, תשתית כל 15–30 שניות, וגרפים רק בטעינה/שינוי פילטר. אין כיום WebSocket מה־Backend לדפדפן; ה־WebSockets הקיימים הם חיבורי Backend יוצאים ל־Polymarket. אין הצדקה להוסיף WebSocket לדפדפן לפני שמדידת polling מוכיחה צורך.

חסמי Go/No-Go עיקריים:

1. working tree של LIVE מלוכלך; השירות מריץ אותו ישירות ולכן אין release commit חד־משמעי.
2. `deployment-state/current-commit` הוא `775fa6d`, HEAD הוא `0b3bfaa`, וב־working tree יש 11 קבצים משתנים עם 1,117 הוספות ו־153 מחיקות.
3. חסרות הגדרות עסקיות מאושרות ל־“עסקה”, equity, daily PnL וגרף הכסף.
4. `LIVE_OPERATOR_TOKEN` לא הופיע בין מפתחות env הפעילים; לפיכך פעולות write קיימות אמורות להיחסם—מצב בטוח למחקר, אבל לא תשתית UI סופית.
5. אין RBAC; יש admin יחיד, session cookie ו־operator token ידני.
6. חסרים indexes ושאילתות טווח עבור הגרפים; אסור להריץ סריקות על SQLite הפעיל.
7. סביבת TST מופיעה במסמכים אך לא אומתה בפועל בבדיקה זו.

## 2. סטטוס ריפו הדאשבורד

### ממצאים מאומתים

| פריט | מצב |
|---|---|
| קיום הריפו | קיים, public |
| default branch | `main` |
| branches | `main` בלבד, לא protected |
| commit אחרון | `d2e2b0a89b6dce370cf74d692b9470208b797323` |
| תאריך commit | `2026-08-06T20:11:49Z` |
| הודעה | `Add Polymarket dashboard frontend` |
| GitHub metadata size | עדיין מדווח `0`, אך tree מכיל קוד בפועל; זה metadata לא מעודכן ולא ריפו ריק |
| טכנולוגיה | Next.js `16.2.6`, React `19.2.6`, TypeScript `5.9.3`, Tailwind/PostCSS 4 |
| routing | Next App Router; קיים רק `app/page.tsx`, כלומר route `/` בלבד |
| UI/גרפים | CSS מותאם ו־SVG ידני; אין ספריית components או charting חיצונית |
| build | `npm run build` → `next build` |
| preview/production | אין script בשם `preview`; `npm run start` → `next start` |
| lint | `npm run lint` → `eslint .` |
| Backend/API/WS/Auth | לא קיימים |

### מבנה הקבצים

```text
.gitignore
README.md
app/
  globals.css
  layout.tsx
  mock-data.ts
  page.tsx
eslint.config.mjs
next-env.d.ts
next.config.ts
package-lock.json
package.json
postcss.config.mjs
public/favicon.svg
tsconfig.json
```

### mocks, כתובות קשיחות וסיכוני production

- `app/page.tsx` מייבא את כל המידע מתוך `app/mock-data.ts`.
- wallet, PnL, אירועים, פוזיציה, charts, stats, logs, alerts ו־health הם ערכים קשיחים.
- זמן השרת הוא למעשה שעון הדפדפן (`new Date()`), לא זמן השרת.
- countdowns מקומיים ואינם מסתנכרנים מחדש אחרי tab sleep, reconnect או rollover.
- פילטרי התאריך ו־mode של הגרף משנים תצוגה בלבד; אין query לנתונים.
- הכפתורים משנים React state מקומי בלבד ומציגים toast “הפעולה עודכנה בהדמיה”.
- קיימים טקסטים קשיחים כמו “לילה טוב, דביר”, תאריך seed, סכום $5 וחוק “Robot Polymarket”.
- אין secrets, private keys, wallet addresses או כתובות API קשיחות בקוד שנבדק.
- `next.config.ts` ריק: אין `basePath`, `assetPrefix` או `output: "export"`.
- `metadata.icons` משתמש ב־`/favicon.svg`; תחת base path הוא עלול לפנות לשורש הדומיין.
- `next/font/google` עלול לדרוש גישה לרשת בזמן build. יש לקבע font מקומי או לאמת build מבודד.
- `main` אינו protected. לפני production יש להוסיף branch protection ו־CI required checks.

מסקנה: הקוד ניתן להתאמה ל־static export, אך לא לשילוב ישיר ללא שינוי routing, data layer, error/loading states ואבטחה.

## 3. מצב מערכת ה־LIVE

### שרת ותהליך פעיל בזמן הבדיקה

| פריט | ממצא |
|---|---|
| hostname | `polymarket-live-fi` |
| timezone מערכת | UTC |
| נתיב עליון | `/opt/polymarket-btc-live` |
| ריפו | `/opt/polymarket-btc-live/repo` |
| קוד אפליקציה | `/opt/polymarket-btc-live/repo/polymarket-collector` |
| DB | `/opt/polymarket-btc-live/poly_live.sqlite3` |
| DB בזמן הדגימה | כ־562 MB, משתנה בזמן אמת |
| דיסק | 38 GB, כ־21% בשימוש, כ־31 GB פנויים |
| service | `polymarket-live.service`, active/running |
| process | `uvicorn live_app:app --host 127.0.0.1 --port 8001` |
| nginx | active/running; ports 80/443 |
| חשיפה ישירה של 8001 | לא; מאזין רק ל־localhost |
| timers | backup יומי ו־archive יומי |
| Kill Switch בזמן הדגימה | `true` |
| Pause Entries בזמן הדגימה | `true` |
| Market WS בזמן הדגימה | `CONNECTED`, אך נצפו reconnects ו־dynamic subscribe timeouts ביומן |
| User WS בזמן הדגימה | `CONNECTED` |

### זהות גרסה—חסם

- branch פעיל: `codex/live-full-implementation-20260805`.
- HEAD: `0b3bfaaf99776778fa35c11ad430d4a5ed95930b`.
- `origin/main`: `775fa6d95a1b4933b621290a3b0743baf6685865`.
- `deployment-state/current-commit`: `775fa6d95a1b4933b621290a3b0743baf6685865`.
- השירות מריץ קבצים ישירות מתוך working tree מלוכלך.
- קיימים גם קבצים untracked. לא שונו ולא נוקו במסגרת הבדיקה.

לכן לא ניתן לטעון שהשירות הפעיל מייצג commit יחיד. לפני כל עבודת Dashboard יש ליצור snapshot/branch מסודר ולזהות במפורש את baseline, בלי למחוק את השינויים הקיימים.

### מגבלת SSH

ה־alias שנמסר נפתר, אך ניסיון SSH מפורש נכשל ב־`Permission denied (publickey)`. הבדיקות בוצעו מתוך סביבת העבודה עצמה, שה־hostname שלה הוא `polymarket-live-fi`, ולכן runtime מקומי כן אומת. לא שונה `authorized_keys` ולא נעשה ניסיון לעקוף הרשאות.

## 4. ארכיטקטורה קיימת

```text
Internet
  -> nginx :443 (TLS, HSTS, security headers)
     -> catch-all proxy_pass 127.0.0.1:8001
        -> systemd: polymarket-live.service
           -> uvicorn / FastAPI: live_app:app
              -> /health (public, redacted)
              -> /live/login
              -> /live/* (session protected)
              -> in-process strategy/runtime/cache/order books
              -> SQLite WAL: /opt/polymarket-btc-live/poly_live.sqlite3
              -> Polymarket Market WS (outgoing)
              -> Polymarket User WS (outgoing, authenticated)
              -> CLOB/Data API through polymarket-client/httpx
              -> Google Secret Manager / env secret provider
```

### Backend ו־Frontend

- Backend: Python 3.12, FastAPI `0.141.1`, Uvicorn `0.52.1`.
- Frontend הפעיל: HTML/CSS/JavaScript שנוצר inline בתוך `live/router.py`; אין template engine ואין static frontend build.
- `live_app.py` הוא entrypoint העצמאי של LIVE. `app.py` הוא מערכת DEMO/legacy ואינו ה־entrypoint של השירות הפעיל.
- root `/` מפנה 307 ל־`/live`.
- nginx הוא catch-all; direct refresh בכל route יעבור ל־FastAPI, אך FastAPI חייב להכיר את route.

### Authentication ו־Authorization

- login יחיד ב־`/live/login`.
- session cookie בשם `live_session`, עם `HttpOnly`, `Secure`, `SameSite=Strict`.
- session חתום HMAC וכולל username, issued time ו־session version.
- כל GET תחת `/live` (פרט login) דורש session.
- פעולות write דורשות גם CSRF וגם `X-Live-Operator-Token`.
- פעולות קריטיות מסוימות דורשות password re-authentication.
- אין roles/RBAC; קיים admin יחיד.
- ברירת המחדל הנוכחית של session היא persistent until logout (`TTL <= 0`). למסך תפעולי מומלץ TTL מוגבל.
- ה־UI הקיים מבקש operator token ב־`window.prompt`; אין לשמר UX זה בדאשבורד החדש, ואין לשמור token ב־localStorage/sessionStorage.
- בזמן הבדיקה `LIVE_OPERATOR_TOKEN` לא הופיע בקובץ ה־env הפעיל, ולכן write אמור להיחסם fail-closed.

### nginx ו־systemd

- nginx מפנה HTTP ל־HTTPS, מוסיף HSTS, `nosniff`, `DENY`, `no-referrer` ו־Permissions-Policy.
- nginx מעביר Upgrade/Connection ומבטל buffering, אך אין כיום WebSocket endpoint לדפדפן.
- systemd כולל hardening משמעותי: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, ללא capabilities, ו־`ReadWritePaths=/opt/polymarket-btc-live`.
- service Restart הוא `on-failure`, לא restart יזום מה־UI.
- אין שירות DEMO על שרת זה לפי runbook הפעיל.

### SQLite, state ו־cache

- SQLite עובד ב־WAL; connections עם `busy_timeout=30000` ו־`synchronous=NORMAL`.
- בזמן הבדיקה: 316 markets, 2,530 account snapshots, 58,108 websocket events, 129,292 market snapshots, 8,577 timeline rows, 54 alerts, ו־strategy deal/fill/position אחד.
- טבלאות legacy `live_rules`, `live_deals`, `live_orders`, `live_order_fills`, `live_positions` היו ריקות; נתוני REAL strategy נמצאים ב־`live_strategy_*`.
- state בזמן אמת קיים גם בזיכרון: order books, WS health/latency records, pending snapshots, runtime readiness ו־heartbeat.
- state מתומצת נכתב גם ל־`live_system_state`.
- reconciliation מול CLOB הוא מקור האמת הפיננסי ומרענן balance, allowance, orders, trades ו־positions.

## 5. רשימת רכיבי המסך בפועל

הרכיבים שאומתו ב־`app/page.tsx`:

1. מצב מכונה, חיבור Polymarket, freshness וזמן “שרת”.
2. כפתור הפעלה/השבתה.
3. ברכת משתמש ומצב מערכת כללי.
4. banner התראה וסגירתו המקומית.
5. יתרת ארנק: total, available, committed, profit יומי ואחוז.
6. אירוע נוכחי: שם, חלון זמן, YES/NO, countdown, strategy/lock.
7. עסקה נוכחית: side, PnL, entry, tokens, invested, current value ופקודה פתוחה.
8. toggle מקומי להצגה/הסתרה של העסקה.
9. האירוע הבא, countdown, eligibility וחוק פעיל.
10. גרף ביצועי ארנק בשלושה modes: balance, cumulative PnL, PnL per event.
11. פילטרים: היום, אתמול, 3/7/30 ימים וטווח מותאם.
12. פעולות: הפעלה/השבתה, pause/resume, cancel open orders, reconciliation, emergency stop.
13. גרף פעילות יומית: wins/losses/skipped.
14. סטטיסטיקות: trades, wins, losses, win rate, averages, net profit, fees.
15. live activity log עם פרטים טכניים.
16. system health: disk, RAM, CPU, uptime, WebSocket, DB, last data, Polymarket latency.
17. confirmation modal ו־toast.

ה־toggle “הצג עסקה” אינו רכיב עסקי. ב־production יש להסירו או להפוך אותו לפילטר UI בלבד; אסור שיסתיר state קריטי כברירת מחדל.

## 6. מיפוי מלא בין רכיבים לנתוני אמת

סימון מקור: **DB** = persisted; **MEM** = זיכרון תהליך; **PM** = Polymarket public; **CLOB** = חשבון מאומת; **CALC** = חישוב; **NEW** = דורש פיתוח.

| רכיב במסך | משמעות עסקית | מקור נתונים קיים | טבלה/שירות | Endpoint קיים | Endpoint חסר | תדירות מומלצת | REST/WS | הרשאה | טיפול בחסר/שגוי |
|---|---|---|---|---|---|---|---|---|---|
| מצב מכונה | האם process חי והאם מותרות כניסות | DB+MEM+systemd | `live_system_state`, runtime health, systemd | `/health`, `/live/health`, `/live/strategy/status` | summary מאוחד; systemd read-only | 1–2s | REST polling | session | להציג בנפרד Process/Mode/Pause/Kill; לא “פעילה” יחיד |
| חיבור Polymarket | Market/User WS בנפרד | MEM+DB | WS managers, state | `/live/health` | לא | 1–2s | REST; WS עתידי | session | degraded אם אחד stale; לא להציג “מחובר” כוללני |
| freshness | גיל frame אחרון | MEM+DB | market WS health, snapshots | `/live/health` | שדה normalized `age_ms/stale_after_ms` | 1s | REST | session | badge stale + timestamp; לא להציג 120ms קשיח |
| זמן שרת | זמן authoritative UTC + תצוגת ישראל | Backend | `now_iso()` | `/live/health`, `/live/strategy/status` | time endpoint לא חובה | sync כל 30s, tick מקומי | REST | session | להציג “לא מסונכרן” אם drift גדול |
| התראות | alerts פעילות persistent | DB | `live_alerts` | `/live/alerts`, `/live/health` | count/last update ב־summary | 2–5s | REST | session | dismiss מקומי אסור; acknowledge הוא פעולה נפרדת |
| יתרת ארנק total | equity: collateral + שווי positions | CLOB+PM+CALC | account snapshot + remote positions | snapshot בתוך `/live/health` | `/live/dashboard/wallet` | 10–15s | REST | session | להציג stale/source; לא לאפס במקרה timeout |
| זמין למסחר | collateral זמין בכפוף allowance/risk/reservations | CLOB+CALC | `balance_usd`, `allowance_usd`, intents | חלקי ב־health | wallet aggregator | 10–15s | REST | session | `unknown` אם CLOB stale; לא להסיק מ־equity |
| committed | capital בפוזיציות/פקודות | DB+CLOB+CALC | strategy positions/intents | positions בתוך strategy status | שדה aggregate | 2–5s | REST | session | להציג breakdown position/order |
| PnL יומי/% | realized לפי יום + denominator מאושר | DB+CALC | strategy positions/deals/fills | `daily_pnl_text` ב־strategy status | endpoint timezone-aware | 10s | REST | session | `N/A` אם baseline חסר; לציין fees included |
| אירוע נוכחי | BTC 5m הפעיל | DB+MEM+PM | `live_markets`, event state, order books | `/live/markets`, strategy status | current-event view model | 1s | REST | session | state UNKNOWN בזמן rollover; last-good עם stale |
| YES/NO | best bid/ask של שני tokens | MEM+DB | order books, `live_markets` | `/live/markets` | current quotes compact | 1s | REST | session | להציג bid/ask/source/age; לא מחיר יחיד עמום |
| countdown | זמן עד event end | PM metadata+CALC | market raw metadata/event id | לא מותאם | event `start/end/server_time` | 1s מקומי, sync 30s | REST | session | clamp 0; refresh authoritative ב־rollover |
| strategy/lock | readiness, locked side/reason | DB+MEM | `live_event_states`, runtime | strategy status | compact event strategy | 1–2s | REST | session | reason code גלוי; מצב unknown fail-closed |
| פוזיציה נוכחית | shares, side, cost, value, PnL | DB+CLOB+MEM+CALC | `live_strategy_positions`, order book | strategy status מחזיר positions | position DTO מצומצם | 1–2s | REST | session | local/remote mismatch = critical, לא להסתיר |
| פקודה פתוחה | intent/order state | DB+CLOB | `live_strategy_intents`, reconciliation | אין list ייעודי; logs בלבד | `/live/dashboard/orders` | 1–2s | REST | session | להציג pending/unknown/reconciling במפורש |
| אירוע הבא | event הבא ו־eligibility | DB+PM+CALC | `live_markets`, discovery | `/live/markets` | `next_event` ב־summary | 5s | REST | session | “לא ידוע” אם discovery stale |
| חוק פעיל/גודל עסקה | config strategy בפועל | env safe config+runtime | `LiveConfig`, runtime | `/live/health` safe config | normalized strategy config | 15–30s | REST | session | להציג source=config; אין `live_rules` פעיל כרגע |
| גרף balance | account snapshots לאורך זמן | DB | `live_account_snapshots` | אין history | `/live/dashboard/equity-series` | בטעינה/פילטר | REST | session | downsample; gap markers; לא interpolate outage |
| גרף cumulative/event PnL | realized PnL לפי deal/event | DB+CALC | `live_strategy_deals/positions/fills` | אין | `/live/dashboard/pnl-series` | בטעינה/פילטר | REST | session | separarate open/unrealized; fees included |
| פילטרי תאריך | half-open interval לפי TZ | CALC+DB | timestamps UTC | logs תומך from/to בלבד | params אחידים לכל analytics | לפי שינוי | REST | session | validate max range; 422 בטווח שגוי |
| פעילות wins/losses/skipped | closed deals + eligible events ללא entry | DB+CALC | deals, event states/timeline | אין | daily activity endpoint | בטעינה, 30s | REST | session | skipped דורש הגדרה; לא להסיק מכל market row |
| stats | counts/win rate/avg/net/fees | DB+CALC | deals/fills | counts חלקי ב־health | stats endpoint | 10–30s | REST | session | denominator גלוי; `N/A` כשאין deals |
| live log | timeline מסונן ומדופדף | DB | `live_audit_timeline` | `/live/logs` | התאמת DTO בלבד | 2–5s | REST | session | pagination, redaction, retry; לא tail בלתי מוגבל |
| disk | filesystem + DB size | OS+DB | `statvfs`, strategy status | DB size בלבד | infra health | 15–30s | REST | session/admin-read | cache; threshold warning/critical |
| RAM/CPU/uptime | health process/host | OS | systemd/proc | אין | infra health | 15–30s | REST | admin-read | ללא shell per request; `unknown` בלי הרשאה |
| DB health | read latency, WAL, growth, backup | DB | SQLite, archive metrics | strategy status חלקי | normalized DB health | 15–30s | REST | session | timeout קצר; אין integrity_check על כל poll |
| Polymarket latency | transport/processing percentiles | MEM | WS latency deque/diagnostics | health חלקי | compact percentiles | 2–5s | REST | session | p50/p95/source window; stale אם אין samples |
| service status | ActiveState/uptime/restarts | systemd | service manager | אין | read-only service health | 15–30s | REST | admin-read | whitelist service; אין sudo/commands מה־UI |
| redemption | resolved/redeem pending/redeemed | DB+CLOB | strategy position/deal/intent | strategy status חלקי | redemption list/read model | 5–15s | REST | session | never auto-redeem מה־Dashboard בשלב ראשון |

## 7. API קיים מול API חסר

### endpoints קיימים ורלוונטיים

| Endpoint | שימוש אפשרי | מגבלה |
|---|---|---|
| `GET /health` | load balancer/smoke | redacted בלבד |
| `GET /live/health` | summary רחב | payload גדול ומעורב; לא contract ייעודי |
| `GET /live/strategy/status` | readiness, positions, DB, account | כולל state פנימי; אין DTO/versioning |
| `GET /live/markets` | 100 markets אחרונים | לא current/next contract; אין range/filter |
| `GET /live/orders`, `/fills`, `/deals` | legacy tables | בזמן הבדיקה ריקות; אינן strategy truth |
| `GET /live/rules` | legacy/paper rules | strategy REAL בפועל מגיע מ־config/runtime |
| `GET /live/reconciliation` | reconciliation history | limit קבוע, ללא date pagination |
| `GET /live/audit` | legacy audit | timeline החדש עשיר יותר |
| `GET /live/logs` | timeline filters + pagination | מתאים כבסיס; limit bounded |
| `GET /live/alerts` | alerts פעילות | מתאים |
| `GET /live/timeline/{type}/{id}` | drill-down | מתאים |
| `GET /live/maintenance/status` | drain readiness | session protected |
| `GET /live/secrets/readiness` | קיום secrets | admin-only semantics רצוי; לא להציג values |

### API חסר מומלץ

כל השמות להלן הם תכנון בלבד:

```text
GET /live/dashboard/summary
GET /live/dashboard/wallet
GET /live/dashboard/equity-series?from=&to=&timezone=&bucket=
GET /live/dashboard/pnl-series?from=&to=&timezone=&bucket=
GET /live/dashboard/activity?from=&to=&timezone=&bucket=day
GET /live/dashboard/orders?state=&cursor=&limit=
GET /live/dashboard/deals?state=&cursor=&limit=
GET /live/dashboard/redemptions?state=&cursor=&limit=
GET /live/dashboard/infra
GET /live/auth/session
```

עקרונות contract:

- version/schema מפורשים, UTC ISO-8601, מספרים כסטרינג Decimal כספי או minor units; לא float.
- בכל response: `server_time`, `data_time`, `age_ms`, `stale`, `source`, `partial` ו־`errors[]`.
- endpoints של chart מחזירים סדרה מצומצמת/downsampled בלבד.
- אין raw payload, addresses מלאות שאינן נחוצות, secrets, cookies, headers או exception text פנימי.
- summary נבנה מ־cache/read model; לא מבצע סריקה של טבלאות גדולות בכל poll.

אין כיום application WebSocket ל־browser. `POLYMARKET_MARKET_WS_URL` ו־`POLYMARKET_USER_WS_URL` הם חיבורי Backend יוצאים ולא endpoints שניתן לחבר אליהם UI.

## 8. הגדרות מדדים וחישובים

הסעיף הבא מפריד בין מצב קיים לבין ברירת מחדל מוצעת הדורשת אישור משתמש.

### “עסקה”

**ברירת מחדל מוצעת:** עסקה אחת היא `live_strategy_deals`/position לוגי אחד עבור event אחד, מה־entry הראשון ועד CLOSED/RESOLVED/REDEEMED. אין לספור order או fill כעסקה. יש להציג בנפרד `orders_count` ו־`fills_count`.

- partial fills של אותו intent מצטברים לפי fill deduplicated (`remote_trade_id`/`fill_id`).
- average fill = `SUM(shares * price) / SUM(shares)`.
- order שבוטל ללא fill אינו עסקה.
- order שבוטל אחרי partial entry כן יוצר עסקה אם נפתחה position.
- position שנמכרה בחלקים נשארת עסקה אחת עד שאין יתרה sellable/remaining או עד resolution.

### PnL

המימוש הקיים ביציאת position מחשב:

```text
allocated_cost = cost_all_in * sold_cumulative_shares / acquired_shares
realized_pnl = cumulative_exit_value - cumulative_exit_fees - allocated_cost
```

`cost_all_in` כולל entry fees; לכן realized PnL נטו כולל entry ו־exit fees. זה בסיס טוב ויש לשמר אותו. אין לחבר שוב fees ב־Dashboard.

- position פתוחה: realized PnL רק על shares שנמכרו; unrealized PnL על remaining לפי executable best bid, לא midpoint.
- unrealized: `remaining_shares * best_bid - allocated_remaining_cost`; להציג source/age ולא להכניס ל־realized.
- winner ב־resolution: value של remaining shares הוא 1 לכל share; loser הוא 0.
- redemption משנה state ל־REDEEMED וסוגר את העסקה; אין להוסיף payout פעם שנייה אם PnL כבר נרשם ב־resolution.
- fees חסרות/לא מאומתות: metric `partial=true`, ולא `0` שקט.
- ROI מוצע: `net_realized_pnl / allocated_cost * 100`; division by zero → `N/A`.

### זמן וימים

- כל timestamp persisted יישאר UTC.
- פילטר “יום” יוצג לפי `Asia/Jerusalem`, כולל DST.
- Backend יקבל local date/TZ, יחשב גבולות `[from_utc, to_utc)` ויבצע range query על timestamp מלא.
- אין להשתמש ב־`substr(timestamp,1,10)`; זה גם UTC semantics וגם מונע index יעיל.
- נמצא פער קיים: `StrategyRepository.daily_pnl()` משתמש ביום UTC, בעוד `live_daily_limits` משתמש `Asia/Jerusalem`. יש ליישר לפני הצגת “היום”.

### יתרת ארנק וגרף כסף

הצעה להפריד שלושה מספרים:

1. **Collateral available:** `balance_usd` מה־CLOB, עם `allowance_usd` ו־timestamp.
2. **Open positions value:** סכום `current_value` מהחשבון המאומת; fallback לפי remaining × executable bid רק עם label משוער.
3. **Total equity:** collateral available + open positions value. יש לוודא מול SDK אם collateral של open orders כבר reserved כדי למנוע double subtraction.

**גרף ברירת מחדל מוצע:** total equity snapshots. מצבי גרף נוספים: cumulative realized net PnL ו־net PnL per closed event. אין לקרוא לגרף realized PnL “יתרת ארנק”.

### wins/losses/skipped

- win: עסקה סגורה עם net realized PnL > 0.
- loss: net realized PnL < 0.
- breakeven: PnL = 0; קטגוריה נפרדת, לא loss.
- skipped: event שבו strategy הייתה eligible אך לא נוצר entry, עם reason code. “כל event ללא עסקה” אינו בהכרח skipped.
- win rate: `wins / (wins + losses)`; breakeven ו־skipped מחוץ למכנה. יש להציג את המכנה ב־tooltip.

## 9. המלצה על הנתיב החדש

### בחירה: `/operations`

נבדקו conceptually `/operations`, `/control-center`, `/live/dashboard`:

- אין route קיים בשם `/operations`; אין התנגשות.
- השם מתאר observability ותפעול, בלי להבטיח שזו מערכת המסחר עצמה.
- `/live/dashboard` נקשר למסך legacy הקיים ועלול לבלבל בין שני UIs.
- `/control-center` מתנגש מושגית בשם האפליקציה הקיימת.

דרישות route:

- `GET /operations` ו־`GET /operations/` חייבים לבצע `require_live_session`; ללא session → 303 ל־`/live/login?next=/operations`.
- direct refresh חייב להחזיר את אותו static `index.html` דרך FastAPI.
- Next build יקבל `basePath: "/operations"` או asset path מפורש שנבדק; favicon/assets לא יפנו בטעות לשורש.
- assets hashed יכולים להיות cacheable ופתוחים; HTML ו־API לא. אם asset חושף metadata רגיש—גם הוא יוגן.
- קישור “Operations” יתווסף לניווט `/live` רק לאחר שהroute עובר auth/smoke.
- nginx catch-all הקיים אינו דורש שינוי אם FastAPI מגיש route ו־assets.

## 10. שיטת אינטגרציה מומלצת

### מועדף: static export בתוך אותו FastAPI origin

```text
polymarket-collector/
  operations-dashboard/      # מקור Next.js
  operations-static/         # artifact בנוי, לא source of truth
  live/
    dashboard_api.py         # read-only DTO/query layer
    router.py                # auth route + mount/asset routes
```

יתרונות:

- אין Node runtime נוסף ב־LIVE.
- אין port/service/process נוסף.
- session cookie ו־same-origin fetch קיימים.
- nginx נשאר ללא שינוי.
- rollback הוא החלפת artifact + קוד FastAPI לאותו release.
- ה־UI לא נוגע ישירות ב־SQLite או ב־Polymarket.

תנאים:

- להתאים `next.config.ts` ל־static export/base path.
- להסיר כל תלות ב־server components/runtime APIs של Next.
- להחליף mock imports ב־typed API client; mocks יישארו רק fixtures/tests.
- להוסיף loading/error/empty/stale states לכל card.
- build נעשה ב־CI/TST, לא על לולאת המסחר ב־LIVE.

חלופה של שירות Next נפרד תחת אותו דומיין אינה מועדפת כעת: היא מוסיפה systemd unit, Node runtime, health/rollback נוסף ופתרון auth proxy. יש לשקול אותה רק אם בעתיד יידרשו SSR/Next server features אמיתיים.

## 11. תכנון אבטחה והרשאות

1. כל read endpoint של Dashboard דורש session קיים.
2. להוסיף capability/role מפורש: `viewer`, `operator`, `critical_operator`; המימוש הנוכחי אינו כולל RBAC.
3. viewer אינו רואה addresses מלאות, secrets readiness מפורט, raw payload או technical errors.
4. write: session + CSRF + server-side authorization. אין לשמור operator token בדפדפן.
5. פעולות קריטיות: re-auth קצר־תוקף, confirmation מדויק ו־audit log.
6. להגביל session TTL למסך תפעולי; לשמר revoke-all/session version.
7. CSP מותאם ל־static Next, ללא `unsafe-eval`; SRI אינו נדרש ל־self-hosted hashed assets אך אין CDN חיצוני.
8. API מחזיר error codes בטוחים ולא exception text/CLOB payload.
9. logs ו־exports עוברים `sanitize`, אך יש להוסיף allowlist DTO ולא להסתמך רק על redaction לפי שם שדה.
10. rate limit על login וגם על פעולות write; idempotency keys לכל פעולה בעלת side effects.

## 12. פעולות שליטה מסוכנות

בשלב הנוכחי כל הכפתורים בדאשבורד החדש חייבים להישאר disabled או להציג “לא מחובר”. אין לחבר אותם ל־endpoints הקיימים.

| פעולה ב־UI | משמעות מדויקת מוצעת | Backend | הרשאה/אישור | audit/idempotency | intermediate/verification/error | האם להציג |
|---|---|---|---|---|---|---|
| “השבת מכונה” | Pause entries בלבד; ממשיך לנהל positions/orders | קיים `/live/pause-entries/pause` | operator + CSRF; confirm | audit קיים; key מומלץ | `pausing` עד summary מאשר; 409 בטוח | כן, בשם “Pause entries” |
| “הפעל מכונה” | Resume entries אחרי readiness | קיים `/live/pause-entries/resume` | operator + CSRF + reauth | audit; idempotent אם כבר resumed | blockers מפורשים; verify `pause_entries=false` | כן, בשם “Resume entries” |
| Kill Switch ON | חסימת submission fail-closed | קיים `/live/kill-switch/activate` | critical operator; confirm | audit + idempotency | verify state true; timeout נשאר unknown/blocked | כן, בולט ונפרד |
| Kill Switch OFF | הסרת חסם רק לאחר readiness | קיים `/live/kill-switch/deactivate` | critical + reauth + typed confirmation | audit + idempotency | verify state false וכל gates READY | כן רק למורשים; לא במסך viewer |
| “בטל פקודות פתוחות” | ביטול orders מסוימים או scoped batch | אין endpoint בטוח כללי | critical; preview + typed confirm | חובה batch idempotency/audit per order | progress per order + reconciliation | לא עד פיתוח ואישור scope |
| “רענן התאמה” | reconciliation קריאה מרחוק שכותבת snapshot/state | קיים `/live/reconciliation/run` | operator + CSRF | audit/run_id; duplicate suppression | running→matched/gaps/failed | כן; עם cooldown |
| “עצירת חירום” | עמום: Kill Switch או emergency close | שני endpoints שונים; אסור למפות אוטומטית | critical | חובה | חייב לבחור פעולה מדויקת | לא בשם הנוכחי |
| Emergency close | preview ואז sell positions; לא global cancel | קיימים preview/execute | operator + reauth + exact phrase | timeline קיים; idempotency נוסף רצוי | per-position result + reconciliation | רק במסך ייעודי, לא dashboard ראשי |
| stop/start/restart service | שינוי process systemd | אין endpoint | מחוץ לאפליקציה | audit חיצוני | drain/readiness + operator runbook | לא להציג ב־UI |
| redemption | פעולה on-chain/SDK | אין UI endpoint מאושר | critical/ops נפרד | tx idempotency + tx hash | pending/confirmed/failed | לא בשלב Dashboard |

מניעת double click: disable מיידית, client request UUID, unique server idempotency record, 409/200 replay semantics, ורענון state authoritative. לעולם אין להסיק הצלחה מ־HTTP timeout.

## 13. תכנון עדכון בזמן אמת

| קבוצת נתונים | קצב | מנגנון |
|---|---:|---|
| clock/countdown | tick מקומי 1s; sync 30s | server time + event end |
| mode/pause/kill/readiness/WS/freshness/current position | 1–2s | summary cached REST |
| orders/activity alerts | 2–5s | REST עם cursor/ETag |
| wallet/reconciliation | 10–15s | snapshot קיים; לא CLOB call מכל browser poll |
| CPU/RAM/disk/service/backup | 15–30s | server-side cached sampler |
| charts/stats | בטעינה/שינוי פילטר; refresh 30–60s | indexed range REST |
| logs | 2–5s רק כשהpanel פתוח | cursor pagination |

`last updated` חייב להיות data timestamp ולא זמן response בלבד. כל widget יציג source ו־stale threshold. במקרה PM/CLOB/DB outage: לשמור last-known value בצבע stale, להציג age/error code, להשבית control actions התלויות בנתון, ולא להחליף ערך ב־0.

## 14. השפעה על ביצועי המסחר

העדיפות העליונה היא בידוד read load מלולאת המסחר.

ממצאים:

- DB פעיל כ־562 MB ונכתב בתדירות גבוהה.
- נמצאה נעילת DB זמנית אפילו בבדיקת schema readonly בזמן הכתיבה.
- SQLite WAL מאפשר readers, אך queries ארוכות עדיין מאריכות WAL ומפעילות CPU/I/O.
- generic `list_table` אינו מתאים ל־analytics.
- `daily_pnl()` הקיים משתמש `substr` ללא index.

דרישות:

1. summary cache בזיכרון שמתעדכן פעם אחת ומשותף לכל clients.
2. endpoints מהירים עם timeout קצר ו־bounded rows.
3. charts על read model/rollup, לא על `live_market_snapshots` גולמי.
4. indexes מוצעים לפני production, לאחר EXPLAIN ובדיקת TST:
   - `live_account_snapshots(sampled_at)`
   - `live_strategy_deals(closed_at, state)`
   - `live_strategy_fills(matched_at, intent_id)`
   - `live_strategy_intents(updated_at, state)`
   - `live_strategy_positions(updated_at, state)`
   - `live_alerts(active, last_seen_at)`
   - לפי צורך `live_market_snapshots(received_at)`; לא אם אין query עסקי ישיר.
5. date filters כ־range על raw timestamp, לא function על column.
6. downsample/rollup יומי ל־7/30 ימים; max points למשל 500.
7. per-user rate limit, ETag/304 ו־AbortController בצד client.
8. לא לבצע CLOB/Polymarket request מכל page refresh; להשתמש ב־reconciliation/samplers קיימים.
9. benchmark כאשר trading loop פעיל: p95 order-book processing, SQLite commit latency, reconnect rate ו־event-loop lag לפני/אחרי.

## 15. תכנית פריסה ל־TST ול־LIVE

אין לבצע את הפקודות להלן בשלב המחקר; הן דוגמאות לתכנית עתידית בלבד.

### שלב A—הכנת baseline

1. לתעד ולשמר את ה־working tree הקיים בלי למחוק שינויים.
2. לבחור commit baseline מאושר ולהקים branch ייעודי.
3. להגן על `main`, להפעיל CI ו־secret scanning.
4. לייבא את dashboard source כתיקייה בתוך ריפו LIVE, עם attribution ל־commit `d2e2b0a`.

```bash
# דוגמה בלבד
git switch -c feature/operations-dashboard <approved-baseline-hash>
npm ci
npm run lint
npm run build
```

### שלב B—data contract ו־read-only API

1. לאשר definitions בסעיף 8.
2. לכתוב DTOs ו־queries bounded.
3. להוסיף indexes/migrations רק אחרי EXPLAIN ו־backup ב־TST.
4. להוסיף cache ו־stale semantics.
5. לא להוסיף write controls.

### שלב C—Frontend

1. static export/base path `/operations`.
2. החלפת mocks ב־typed client.
3. empty/loading/error/stale/partial states.
4. direct refresh, RTL, mobile, accessibility.
5. להשאיר controls disabled ומסומנים “שלב עתידי”.

### שלב D—TST

1. לאמת קודם מהו host/service/DB האמיתי של TST; המסמך הישן מזכיר `polymarket-btc-tst` אך זה לא אומת.
2. להשתמש בעותק sanitized/fixture או מקור אמת read-only.
3. להריץ build, tests, migration dry-run, query plans ועומס.
4. לבדוק session/CSRF/RBAC ונתוני timezone.
5. לבצע canary UI ללא write endpoints.

### שלב E—LIVE

1. Maintenance drain ורק לאחר `stop_ready=true`.
2. backup עקבי + readback/integrity על עותק.
3. artifact חתום/מזוהה ב־commit.
4. release directory חדש והחלפה אטומית.
5. restart אחד מתוכנן רק אם קוד Backend השתנה.
6. smoke: `/health`, login, `/operations`, refresh ישיר, summary, charts, DB/WS freshness.
7. הפעלה הדרגתית: viewer יחיד → משתמשים מורשים → controls בשלב release נפרד.

nginx לא אמור להשתנות בשיטה המועדפת. אם יתברר שנדרש שינוי, הוא חייב לעבור `nginx -t`, backup ו־rollback נפרד.

## 16. תכנית בדיקות

### Build ו־routing

- `npm ci`, lint, typecheck, production build ו־static export reproducible.
- אין network fetch בלתי צפוי בזמן build.
- `/operations`, trailing slash ו־direct refresh.
- hashed assets, favicon, fonts ו־base path.
- navigation מ־`/live` וחזרה.
- 404 אמיתי אינו מחזיר בטעות dashboard.

### Auth והרשאות

- unauthenticated → login עם safe next path.
- session expired/revoked/rotated.
- CSRF חסר/שגוי.
- viewer אינו רואה/מפעיל controls.
- operator ללא reauth נחסם בפעולה קריטית.
- אין token ב־URL, logs, localStorage או error reporting.

### נתונים פיננסיים

- balance/allowance/positions מול CLOB snapshot ידוע.
- fill יחיד, כמה partial fills, duplicate fill, fill מאוחר אחרי cancel.
- position sold in parts, dust, TP, stop, emergency exit.
- fees entry/exit, fee חסר, rounding Decimal.
- resolution winner/loser ו־redeem; אין double count.
- event פתוח, event ללא trade, DB ריק.
- canceled/unmatched/zero-fill אינם נספרים כעסקה.
- realized/unrealized/equity מול fixture ידני.

### זמן ופילטרים

- UTC ↔ `Asia/Jerusalem`, כולל מעבר DST.
- היום/אתמול/3/7/30 ימים וטווח מותאם.
- boundary בדיוק בחצות וב־`to` הבלעדי.
- tab sleep/countdown resync ו־event rollover.

### אמינות

- Market WS disconnected/stale/reconnecting.
- User WS auth failed/stale.
- Polymarket/CLOB timeout ו־HTTP errors.
- DB busy/timeout/readonly/corrupt fixture.
- Backend 401/403/409/422/500 ו־partial response.
- last-known data נשמר ומסומן stale.
- alert acknowledge failure אינו מעלים alert.

### UI

- mobile, tablet, desktop, RTL, keyboard, focus trap/modal, screen reader labels.
- charts עם 0/1/500 points, negative values וגודל מספר גדול.
- slow network, duplicate clicks ו־request cancellation.

### Performance

- baseline מול dashboard polling: event-loop lag, DB busy rate, CPU, memory, WAL size.
- 1/5/20 clients; p95/p99 API latency.
- query plan ללא full scan לטווחים רגילים.
- chart response bounded; log pagination bounded.
- order/market WS processing אינו מדרדר מעבר לסף מאושר.

### Rollback

- static artifact rollback.
- Backend release rollback.
- migration forward/backward compatibility.
- health/login/market WS/reconciliation אחרי rollback.

## 17. תכנית rollback

1. לפני release: לשמור current/previous commit, artifact hash, config checksum ללא values, systemd/nginx backup ו־SQLite backup עקבי.
2. UI בלבד: להחזיר symlink/artifact הקודם; ה־API נשאר backward compatible.
3. Backend: maintenance drain, kill/pause fail-closed, להחזיר checkout ל־hash מאושר ולהפעיל service לפי runbook.
4. DB: migrations חייבות להיות additive/backward compatible לפחות release אחד. אין להחליף DB פעיל בעותק restore במסגרת rollback רגיל.
5. אם אין previous release תקין: להסיר קישור/route Dashboard או להשאירו disabled; trading service נשאר במצב הבטוח.
6. אחרי rollback: `/health`, login, readiness, reconciliation, WS, open positions/orders ו־DB growth.

## 18. חסמים וסיכונים

| חומרה | חסם/סיכון | השפעה | פעולה נדרשת |
|---|---|---|---|
| קריטי | release identity לא חד־משמעי + working tree מלוכלך | rollback ו־reproducibility לא אמינים | לשמר שינויים ולייצר baseline מאושר |
| קריטי | definitions פיננסיות לא מאושרות | מספרים מטעים | החלטות סעיף 19 + fixtures |
| גבוה | legacy endpoints ריקים מול strategy tables פעילות | dashboard יציג 0 שגוי | read-only DTOs ל־strategy truth |
| גבוה | analytics ללא indexes | הפרעה למסחר/DB busy | query plans, indexes/rollups ב־TST |
| גבוה | אין RBAC; operator token UX ידני | הרשאות חלשות/UX מסוכן | role/capability design, no browser-stored token |
| גבוה | כפתורי prototype עמומים | פעולה שגויה | rename, disable, release נפרד |
| בינוני | session persistent | סיכון workstation | TTL + reauth policy |
| בינוני | WebSocket reconnect timeouts נצפו | stale UI/strategy risk | להציג שני WS ומדדי freshness, לא להסתיר |
| בינוני | TST לא אומת | תכנית פריסה לא נבדקה | inventory read-only נפרד |
| בינוני | metadata GitHub size=0 למרות code | tooling עלול לזהות ריפו כריק | להסתמך על git tree/commit, לא size |
| נמוך | Next font/favicon/basePath | build/assets שבורים | local font + static export tests |

## 19. שאלות שמחייבות החלטת משתמש

1. האם “עסקה” מאושרת כ־deal/position אחד לכל event, ולא order/fill?
2. האם ברירת מחדל של גרף הכסף היא total equity, או balance בלבד?
3. האם daily metrics יוצגו לפי `Asia/Jerusalem` לכל המשתמשים?
4. מהו starting equity denominator לחישוב אחוז רווח יומי?
5. האם “skipped” הוא רק event שהיה eligible עם reason code, או כל event ללא entry?
6. האם פעולות שליטה צריכות להופיע בכלל ב־Dashboard הראשי, או במסך operator נפרד?
7. מה ההבדל העסקי הרצוי בין Pause Entries, Kill Switch ו־“השבת מכונה”?
8. האם viewer role נדרש, ומי רשאי לראות wallet amounts/addresses/log details?
9. האם להציג unrealized לפי best bid שמרני או midpoint? ברירת המחדל המוצעת היא best bid.
10. האם redemptions נשארות manual מחוץ ל־UI? זו ברירת המחדל המומלצת.
11. מהו SLA freshness: 1s ל־market, 15s ל־account ו־30s ל־infra כפי שמוצע?
12. מהו host/branch/deployment contract המאושר של TST?
13. איזה baseline commit ישמר את השינויים הלא־מחויבים הפעילים כעת?

## 20. תכנית ביצוע והערכת היקף

| שלב | תוצר | היקף |
|---|---|---|
| 0 | שימור working tree, קביעת baseline ו־release identity | בינוני |
| 1 | אישור definitions, DTOs, timezone ו־security roles | בינוני |
| 2 | import source של Dashboard והתאמת static export/base path | בינוני |
| 3 | read-only summary/wallet/current event/positions API + cache | גדול |
| 4 | analytics queries, indexes/rollups וגרפים | גדול |
| 5 | החלפת mocks, states, RTL/accessibility/mobile | גדול |
| 6 | auth integration, viewer/operator capabilities | בינוני–גדול |
| 7 | unit/integration/financial fixtures/performance tests | גדול |
| 8 | TST deployment, real read-only validation, load test | בינוני |
| 9 | LIVE atomic deployment ללא controls | בינוני |
| 10 | controls design/approval/implementation release נפרד | גדול |

## Checklist מסכם

### קיים ומוכן

- [x] ריפו Dashboard עם קוד Next.js בפועל.
- [x] FastAPI LIVE עצמאי תחת `/live`.
- [x] nginx TLS catch-all ל־localhost:8001.
- [x] session cookie, CSRF, re-auth infrastructure.
- [x] public/private health.
- [x] Market WS ו־User WS health/state.
- [x] SQLite WAL, account snapshots, markets, alerts ו־timeline.
- [x] strategy positions/deals/fills/intents persisted.
- [x] backup/archive timers ו־rollback runbook.

### קיים אך דורש התאמה

- [ ] `/live/health` ו־`/live/strategy/status` ל־DTO קטן ויציב.
- [ ] auth ל־`/operations` ו־role/capability model.
- [ ] Next routing/base path/static export/fonts/assets.
- [ ] mock UI ל־typed API client.
- [ ] PnL/fees/redemption calculations ל־contract מאושר.
- [ ] logs/alerts ל־pagination/stale/error UX.
- [ ] deployment state ל־release identity אמין.

### חסר ודורש פיתוח

- [ ] dashboard summary/cache.
- [ ] wallet/equity aggregator.
- [ ] current/next event view model.
- [ ] strategy orders/deals/redemptions read APIs.
- [ ] chart/stats range APIs ו־indexes/rollups.
- [ ] OS/systemd read-only health sampler.
- [ ] loading/error/empty/stale states.
- [ ] test fixtures ל־partial fills, fees, resolution ו־redeem.
- [ ] RBAC/authorization מעבר ל־admin יחיד.

### חסום בגלל מידע או קוד חסר

- [ ] baseline מאושר ל־working tree הפעיל.
- [ ] אימות סביבת TST בפועל.
- [ ] SLA/thresholds לביצועים ול־freshness.
- [ ] policy מאושר להצגת נתוני חשבון.

### דורש החלטת משתמש

- [ ] הגדרת “עסקה”.
- [ ] total equity מול balance בגרף הראשי.
- [ ] timezone יומי.
- [ ] denominator לאחוז PnL.
- [ ] הגדרת skipped/breakeven.
- [ ] מיקום והרשאות של controls.
- [ ] משמעות “השבת מכונה”.
- [ ] מדיניות redemption.
- [ ] baseline commit ו־TST target.

---

## סיכום החלטת Go/No-Go

**No-Go למימוש/פריסה מיידיים.**  
**Go לשלב תכנון מפורט ויישור definitions/baseline בלבד.**

לאחר אישור המשתמש על סעיף 19, ניקוי אי־הוודאות של release identity ואימות TST, אפשר להתחיל בשלב read-only API + static Dashboard ללא פעולות שליטה. חיבור controls צריך להישאר release נפרד לאחר בדיקות ואישור מפורש.
