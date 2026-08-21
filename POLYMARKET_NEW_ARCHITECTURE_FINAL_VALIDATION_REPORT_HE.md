# דוח מימוש ו־Validation — הארכיטקטורה החדשה של Polymarket LIVE

**תאריך:** 2026-08-09  
**ענף:** `architecture/trading-core-refactor-20260808`  
**בסיס:** `POLYMARKET_NEW_ARCHITECTURE_IMPLEMENTATION_STATUS_HE.md`

## תקציר

כל פערי הקוד, הבדיקות וה־deployment שהוגדרו במסמך הסטטוס הושלמו. נשארו שתי פעולות מכוונות:

1. canary אמיתי בסך $5 — דורש אישור פיננסי מפורש לפני שליחת order.
2. soak של 30–60 דקות — הושאר בכוונה להרצה ידנית לפי בקשת המשתמש.

## מה בוצע

### Persistence writer resilience

- חיבור SQLite ארוך־חיים נסגר ונפתח מחדש לאחר write failure.
- retry ישן אינו דורס snapshot או state חדשים שנכנסו בזמן שה־batch נכשל.
- נוסף fault test שמדמה `sqlite3.OperationalError` ומוודא שהחיבור הבעייתי נזרק.

### Market metadata locking

- קריאות SQLite הוצאו מחוץ ל־market cache publication lock.
- ה־lock מוחזק רק בזמן atomic reference swap.
- בדיקת concurrency מוכיחה ש־publish קצר אינו נחסם בזמן lookup איטי.

### User WebSocket isolation

- condition IDs ו־outcome mapping נקראים מ־RAM market cache.
- order/trade events נכנסים ל־FIFO persistence queue.
- receive path אינו ממתין ל־SQLite.
- reconciliation מופעל לאחר persistence מסודר.
- overflow או persistence failure גורמים ל־`pause_entries=true` — fail closed.
- בדיקת DB slowdown הזרימה 20 אירועים מול write delay של 50ms ללא drop וללא חסימת ingress מהותית.

### Process isolation

נוספו:

- `trader_app.py` — בעלים יחיד של Market WS, User WS, Strategy, adapter, reconciliation, DB writes ו־signing.
- `dashboard_app.py` — UI/API בלבד; אינו מאתחל adapter, WebSockets או Strategy.
- `polymarket-trader.service`.
- `polymarket-dashboard.service`.
- `dashboard.env.example` ללא private key וללא Polymarket API signing credentials.

בוצעה בדיקת process integration עם שני תהליכי Uvicorn אמיתיים, DB זמני ו־Unix Socket אמיתי. שני התהליכים עלו, ה־Dashboard הגיע ל־Trader וה־IPC החזיר את כל רכיבי הסטטוס.

### Unix Socket IPC

נוסף `/run/polymarket/trader.sock` עם:

- JSON line protocol מוגבל ל־1MiB.
- הרשאות socket `0660`.
- בדיקת Linux peer UID באמצעות `SO_PEERCRED`.
- timeout ו־structured errors.
- command allowlist קשיח ב־Trader.

פקודות state-changing של ה־Dashboard מנותבות ל־Trader, כולל pause/resume, kill switch, emergency close, reconciliation, alerts, rules, fixtures, maintenance, backup, account refresh ו־dry-run.

### Dashboard read-only

`LiveRepository(query_only=True)` משתמש ב:

```sql
PRAGMA query_only = ON;
```

וב־SQLite URI עם `mode=ro`.

בדיקה מוכיחה שקריאות עובדות וכל ניסיון כתיבה נכשל עם `sqlite3.OperationalError`. גם migrations חסומות במפורש.

### Security boundary

- Dashboard production unit משתמש ב־`dashboard.env` נפרד.
- קובץ זה אינו מכיל private key, API key, API secret או API passphrase.
- רק Trader טוען את סביבת המסחר וה־signing.
- Dashboard אינו מסוגל לבצע order מקומית; פעולות עוברות ל־Trader דרך IPC.

### Block detector

נוסף watchdog שב־event-loop lag של 50ms ומעלה שומר:

- timestamp.
- lag מדויק.
- שמות asyncio tasks.
- stack snapshot של כל task פעיל.

קיים cooldown כדי למנוע הצפת diagnostics. בדיקה יזומה של stall באורך 80ms עברה.

### FAK zero-fill recovery

במהלך ה־deployment נמצא intent ישן של FAK שנדחה על ידי הבורסה עם `No orders found to match`, אך נשמר בעבר כ־`RECONCILIATION_REQUIRED` ללא remote order. לאחר אימות שאין order או position מרוחקים הוא נסגר כ־`ZERO_FILL`. ה־adapter תוקן כך ששגיאה זו תנורמל מעתה כ־`FAK_NOT_FILLED`, ונוספה בדיקת regression.

### Fault/restart validation

נבדקו:

- crash לפני submit: durable intent נשמר, duplicate entry נחסם ו־reconciliation עובר ל־gaps/pause.
- crash אחרי submit ולפני local response: remote/local mismatch מזוהה, אין retry ואין order כפול, והמערכת נעצרת fail-closed.
- restart עם fill/position קיים והתאוששות דרך reconciliation.
- duplicate idempotency.
- transport timeout כמצב uncertain ללא retry.
- cancel uncertainty ללא parallel sell.
- DB writer failure/reconnect.
- User WS persistence slowdown.

### Dashboard stress

בוצעו 50 read-only Dashboard refresh workloads בארבעה processes במקביל ל־Trader event-loop heartbeat.

תוצאה: PASS — event-loop lag נשאר מתחת לסף acceptance של 100ms.

### Controlled lifecycle validation

הסקריפט `run_live_strategy_fixture_soak.py` תוקן כך שה־RAM safety snapshot נטען לאחר הגדרת מצב הבטיחות.

תוצאה:

```text
events: 12
entry intents: 9
simultaneous skips: 3
frames processed: 22
duplicate entry groups: 0
parallel exit groups: 0
active positions: 0
legacy orders: 0
SQLite integrity_check: ok
```

הבדיקה כוללת Entry, simultaneous trigger skip, replay לאחר restart, TP וסגירת positions.

### uvloop benchmark

נוסף וקובע `uvloop==0.21.0` ונוסף benchmark חוזר של 100,000 פעולות, חמש חזרות.

```text
asyncio:
yield median:    533.498ms — 187,442 ops/s
callback median: 415.273ms — 240,806 ops/s

uvloop:
yield median:    310.778ms — 321,773 ops/s
callback median: 221.226ms — 452,026 ops/s
```

לכן `polymarket-trader.service` מוגדר במפורש עם `--loop uvloop`.

## בדיקות סופיות

```text
170 passed
9 subtests passed
7 deprecation warnings בלבד
systemd-analyze verify: PASS
git diff --check: PASS
process integration: PASS
controlled lifecycle: PASS
SQLite integrity: ok
```

## מצב deployment

ה־cutover לייצור הושלם בהצלחה עם rollback אוטומטי וגיבוי תחת:

```text
/opt/polymarket-btc-live/deployment-backups/architecture-split.ImKlhb
```

מצב השירותים לאחר הפריסה:

```text
polymarket-trader.service    active / enabled
polymarket-dashboard.service active / enabled
polymarket-live.service      inactive / disabled
Dashboard health             ok
Market WS                    CONNECTED
User WS                      CONNECTED
Strategy readiness           READY
Reconciliation readiness     READY
Heartbeat                    OK
Pause entries                true
```

קובצי ה־env מוגנים בהרשאות `0600 root:root`, ה־socket בהרשאות `0660 dvir:dvir`, וב־Dashboard אין private key או Polymarket API signing credentials.

## Real-money canary

לא נשלח order אמיתי. לאחר deployment תקין נדרש preflight חוזר ואישור מפורש לשליחת canary של עד $5. בדיקות mock/fault אינן תחליף להוכחת exchange lifecycle אמיתי.

## פקודת soak ידנית — 60 דקות

יש להריץ רק לאחר התקנת `polymarket-trader.service`, כאשר המערכת במצב בטוח:

```text
kill_switch=true
pause_entries=true
canary_armed=false
```

הפקודה המדויקת:

```bash
cd /opt/polymarket-btc-live/repo/polymarket-collector && trader_pid="$(systemctl show polymarket-trader.service -p MainPID --value)" && test "$trader_pid" -gt 0 && /opt/polymarket-btc-live/.venv/bin/python scripts/market_ws_soak_monitor.py --duration 3600 --interval 5 --pid "$trader_pid" --db /opt/polymarket-btc-live/poly_live.sqlite3 --diagnostics /opt/polymarket-btc-live/output/market_ws_latency_diagnostics.json --output "/opt/polymarket-btc-live/output/architecture_soak_$(date -u +%Y%m%dT%H%M%SZ).json"
```

הסקריפט הוא read-only ומתריע על כל שינוי ב־orders/fills/intents, חריגה ממצב הבטיחות, שינוי readiness, restart, backlog או latency.

## תוצאות soak של 60 דקות

הבדיקה הושלמה ב־2026-08-09 ללא הפרת בטיחות וללא שינוי במספר ה־orders, fills או intents:

```text
Duration                     3600.003 seconds
Records                      236,000
Diagnostic rotations         12
Trader PID changes           0
Safety violations            0
Count violations             0
Persistence failures         0
Cache refresh failures       0
Ingress saturation events    0
Snapshot drops/coalescing     0
```

מצב הבטיחות נשמר לכל אורך הבדיקה: `kill_switch=true`, `pause_entries=true`, `canary_armed=false`. השירות נשאר באותו PID, וכל מוני ה־orders/fills/intents נשארו ללא שינוי.

מדדי ה־hot path היו יציבים לאורך כל ארבעת חלונות ה־15 דקות:

```text
Total processing   p50 0.337ms  p95 0.551ms  p99 0.720ms  max 35.635ms
Handler -> book    p50 0.230ms  p95 0.415ms  p99 0.561ms  max 34.387ms
Book -> strategy   p50 0.051ms  p95 0.092ms  p99 0.157ms  max 35.426ms
Ingress wait       p50 0.182ms  p95 2.931ms  p99 5.652ms  max 235.222ms
Socket -> handler  p50 0.267ms  p95 2.980ms  p99 5.746ms  max 235.243ms
```

לא נצפתה הצטברות backlog: עומק ingress מרבי היה 16 מתוך 32, עומק persistence מרבי היה 2, ו־TCP/WS queues חזרו לאפס. צריכת CPU ממוצעת הייתה 33.2% (מקסימום 68.6%), מספר ה־threads נשאר 14, וה־RSS עלה בכ־7.1MiB בלבד במהלך שעה — ללא אינדיקציה ברורה לדליפה.

מנגנוני fail-closed הופעלו כנדרש: 7,342 frames עם exchange timestamp לא־סדור ועוד 6 frames ישנים נדחו; זמן `NOT_READY` מצטבר היה 28.25 שניות בלבד, והמקטע הארוך ביותר 2.741 שניות. אירוע `BEST_PRICE_MISMATCH` גרם לניתוק יזום ול־resync, והחיבור חודש לאחר כשתי שניות ללא order או fill.

הערה שאינה חוסמת: event-loop lag היה `p99=175.659ms` ו־`max=472.651ms`, מעל יעד ה־tail המקורי. עם זאת, לא הייתה הידרדרות בין חלונות הזמן, ה־hot-path processing נשאר מתחת ל־1ms ב־p99 ולא נוצר backlog. מומלץ לטפל בזנב זה כעבודת performance נפרדת ולא כחסם לפריסה הנוכחית.

קובץ התוצאות המלא:

```text
/opt/polymarket-btc-live/output/architecture_soak_20260809T091145Z.json
```

## החלטת סטטוס

```text
Trading Hot Path             PASS
Persistence resilience       PASS
User WS isolation            PASS
Process split implementation PASS
Unix Socket IPC              PASS
Dashboard read-only          PASS
Security boundary code       PASS
Fault/restart tests           PASS
Dashboard stress             PASS
uvloop benchmark             PASS
Local architecture validation PASS
Production deployment        PASS
Real $5 canary               PENDING EXPLICIT APPROVAL
60 minute soak               PASS
```
