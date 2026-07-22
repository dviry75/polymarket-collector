# מצב מערכת Polymarket LIVE והשלבים הבאים

תאריך בדיקה: 22 ביולי 2026, UTC. המסמך מבוסס על בדיקת הקוד ב־`main` (לפני commit המסמך: `36e0906`), תצורת השרת הפעילה, systemd, nginx, Certbot, SQLite, endpoints, logs ובדיקות אוטומטיות. לא בוצעו מסחר, שינוי תצורה, restart, migration, allowance, redeem או פעולת ארנק. ערכי סודות לא נקראו ולא נכללו.

## 1. תקציר מנהלים

המערכת באוויר: DEMO ו־LIVE פעילים, נגישים ב־HTTPS ומחזירים health תקין מקומית וציבורית. ממשק LIVE עולה, דורש Login, והטאבים שנבדקו החזירו HTTP 200 לאחר התחברות. עם זאת, HTTP 200 מעיד על זמינות ה־UI בלבד ולא על השלמת תהליך מסחרי.

אי אפשר ואסור לסחור כרגע בכסף אמיתי. ההגדרות הפעילות הן `TRADING_MODE=DEMO`, `LIVE_ADAPTER=mock`, `LIVE_KILL_SWITCH=true`, `LIVE_TRADING_ENABLED=false`, `LIVE_ORDER_SUBMISSION_ENABLED=false`, `LIVE_REDEMPTION_MODE=manual`. גם אם הדגלים ישונו, `RealPolymarketTradingAdapter` חוסם במפורש יצירה וביטול של פקודות ואינו מממש יתרות, allowances, orders, trades או positions אמיתיים.

החסם המרכזי הוא היעדר שכבת מסחר אמיתית מקצה לקצה: Credentials/Wallet, CLOB adapter, User WebSocket, lifecycle של orders/fills/positions, ביצוע Rules/TP/SL/End Event, Reconciliation והתאוששות — כולם לא מחוברים ולא אומתו מול Polymarket. בנוסף, מערכת ההפעלה Ubuntu 25.10 הגיעה ל־EOL ב־9 ביולי 2026, ואין להפעיל עליה מסחר אמיתי.

הערכת המוכנות היא מקצועית אך שמרנית:

| תחום | מוכנות | נימוק |
|---|---:|---|
| UI ותשתית | 78% | שני services, nginx, TLS, login, UI ו־DB נפרד עובדים; חסרים HA, capacity test ו־OS נתמך. |
| אבטחה בסיסית | 67% | Argon2id, cookies מאובטחים, CSRF, re-auth, firewall ו־loopback קיימים; rate limit בזיכרון בלבד, Admin יחיד ללא RBAC, sessions ללא TTL ו־OS EOL. |
| קליטת נתוני שוק | 28% | REST ציבורי ו־bounded WebSocket smoke קיימים; אין consumer רציף, supervisor, subscriptions דינמיים פעילים או הוכחת freshness לאורך זמן ב־LIVE. |
| מנגנון מסחר | 18% | interfaces, mock, risk gates ו־idempotency קיימים; adapter אמיתי חוסם כל כתיבה. |
| ניהול עסקאות | 22% | סכמת deals/orders/fills ו־mock lifecycle קיימות; אין executor ל־Rules, TP, SL או End Event. |
| התאוששות מתקלות | 25% | systemd restart, audit, backup ידני ו־reconciliation skeleton קיימים; אין recovery orchestration, backup מתוזמן או DR test. |
| מוכנות למסחר אמיתי | 12% | מעטפת בטיחות טובה כבסיס, אך המסלול האמיתי אינו מחובר או מאומת ו־OS אינו נתמך. |

## 2. ארכיטקטורה נוכחית

```text
Internet
  → UFW: 80/443 (ו־SSH 22)
    → nginx + TLS
      → poly.dvirtechnologies.com
        → 127.0.0.1:8000 → polymarket.service → app:app (DEMO)
          → /opt/polymarket-btc/polymarket-collector/poly_data.sqlite3
      → live-poly.dvirtechnologies.com
        → 127.0.0.1:8001 → polymarket-live.service → live_app:app (LIVE)
          → /opt/polymarket-btc-live/poly_live.sqlite3
```

- הריפוזיטורי: `/opt/polymarket-btc`, ענף `main`, remote `https://github.com/dviry75/polymarket-collector`.
- קוד האפליקציה בריפוזיטורי: `/opt/polymarket-btc/polymarket-collector`. ה־entrypoints הם `app.py` ל־DEMO ו־`live_app.py` ל־LIVE.
- פריסת LIVE: `/opt/polymarket-btc-live`, עם virtualenv עצמאי `/opt/polymarket-btc-live/.venv`. השוואת קבצים הראתה שקוד הפריסה זהה לקוד בריפוזיטורי; רק ב־checkout קיימים `output` ומסד/WAL/SHM של DEMO.
- DEMO משתמש ב־virtualenv `/opt/polymarket-btc/.venv`; LIVE משתמש ב־virtualenv נפרד.
- שני services הם `enabled`. LIVE מוגדר `Restart=on-failure`; DEMO `Restart=always`. שניהם עלו לאחר boot הנוכחי ומאז פעילים.
- nginx מפנה HTTP ל־HTTPS. LIVE כולל WebSocket upgrade headers ו־security headers; DEMO אינו כולל את אותה קבוצת headers.
- תעודות ECDSA תקפות: LIVE עד 20 באוקטובר 2026; DEMO עד 6 באוקטובר 2026. `certbot.timer` פעיל ומופעל פעמיים ביום.
- ports ‏8000/8001 מאזינים רק ב־`127.0.0.1`. nginx מאזין ב־80/443. UFW פעיל עם deny incoming כברירת מחדל ומאפשר 22/80/443.

## 3. מה כבר עובד

| יכולת | מה היא עושה והיכן | אימות | דירוג |
|---|---|---|---|
| Login | `/live/login`, `live/auth.py`; משתמש יחיד מוגדר | login מוצלח נראה ב־logs; tests בודקים Argon2id | חלקי |
| Sessions | cookie חתום HMAC ו־session version ב־DB | unit/integration tests; revoke-all מבטל version | חלקי; ללא TTL כברירת מחדל |
| Logout | מוחק cookie ודורש CSRF | tests ו־audit DB (2 פעולות logout) | מתאים ל־control UI |
| Dashboard | `/live`, טאבים ומדדי safety | נצפו HTTP 200 בטאבים לאחר Login | חלקי/Control-plane |
| Markets | טבלה, public metadata ו־Market WS fixture/smoke | tests; DB הפעיל ריק | Demo/Smoke בלבד |
| Orders/Fills | טבלאות, APIs, mock lifecycle ו־duplicate block | tests של filled/duplicate/partial policy | Mock בלבד |
| Deals | סכימה, תצוגה ויצירה דרך `TradingEngine` | tests ברמת mock; DB הפעיל ריק | חלקי |
| Rules | יצירה וולידציה בסיסית של entry/SL/TP | test יצירה; אין loop מבצע | קיים בקוד, לא פעיל |
| Reconciliation | השוואת local/remote skeleton ורישום gaps | test מול mock/fixture; 0 ריצות ב־LIVE DB | Mock בלבד |
| Audit | `live_audit_log` וייצוא Excel | 28 רשומות בפועל (26 login, 2 logout) | עובד בסיסית |
| Maintenance | Drain/Cancel/Readiness ו־final reconciliation | tests עוברים | חלקי; לא נבדק בעסקה אמיתית |
| Settings/Deployment | מציג safe config ו־readiness | קוד ו־UI; ללא ערכי סוד | עובד כתצוגת readiness |
| Health | `/health` ציבורי מצומצם ו־`/live/health` מוגן | 200 מקומי וציבורי בשני האתרים | עובד; אינו deep health |
| HTTPS | nginx + Certbot | `nginx -t`, curl ו־certificate inventory | עובד |
| Restart persistence | systemd enabled ו־DB על דיסק | `is-enabled`, status ו־start timestamps | חלקי; recovery עסקי חסר |
| DB separation | LIVE path נפרד מ־DEMO הפעיל | service health, env path וטבלאות | עובד, עם legacy tables ב־DEMO |
| Tests | `tests/test_live_system.py` | 12/12 עברו; 5 deprecation warnings | טוב ל־mock, לא הוכחת production |
| Security controls | Kill Switch, risk caps, CSRF, re-auth, secure cookie | קוד/tests/state פעיל | בסיס טוב, לא מלא |
| Backup infrastructure | SQLite backup API, gzip, SHA-256, retention | test עובר | ידני בלבד; אין backup פעיל/מתוזמן |

## 4. מצב ה־UI

ה־UI הוא control center יחיד המיוצר server-side מתוך `live/router.py`. טאבים קיימים: Overview, Operations, Risk, Logs, Market, Account, Dry Run, Reconciliation, Orders, Deployment ו־Maintenance.

| מסך | מוצג/פעולות | מקור נתונים ומגבלות |
|---|---|---|
| Login | הזדהות Admin | אמיתי; rate limit מקומי לתהליך בלבד. |
| Overview | mode, adapter, kill switch, counts, readiness | config ו־LIVE DB אמיתיים; כרגע רוב טבלאות המסחר ריקות. |
| Operations | Orders, fills, deals, rules ופעולות control | נתוני DB אמיתיים, אך מקור המסחר הוא mock ואין engine פעיל. |
| Risk | caps, exposure, loss limits ו־Kill Switch | הגדרות אמיתיות; counters טרם הוכחו מול fills אמיתיים. |
| Logs | Audit rows ו־WebSocket events | Audit אמיתי; אין ingestion מלא של operational logs/alerts. |
| Market | metadata, WS health ו־smoke | REST ציבורי/fixture/smoke; אין stream רציף. |
| Account | profile/proxy wallet readiness | יכולת public read קיימת; profile address/credentials לא מוגדרים בפועל. |
| Dry Run | preview ל־Entry/TP/SL/Exit | חישוב מקומי בלבד; balance/allowance מסומנים `NOT_CONFIGURED`. |
| Reconciliation | runs/gaps והפעלה ידנית | מול adapter הנבחר; כרגע mock, לכן אינו מעיד על חשבון אמיתי. |
| Orders | Orders/fills/deals tables | mock/ריק כרגע; cancel אמיתי חסום בקוד. |
| Deployment | safe config, secret readiness ו־checks | snapshot מועיל; אינו מחליף בדיקת runtime מלאה. |
| Maintenance | Drain, readiness, cancel drain, backup | מנגנון מקומי; אין אישור שהצליח עם orders/positions אמיתיים. |

פעולות מסוכנות מוגנות ב־session, CSRF ובחלקן re-auth; יצירת Rule דורשת re-auth. פעולות מסחר אמיתי, cancel אמיתי ו־redeem חסומות או אינן קיימות. HTTP 200 במסך מציין שה־route וה־rendering עובדים — לא שה־Market Data טרי, שה־wallet מחובר או שפקודה יכולה להתבצע ולהיסגר בבטחה.

## 5. מצב מסדי הנתונים

מסד LIVE הפעיל הוא `/opt/polymarket-btc-live/poly_live.sqlite3` (135,168 bytes בזמן הבדיקה). קבוצות הטבלאות: markets; rules; deals/positions; orders/fills; websocket events; account snapshots; reconciliation; daily limits/system state; audit; backups; dry runs. היו 28 audit rows, שתי רשומות state (`kill_switch=true`, `session_version=1`) ואפס markets, rules, deals, orders, fills, positions, reconciliation runs ו־backups.

מסד DEMO הפעיל אומת דרך `/health`: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3` (כ־206 MB, עם WAL/SHM פעילים). הוא מכיל `events`, `orderbook_log`, `btc_volume_log`, `rules`, `deals`, וגם 13 טבלאות `live_*` ישנות. טבלאות ה־LIVE בו ריקות, למעט `live_system_state` עם שורה אחת. השירות LIVE אינו משתמש בהן, ולכן כרגע זו שארית וסיכון תחזוקתי עתידי, לא ערבוב נתוני המסחר הפעיל.

קיימים גם `/opt/polymarket-btc/poly_data.sqlite3` ו־`/home/dvir/poly_data.sqlite3`, כל אחד 46,641,152 bytes, עם `events`, `orderbook_log`, `sqlite_sequence`; הם אינם הנתיב הפעיל של DEMO. בנוסף קיימים קובצי DB/backup לא מנוהלים ב־working tree ובבית המשתמש. אין למחוק אותם ללא inventory, snapshot, hash comparison, owner approval וחלון תחזוקה. הדרך הבטוחה בעתיד: לעצור כתיבות בחלון מאושר, ליצור SQLite online backup, לבצע `PRAGMA integrity_check` על העותק, למפות inode/hash/mtime ו־service paths, לארכב מחוץ לעץ Git, ורק אז להסיר legacy tables או copies בתהליך migration מתועד והפיך.

## 6. מצב האבטחה

| נושא | דירוג | ממצא |
|---|---|---|
| Argon2id | תקין | hash מוגדר; הקוד תומך ומעדיף Argon2id והבדיקה עוברת. קיימת fallback חלשה לצורות legacy ולכן יש למנוע שימוש בהן במדיניות. |
| Session Secret | חלקי | מוגדר וחתימת HMAC קיימת; אינו ב־Secret Manager וה־session ללא TTL כברירת מחדל. שינוי secret מבטל cookies קיימים. |
| Cookies | תקין בסיסית | `HttpOnly`, `Secure`, `SameSite=strict`; logout מוחק cookie. |
| CSRF | תקין בסיסית | token נגזר מה־session ונדרש ב־POST מוגנים; tests עוברים. |
| Login rate limiting | חלקי | 5 כשלונות/דקה, בזיכרון של process בלבד; reset ב־restart, ללא shared store/ban והסתמכות על כתובת proxy דורשת hardening. |
| Re-authentication | חלקי | קיים ל־Kill Switch off, revoke sessions ויצירת Rule; לא כל פעולה תפעולית רגישה דורשת re-auth. |
| Revoke sessions | תקין | increment של version ב־DB מבטל sessions; password change אינו workflow קיים, ולכן ביטול בעקבות שינוי password לא אומת. |
| Secret Manager | חסר | provider ל־Google קיים בקוד, אך שמות project/prefix ו־credentials אמיתיים אינם מוגדרים. |
| `live.env` | תקין בסיסית | `root:root`, mode `600`; values לא הוצגו. הוא מכיל גם שני secrets של Login ולכן אינו "non-secret env" בפועל. |
| nginx headers | חלקי | LIVE כולל nosniff, frame deny, referrer ו־permissions; חסרים HSTS ו־CSP. DEMO חסר headers מקבילים. |
| TLS | תקין | HTTPS עובד ותעודות בתוקף; renewal timer פעיל. nginx מזהיר על תחביר `listen ... http2` deprecated. |
| Health exposure | חלקי | `/health` ציבורי ומצומצם; DEMO health חושף path פנימי ושם market ולכן אינו minimal. |
| Logs וסודות | חלקי | לא נמצאו secrets בפלט שנבדק; access logs חושפים IPs ונסיונות scanner. אין redaction pipeline מוכח לכל payload עתידי. |
| משתמש Admin | חלקי | משתמש יחיד `Admin@system.com`; אין user store או lifecycle מלא. |
| RBAC | חסר | אין הפרדת viewer/operator/admin. |
| Ports/firewall | תקין | app ports ב־loopback; UFW deny inbound ומאפשר רק 22/80/443. ports ‏20201/20202 גם מאזינים ודורשים זיהוי, אף שאינם מורשים ב־UFW. |
| SSH | חלקי | socket פעיל ונראים ניסיונות brute-force ציבוריים; policy של keys, root login, OS Login ו־fail2ban לא אומתה. |
| Ubuntu | דורש טיפול דחוף | 25.10 הגיע ל־EOL ב־9 ביולי 2026; נתיב השדרוג הנתמך הוא 26.04 LTS. |

## 7. מצב מנגנון המסחר

### קיים בקוד

Interfaces ל־adapter; mock adapter; skeleton ל־Polymarket; REST public metadata/orderbook; bounded Market WebSocket smoke; User WS parser/fixture; repository; risk caps; order manager; trading engine ל־entry/exit intent; idempotency key ייחודי; mock fills; reconciliation skeleton; Rule schema; Dry Run; Audit; Kill Switch; Maintenance/Drain; GTC/FAK/FOK validation; price/tick/staleness/slippage fields.

### מחובר בפועל

רק UI, DB, auth, safety config, mock adapter, public health ותשתית HTTP/TLS מחוברים. אין background tasks ב־startup: אין market stream רציף, User WS, Rule evaluator, reconciliation scheduler, TP/SL watcher, end-event handler או backup scheduler. מסד LIVE הריק מאשר שלא הייתה קליטת market/order/deal פעילה.

### נבדק בדמו

Mock filled order, duplicate idempotency, FAK partial-fill rejection, fixture processing, mock reconciliation, Rule validation, Dry Run, Maintenance/Drain ו־backup נבדקו; 12 tests עברו. אין test מלא ל־GTC lifecycle, cancellation race, repeated partial fills, residual cleanup, restart באמצע עסקה או fees אמיתיים.

### נבדק מול Polymarket

REST/public client וקוד Market WS smoke קיימים, אך לא בוצעה במסגרת בדיקה זו קריאה authenticated ולא הופעל smoke שמשנה state. לא נמצאה ראיה ל־Market WebSocket רציף ב־LIVE. אין User WS אמיתי. אין credentials/wallet/allowances/balances אמיתיים, ואין BUY/SELL/cancel/redeem אמיתיים. לכן כל אלה: **לא אומתו מול Polymarket**.

### מוכן למסחר אמיתי

אף מסלול ביצוע אינו מוכן. פירוט:

| יכולת | מצב מדויק |
|---|---|
| Market data / Public WS | REST וקוד smoke קיימים; stream רציף, reconnect ו־freshness enforcement תפעולי אינם מחוברים. |
| User WebSocket | מחלקת fixture/parser בלבד; אין connect/auth/reconnect. |
| Order creation/cancellation/status | mock עובד; adapter אמיתי מחזיר blocked/not configured. |
| GTC/FAK/FOK | names ו־policy קיימים; FOK/FAK נבדקו חלקית ב־mock, GTC לא הוכח lifecycle-wise. |
| BUY/SELL | intent code קיים; לא מול Polymarket. |
| Take Profit / Stop Loss | שדות ו־Dry Run קיימים; אין watcher/executor. SL retry config אינו ממומש כלולאה. |
| End Event | אין handler שמסיים deal/position על resolution. |
| Partial fills | mock scenario ו־policy block קיימים; aggregation/lifecycle אמיתי חסר. |
| Residual positions | שדות remaining size קיימים; אין cleanup policy/executor. |
| Price protection/slippage | risk/dry-run fields קיימים; לא הוכח worst-price enforcement ב־CLOB אמיתי. |
| Fees | metadata fields ו־estimated fee=0 ב־Dry Run; אין fee calculation/accounting אמיתי. |
| Allowances/Balances | methods קיימים ב־interface; אמיתי מחזיר `not_configured`. לא בוצעה allowance. |
| Positions/Redemption | schema/public reads קיימים; adapter positions ריק ו־redeem אינו ממומש. mode הוא manual. |
| Reconciliation | skeleton ידני מול adapter; אין scheduler, authoritative merge או resolution workflow. |
| Recovery after restart | DB נשמר ו־service חוזר; אין rebuild של remote state או replay. |
| Recovery after WS disconnect | bounded smoke מסמן disconnect; אין daemon reconnect/backoff/resubscribe. |
| Duplicate prevention | DB idempotency עובד; lock בזיכרון אובד ב־restart ואינו distributed. |
| Event scoping | IDs נשמרים; אין engine שמבטיח full lifecycle scoped לאירוע. |
| Rule activation/deactivation | creation/status repository קיימים; אין endpoints מלאים להפעלה/כיבוי ואין executor. התנהגות deal פתוח אחרי deactivation לא מוגדרת. |
| Kill Switch | פעיל ומונע submission דרך RiskManager; לא מבטל orders קיימים ולא סוגר positions, ולא נבדק אמיתית. |
| Maintenance Drain | mode/readiness/reconciliation/backup קיימים; cancel-all/position closing אינם ממומשים end-to-end מול Polymarket. |

## 8. מה חסר לפני מסחר אמיתי

### קריטי — אסור לסחור לפני השלמה

- שדרוג מתועד ל־Ubuntu 26.04 LTS, snapshot/rollback ובדיקות לאחר השדרוג.
- Secret Manager, credentials, wallet/funder/proxy identity ו־least privilege — ללא הדפסת סודות.
- read-only account adapter אמיתי: balances, allowances, orders, trades, positions ו־fee schedule.
- Market WS רציף ו־User WS authenticated עם reconnect, staleness alarms ו־resubscription.
- CLOB adapter אמיתי ל־create/get/cancel, כולל GTC/FAK/FOK, BUY/SELL ו־idempotency durable.
- authoritative order state machine: ack, live, matched, partial, filled, cancel pending/cancelled/rejected/unknown.
- TP, SL, End Event, partial-fill ו־residual-position executors עם worst-price/fee/slippage policy.
- reconciliation תקופתי ואחרי restart/disconnect, כולל חסימת מסחר על gap ו־manual resolution.
- Kill Switch אמיתי שנבדק: עצירת entries, cancel orders, policy לפוזיציות והתראה; Maintenance Drain אמיתי.
- backup מתוזמן, off-VM, restore test; monitoring/alerts מחוץ ל־DB.
- runbooks, canary Go/No-Go, two-person approval ומגבלות כספיות.

### חשוב — ניתן לעלות ל־Canary מוגבל אך לא למסחר מלא

- RBAC או לפחות הפרדת viewer/operator/admin; session TTL ו־durable/distributed login throttling.
- CSP/HSTS, hardening SSH, זיהוי ports ‏20201/20202 ו־security review נוסף.
- capacity/soak test ל־SQLite ולשני services על 2 vCPU/1.9 GiB ללא swap.
- audit export מאובטח, operational dashboard, manual reconciliation tooling ו־data retention.
- הפרדת LIVE ל־VM/DB מנוהל אם load או criticality מצדיקים זאת.

### ניתן לדחות

- עיצוב UI מתקדם, גרפים נוספים ומסכי נוחות.
- multi-user מלא מעבר למינימום הפרדת תפקידים שנדרש ל־Canary.
- אופטימיזציות ביצועים שאינן נדרשות לפי load test.
- redemption אוטומטי; אפשר להישאר manual עם runbook ואישור כפול.

## 9. סיכונים מרכזיים

| סיכון | חומרה | הסתברות | השפעה | מצב נוכחי | טיפול מומלץ |
|---|---|---|---|---|---|
| Ubuntu EOL | קריטית | ודאית | ללא security updates | 25.10 EOL | שדרוג 26.04 LTS לפני Canary |
| מסדי DB כפולים/legacy | בינונית | גבוהה | טעויות תפעול/גיבוי | 3 DEMO paths ו־live_* ב־active DEMO | inventory, archive, migration מאושר |
| SQLite תחת עומס | גבוהה | בינונית | locks/latency/lost timing | DEMO כ־206 MB עם WAL, LIVE נפרד | load test, busy timeout, backup, שקילת DB מנוהל |
| LIVE+DEMO על VM אחד | גבוהה | בינונית | resource contention/failure domain | 2 vCPU, 1.9 GiB, ללא swap; LIVE peak 1.2 GiB | limits, monitoring, soak; הפרדה לפני scale |
| Market/User WS נופל | קריטית | גבוהה | state stale והחלטות שגויות | אין consumer רציף/User WS | reconnect/backoff, stale block, alert |
| פקודה לא בוטלה | קריטית | בינונית | חשיפה לא רצויה | cancel אמיתי חסום | cancel state machine + reconcile |
| Partial fill | קריטית | גבוהה | exposure לא צפוי | policy/mock בלבד | fill aggregation ו־residual policy |
| Residual position | קריטית | בינונית | סיכון נשאר פתוח | אין cleanup executor | deterministic exit escalation |
| איבוד חיבור בזמן SL | קריטית | בינונית | הפסד גדל | SL אינו פעיל | independent watcher, retry/worst-price/fallback |
| סוד נחשף | קריטית | בינונית | גניבת כספים | secrets ב־root env; probes ציבוריים | Secret Manager, rotation, redaction, least privilege |
| אין backup אוטומטי | גבוהה | גבוהה | אובדן DB/audit | קוד ידני, 0 backups, אין timer | scheduled off-host backup + restore drill |
| אין התראות חיצוניות | גבוהה | גבוהה | תגובה מאוחרת | Noop provider כותב audit בלבד | Pager/Telegram/Email עם escalation |
| restart בזמן עסקה | קריטית | בינונית | כפילות/עסקה יתומה | אין startup reconciliation | boot barrier ו־remote truth rebuild |
| stale market data | קריטית | בינונית | מחיר שגוי | risk gate קיים, feed לא פעיל | monotonic timestamps + hard block |
| כפילות פקודות | קריטית | בינונית | חשיפה כפולה | DB key טוב, memory lock בלבד | exchange client ID + persistent intent state |
| עמלות/החלקה | גבוהה | גבוהה | P&L שגוי | estimated fee אפס | real fee fetch, conservative worst price |
| סריקות/SSH brute-force | גבוהה | גבוהה | compromise | ניסיונות נראים ב־logs | key-only, OS Login policy, fail2ban/IAP |
| דיסק/משאבים | בינונית | בינונית | outage/DB failure | root 72%, 5.2 GiB פנוי; אין swap | thresholds, cleanup מאושר, VM sizing |

## 10. תוכנית עבודה מומלצת

בכל השלבים עד Canary אין לגעת בכסף אמיתי; Canary דורש אישור ידני מפורש.

1. **ייצוב OS ותשתית.** מטרה: בסיס נתמך. פעולות: snapshot, תכנית rollback, upgrade ל־26.04 LTS, nginx warning fix, resource limits. תוצר: VM נתמך. קבלה: services/health/tests/TLS/ports תקינים לאחר reboot. סיכון גבוה; אישור ידני נדרש; אין מידע פיננסי; Demo בלבד; ללא כסף.
2. **גיבוי ו־DR.** להפעיל backup מתוזמן, מוצפן ו־off-host ל־LIVE ול־DEMO ולתרגל restore. קבלה: checksum, integrity check ו־RTO/RPO מתועדים. סיכון בינוני; נדרשים יעד/retention מהבעלים; ללא כסף.
3. **יישוב DB inventory.** למפות copies ו־legacy `live_*`, לארכב ולנקות רק בתהליך מאושר. קבלה: service paths יחידים ו־rollback. סיכון גבוה; אישור ידני; Demo בלבד.
4. **חיבור Polymarket לקריאה בלבד.** REST/CLOB public + account reads ללא write. קבלה: IDs, token mapping, fees, balances ו־positions עקביים. סיכון נמוך; נדרש profile/wallet public address; ללא כסף.
5. **Market WebSocket רציף.** supervisor, subscribe/resubscribe, heartbeat/stale gate. קבלה: soak של 24–72 שעות וניתוק יזום. ללא כסף.
6. **User WebSocket.** auth דרך Secret Manager, fills/orders stream ו־reconnect. קבלה: fixture + sandbox/חשבון מאושר, ללא דליפת auth. נדרשים credentials; תחילה ללא submission.
7. **Secret Manager.** להוציא trading secrets מ־env, להגדיר IAM/rotation/readiness. קבלה: service קורא secrets, logs נקיים. סיכון גבוה; אישור בעלים; ללא עסקה.
8. **Wallet/Balances.** לאמת profile/proxy/funder, collateral chain ויתרות read-only. קבלה: התאמה כפולה מול UI/API. נדרש wallet public identity; ללא transfer.
9. **Allowances.** תחילה בדיקת read-only; שינוי allowance רק בחלון נפרד ובאישור מפורש. קבלה: spender/chain/cap מאומתים. נוגע בהרשאת כסף ולכן אסור אוטומטית.
10. **Order lifecycle ב־mock/DEMO.** state machine מלאה, retries, timeout, unknown, cancel races ו־idempotency. קבלה: deterministic tests ו־property/fault tests. Demo בלבד.
11. **BUY.** תחילה signed request construction offline, אחר כך Canary מינימלי. קבלה: ack/fill/reconcile. אישור ידני; נוגע בכסף רק בשלב Canary.
12. **SELL.** אותו מסלול, כולל max sell size ו־position ownership. קבלה: exposure חוזר לאפס. אישור ידני.
13. **GTC/FAK/FOK.** matrix בדיקות לכל status/cancel/partial. קבלה: semantics מול API מתועדים. Canary נדרש רק לבדיקת אמת.
14. **Partial fills.** aggregation, remaining size, cancel remainder ופוליסה. קבלה: multi-fill/out-of-order/duplicate events. תחילה Demo.
15. **Take Profit.** evaluator + exit intent + worst price. קבלה: trigger חד־פעמי, partial/retry/reconcile. תחילה Demo.
16. **Stop Loss.** watcher בלתי תלוי, FAK/retries/escalation. קבלה: disconnect/stale/partial/failure drills. תחילה Demo; Canary באישור.
17. **End Event.** resolution detection, halt entries, settle/mark/redeem manual. קבלה: resolved/cancelled/ambiguous cases. ללא redeem אוטומטי.
18. **Restart/recovery.** startup barrier, remote snapshot, resume/stop decisions. קבלה: restart בכל lifecycle state ללא כפילות. Demo בלבד תחילה.
19. **Reconciliation.** scheduler, authoritative comparison ו־gap workflow. קבלה: injected missing order/fill/position חוסם ומתריע. Demo בלבד תחילה.
20. **Kill Switch.** להגדיר stop-new, cancel-open ו־position policy. קבלה: drill כולל WS/API failure. שינוי state דורש re-auth ואישור.
21. **Maintenance Drain.** close/hold policy, final reconcile, backup, stop-ready. קבלה: אין non-final orders/exposure. Demo בלבד תחילה.
22. **Monitoring/alerts.** latency, stale WS, gaps, P&L, disk, memory, service, cert ו־backup. קבלה: alerts מגיעים לערוץ חיצוני ומתבצע acknowledgment. נדרש ערוץ מהבעלים.
23. **Canary trade.** עסקה יחידה בסכום מינימלי, two-person checklist, observation וחזרה לאפס. קבלה: BUY→fill→SELL/exit→reconcile→audit→P&L. אישור מפורש; נוגע בכסף.
24. **מעבר הדרגתי.** הגדלת caps רק לאחר מספר canaries וימי soak ללא gaps. קבלה: SLO ו־loss limits. אישור בכל מדרגה; נוגע בכסף.

## 11. סדר עדיפויות

| עדיפות | משימה | למה היא חשובה | תלות | הערכת מורכבות | מצב |
|---|---|---|---|---|---|
| P0 | שדרוג OS + snapshot/rollback | OS EOL | חלון תחזוקה | גבוהה | חסר |
| P0 | גיבוי מתוזמן off-host + restore | הגנת DB/audit | יעד גיבוי | בינונית | קוד ידני בלבד |
| P0 | Real adapter read-only + identity | בסיס לאמת חשבון | public wallet/credentials מאובטחים | גבוהה | חסר |
| P0 | Market/User WS רציפים | אמת בזמן אמת | adapter/secrets | גבוהה | חסר |
| P0 | Order state machine + reconcile/recovery | מניעת כפילות/יתומים | WS/adapter | גבוהה מאוד | skeleton |
| P0 | TP/SL/End Event/partial/residual | בטיחות יציאה | lifecycle | גבוהה מאוד | חסר executor |
| P0 | Kill Switch/Drain drills | עצירה בטוחה | cancel/reconcile | גבוהה | חלקי |
| P1 | Monitoring והתראות | זיהוי מהיר | metrics/channels | בינונית | Noop בלבד |
| P1 | Fees/slippage/worst-price | P&L וביצוע בטוח | market/order APIs | בינונית | חלקי |
| P1 | DB inventory/legacy cleanup | מניעת טעות תפעולית | backups | בינונית | לא טופל |
| P1 | Security hardening/RBAC/session TTL | צמצום תקיפה | OS stable | בינונית | חלקי |
| P2 | VM separation/DB migration | failure-domain ו־scale | load test | גבוהה | לא הוחלט |
| P2 | Audit/reconciliation tooling | תפעול וחקירה | lifecycle | בינונית | חלקי |
| P3 | UI/גרפים/עיצוב | נוחות | none | נמוכה | ניתן לדחות |

## 12. Checklist לפני Canary Trade

- [ ] GO: Ubuntu 26.04 LTS נתמך, patched, rebooted ונבדק; אחרת NO-GO.
- [ ] GO: snapshot ו־rollback תורגלו; אחרת NO-GO.
- [ ] GO: backup off-host תקין ו־restore נבדק; אחרת NO-GO.
- [ ] GO: code revision ו־deployment revision זהים ומאושרים.
- [ ] GO: secrets ב־Secret Manager, הרשאות מינימליות ו־rotation plan.
- [ ] GO: wallet/proxy/funder/chain/contract IDs אומתו על ידי שני אנשים.
- [ ] GO: balances ו־allowances נקראו והתאימו; לא בוצע allowance ללא אישור נפרד.
- [ ] GO: Market WS ו־User WS healthy לאורך soak וה־stale gates נבדקו.
- [ ] GO: order lifecycle, BUY/SELL, GTC/FAK/FOK, cancel ו־unknown state עברו ב־Demo.
- [ ] GO: partial fills ו־residual positions עברו fault tests.
- [ ] GO: TP, SL ו־End Event עברו drills כולל disconnect/restart.
- [ ] GO: reconciliation מחזורי ואחרי restart עבר ללא gaps.
- [ ] GO: Kill Switch ו־Drain עברו drill ומתקבל alert חיצוני.
- [ ] GO: caps: עסקה אחת, סכום Canary מוסכם, exposure/loss limits קשיחים.
- [ ] GO: fee/slippage/worst price/min size/tick size מאומתים בזמן אמת.
- [ ] GO: on-call, ערוץ התראה וחלון תפעול מאויש.
- [ ] GO: runbook לכשל cancel/SL/residual זמין.
- [ ] GO: אישור מפורש של הבעלים מיד לפני arm; כל סעיף חסר הוא NO-GO.

## 13. Checklist לפני מסחר מלא

- [ ] כל Checklist ה־Canary ירוק ומתועד.
- [ ] מספר Canaries מוסכם הושלם מקצה לקצה ללא reconciliation gaps.
- [ ] 7–14 ימי soak לפחות ל־feeds, alerts, backups ומשאבים.
- [ ] recovery נבדק ב־restart, network partition ו־API degradation.
- [ ] limits נבדקו בעומס ומוגדלים בהדרגה בלבד.
- [ ] SQLite הוכח כמתאים לעומס או הוחלף ב־DB מנוהל.
- [ ] RPO/RTO, retention, audit export ו־incident response מאושרים.
- [ ] RBAC/two-person approval לפעולות קריטיות פעילים.
- [ ] vulnerability/dependency scan ו־external security review הושלמו.
- [ ] P&L, fees, fills ו־positions הושוו ידנית לדוחות Polymarket.
- [ ] redemption policy מאושרת ונבדקה בלי אוטומציה לא מבוקרת.
- [ ] capacity margin מספק כאשר DEMO ו־LIVE יחד, או שהשירותים הופרדו.
- [ ] בעל המערכת אישר בכתב caps, loss policy, concurrency ו־go-live window.

## 14. פקודות תפעול שימושיות

הפקודות הבאות הן קריאה בלבד, למעט פקודת restart שמסומנת במפורש. הן אינן מציגות secrets.

```bash
sudo systemctl status polymarket.service --no-pager -l
sudo systemctl status polymarket-live.service --no-pager -l
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8001/health
curl -fsS https://poly.dvirtechnologies.com/health
curl -fsS https://live-poly.dvirtechnologies.com/health
sudo journalctl -u polymarket-live.service -n 200 --no-pager
sudo journalctl -u polymarket.service -n 200 --no-pager
sudo nginx -t
sudo certbot certificates
df -hT
free -h
ss -lntp
/opt/polymarket-btc-live/.venv/bin/python -c "import sqlite3; p='/opt/polymarket-btc-live/poly_live.sqlite3'; c=sqlite3.connect('file:'+p+'?mode=ro',uri=True); print([r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")])"
systemctl list-timers --all --no-pager
sudo find /opt/polymarket-btc-live/backups -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %s %p\n'
```

Restart של LIVE בלבד — פעולה משנת מצב, רק בחלון מאושר ואחרי Drain/backup/reconciliation:

```bash
sudo systemctl restart polymarket-live.service
sudo systemctl status polymarket-live.service --no-pager -l
curl -fsS http://127.0.0.1:8001/health
```

Rollback בטוח אינו `git reset --hard`: שמור revision פריסה קודם ו־DB backup, בצע Drain, פרוס עותק code מאושר לתיקייה versioned/temporary, החלף release באופן אטומי, restart ל־LIVE בלבד, בדוק health ו־reconciliation. שחזור DB יבוצע רק אם schema/data מחייבים זאת, מעותק שעבר checksum/integrity check ובאישור מפורש.

## 15. Rollback ותוכנית חירום

- **LIVE לא עולה:** השאר Kill Switch פעיל; בדוק status/journal/config/DB permissions; חזור ל־release הקודם בלי לשנות DB עד להבנת התאימות.
- **nginx נכשל:** אל תבצע reload אם `nginx -t` נכשל; שחזר config מאושר ובדוק שוב. DEMO/LIVE נשארים על loopback.
- **TLS נכשל:** אל תעקוף HTTPS למסחר; בדוק timer, certificate dates ו־renewal logs; עצור go-live.
- **DB ננעל:** עצור submissions, שמור WAL/SHM, בדוק process writers ו־busy state; אל תמחק WAL/SHM. בצע backup/integrity diagnostics.
- **WebSocket נופל:** חסום entries, סמן data stale, reconnect/resubscribe, בצע REST snapshot ו־reconciliation לפני resume.
- **פקודה כפולה:** הפעל Kill Switch, אל תניח איזו פקודה תקפה; קרא remote orders/fills, בטל רק לאחר זיהוי חד־משמעי, reconcile exposure.
- **Stop Loss לא בוצע:** Kill Switch, alert מיידי, בדיקת position/order authoritative; הפעל manual emergency exit לפי worst-price policy ואישור.
- **נשארה פוזיציה:** חסום rule/entries, reconcile fills ו־size, בצע residual runbook; אל תסמן deal closed לפני exposure אפס.
- **עצירת מסחר:** הפעל Kill Switch, Drain, cancel open orders, נהל positions לפי policy, final reconciliation ו־backup.
- **השרת נופל:** אל תפעיל submission מיד לאחר boot; startup barrier חייב לקרוא remote state, reconcile ולהמתין לאישור operator.

## 16. מידע שחסר מבעל המערכת

- כתובת profile/wallet הציבורית, סוג login, proxy/funder וה־chain — ללא private key בצ'אט או במסמך.
- אישור שימוש ב־Google Secret Manager, project/prefix ו־IAM owner.
- סכום Canary, max trade, max total exposure, max open trades והפסד יומי/כולל.
- מדיניות SL: trigger source, worst price, retries, זמן escalation והאם מותר marketable FAK.
- מדיניות TP, End Event, unresolved/cancelled market ו־manual override.
- מדיניות partial fill: accept/cancel remainder/complete, ומינימום residual.
- מדיניות residual position ו־emergency liquidation.
- הנחות fees/slippage ומקור authoritative.
- מדיניות GTC/FAK/FOK לכל purpose.
- redemption: manual steps, approver ו־schedule.
- ערוצי alerts, on-call ו־SLA acknowledgment.
- יעד backup חיצוני, encryption, retention, RPO/RTO.
- חלון שדרוג OS ותחזוקה, ואישור snapshot/rollback.
- החלטה אם להפריד LIVE מ־DEMO ל־VM/DB נפרדים.
- דרישות RBAC/two-person approval ומשך session.

## 17. מסקנה

עובדים בפועל: שני האתרים, TLS, nginx, systemd, Login/Logout/Sessions, UI, health, DB LIVE נפרד, audit בסיסי, Kill Switch פעיל, mock workflow ובדיקות. קיימים בקוד אך לא אומתו במסחר אמיתי: risk/order/deal abstractions, Market WS smoke, User WS fixture, reconciliation, backup, Drain ו־Secret Manager provider.

לא עובדים כמערכת מסחר אמיתית: adapter authenticated, create/cancel/status אמיתי, balances/allowances, User WS, execution רציף של Rules, Take Profit, Stop Loss, End Event, partial fills, residual cleanup, recovery, scheduled reconciliation, scheduled off-host backup והתראות חיצוניות.

לכן **אסור לבצע מסחר אמיתי כרגע**. השלב הבא המדויק ביותר הוא: snapshot וגיבוי מאומת, שדרוג ה־VM מ־Ubuntu 25.10 EOL ל־26.04 LTS, ואז חיבור read-only ל־Polymarket ו־Market/User streams לפני כל יכולת כתיבה.

שלושת הדברים הקריטיים ביותר הם: (1) OS נתמך וגיבוי/rollback מוכחים; (2) אמת חשבון ושוק רציפה — adapter read-only, Market WS, User WS ו־reconciliation; (3) lifecycle בטוח ומוכח ל־orders/fills/positions עם TP/SL/End Event/Kill Switch/Recovery לפני Canary.

מקורות חיצוניים למצב ה־OS: [הודעת Ubuntu על EOL של 25.10 ב־9 ביולי 2026](https://discourse.ubuntu.com/t/ubuntu-25-10-questing-quokka-reached-end-of-life-on-9-july-2026/85017), [מחזור החיים הרשמי של Ubuntu ו־26.04 LTS](https://ubuntu.com/about/release-cycle?product=ubuntu&release=ubuntu&version=26.04+LTS).
