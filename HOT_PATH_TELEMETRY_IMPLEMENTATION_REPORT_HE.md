# דוח יישום Telemetry ל־Market WebSocket Hot Path

תאריך: 2026-08-21 UTC  
פרויקט: `polymarket-btc-live`  
סביבה שנבדקה בקוד: `LIVE / REAL_TRADING`  
מצב ביצוע: code review checkpoint בלבד; לא בוצע deploy או restart.

## תקציר

נוספה טלמטריה bounded, בזיכרון, ללא כתיבת DB או log לכל frame. הנתונים נחשפים read-only דרך `MarketWebSocketManager.health()["hot_path_telemetry"]`, ולכן הם מגיעים למנגנון STATUS הקיים ללא service או dashboard חדשים.

ה־reader עדיין מתחיל **אחרי** `await self.on_reconnect()`. סדר זה לא שונה בכוונה. גם reconciliation, readiness, order-book, freshness threshold, queue sizes, strategy ו־execution לא שונו.

## A. What Was Added

### 1. WebSocket receive backlog

| Name | Definition | Measurement point | Unit | Overhead | Limitations |
|---|---|---|---|---|---|
| `ws_library_queue_depth_current` | עומק התור הפנימי האחרון שנצפה | מיד לאחר חזרת `ws.recv()`, לפני JSON parsing | frames | `len()` בלבד | best-effort על `ws.recv_messages.frames`, API פרטי של הספרייה; אינו kernel/TCP queue; ה־frame שכבר נשלף אינו נספר |
| `ws_library_queue_depth_high_watermark` | המקסימום שנצפה לאורך חיי התהליך | אותה נקודה | frames | השוואת scalar | lifetime high-watermark, לא rolling max |
| `ws_library_queue_depth_p50/p95/p99` | percentiles של 4,096 הדגימות האחרונות | מחושב רק בעת קריאת STATUS | frames | append O(1) ב־hot path; sort בזמן read | sampling מתרחש רק כאשר reader חוזר מ־`recv`; אינו רואה עומק בזמן שה־reader תקוע |
| `ws_library_queue_depth_unavailable_samples` | מספר פעמים שבהן מבנה הספרייה לא היה נגיש | לאחר `recv` | count | increment | מאפשר לזהות version incompatibility בלי להמציא queue depth |
| `consecutive_recv_drain_count` | רצף נוכחי של `recv` שחזרו בתוך ≤1ms | לאחר `recv` | count | scalar | proxy בלבד; אינו נקרא queue depth |
| `max_consecutive_immediate_recv` | הרצף המקסימלי של immediate receives | לאחר `recv` | count | scalar | proxy בלבד; scheduler/network יכולים להשפיע |
| `recv_to_recv_gap_ms` | זמן בין שתי חזרות עוקבות מ־`ws.recv()` | reader | ms | rolling ring חסום | כולל parsing/enqueue/event-loop scheduling בין receives |

מדידת עומק התור נשענת על inspection שהיה קיים לפני patch זה. היא עטופה ב־fallback בטוח ל־`None`; אין תלות בה לצורך התנהגות. אם גרסת `websockets` תפסיק לחשוף את המבנה, ה־proxy metrics ימשיכו לפעול.

### 2. Event Loop Lag

| Name | Definition | Measurement point | Unit | Overhead | Limitations |
|---|---|---|---|---|---|
| `event_loop_lag_ms.current/p50/p95/p99/max` | drift מול wake-up צפוי של watchdog | task תקופתי; 100ms במצב רגיל | ms | wake-up אחד ל־100ms ו־append חסום | `max` הוא lifetime; percentiles הם 4,096 הדגימות האחרונות |
| `event_loop_lag_buckets.gt_100ms/gt_500ms/gt_1000ms/gt_5000ms/gt_20000ms` | counters cumulative עבור stalls שחצו כל threshold | helper יחיד `_record_event_loop_lag` | count | חמישה comparisons למדידה | buckets מצטברים; stall של 20s נספר בכל thresholds הנמוכים |
| disconnect snapshot: `event_loop_lag_ms`, `event_loop_lag_max_lifetime_ms` | lag נוכחי ומקסימום בעת disconnect | `mark_disconnect` | ms | רק בעת disconnect | ה־current הוא הדגימה האחרונה, לא בהכרח השיא המדויק ברגע הסגירה |

כאשר heavy diagnostics מופעלים במפורש, interval נשאר 10ms וניתן לבצע stack capture. במצב הרגיל stack capture וכתיבת diagnostics נשארים כבויים.

### 3. Message Timing

| Name | Definition | Measurement point | Unit | Overhead | Limitations |
|---|---|---|---|---|---|
| `exchange_timestamp_ms` | timestamp מהודעת exchange לאחר normalization | כניסה ל־`process_message` | Unix ms | parsing קיים | תלוי באיכות timestamp מה־exchange |
| `reader_recv_timestamp_ns` | wall-clock מיד לאחר חזרת `ws.recv()` | reader | Unix ns | timestamp קיים | אינו זמן הגעת bytes ל־kernel |
| `processor_start_timestamp_ns` | wall-clock לפני book/readiness processing | processor | Unix ns | timestamp קיים | אחרי library queue ו־ingress queue |
| `processor_finish_timestamp_ns` | wall-clock בסיום `process_message` | processor | Unix ns | קריאת clock אחת | אינו כולל strategy worker async מאוחר יותר |
| `exchange_age_at_reader_ms` | max(0, reader receive wall time − exchange timestamp) | reader boundary | ms | subtraction + rolling append | כולל exchange, network ו־library queue; **אינו network latency** |
| `ingress_queue_wait_ms` | max(0, dequeue monotonic − enqueue monotonic) | ingress processor | ms | subtraction + rolling append | אינו כולל library queue |
| `market_processing_ms` | processor start עד finish | `process_message` | ms | subtraction + rolling append | כולל book/readiness/callback scheduling sync |
| `exchange_age_at_processing_ms` | max(0, processing wall time − exchange timestamp) | processor start | ms | subtraction + rolling append | end-to-processing age; אינו מייחס אשמה לרשת |

ערכים מקומיים של duration/age נחתכים ל־0 לצורכי telemetry. מדיניות FUTURE/STALE של order-book לא שונתה.

### 4. Stale לפי זמן מאז generation

לכל הודעה עם generation timing מחושב bucket לפי seconds מאז התחלת החיבור:

- `0_1s`
- `1_5s`
- `5_15s`
- `15_30s`
- `30_60s`
- `gt_60s`

Counters:

- `messages_total_by_reconnect_age_bucket`
- `stale_total_by_reconnect_age_bucket`

ה־stale counter גדל רק כאשר `OrderBookSet.apply` מחזיר בדיוק `STALE_EXCHANGE_TIMESTAMP`. אין label לפי market/condition/asset.

### 5. Reconnect / connection lifecycle

| Metric | Definition |
|---|---|
| `connection_attempts_total` | כל כניסה לניסיון connector, כולל ניסיון ראשון |
| `reconnect_attempts_total` | כל connector attempt אחרי הניסיון הראשון; אינו disconnect counter |
| `successful_connections_total` | כניסה מוצלחת ל־WebSocket context |
| `connection_generations_total` | generation חדש שנוצר לאחר connect מוצלח |
| `disconnects_total` | סיום שנספר רק לאחר שהיה connect מוצלח |
| `connection_failures_total` | ניסיון connection שנכשל לפני connection מוצלח |
| `connection_lifetime_seconds.current/p50/p95/p99/max` | זמן generation מוצלח עד disconnect |
| `heartbeat_timeout_disconnects` | רק `ConnectionClosed/ConnectionClosedError` עם evidence מפורש של `ping timeout` בטקסט |

ה־counter הישן `status.reconnect_attempts` נשמר ללא שינוי לצורכי backward compatibility; המדדים החדשים הם המקור להפרדה הסמנטית.

### 6. Boundedness וחשיפה

- כל rolling ring מוגבל ל־4,096 samples.
- disconnect history מוגבל ל־100 records; STATUS חושף 20 אחרונים.
- reason/bucket dictionaries הם fixed-cardinality.
- percentiles מחושבים רק בקריאת STATUS, לא לכל frame.
- אין DB write חדש לכל frame.
- אין log חדש לכל frame.
- אין JSON serialization חדש לכל frame לצורך telemetry.
- אין hashing חדש.
- אין metric labels לפי IDs.

## B. Disconnect Classification

כל disconnect record כולל:

- `generation`
- `disconnect_timestamp`
- `exception_class`
- `exception_text` (מוגבל ל־500 chars)
- `local_close_initiated`
- `local_close_reason`
- `local_close_timestamp`
- `close_code_received`
- `close_reason_received`
- `close_code_sent` כאשר נגיש דרך state ציבורי של exception
- `last_ping_timestamp`
- `last_pong_timestamp`
- `connection_lifetime_seconds`
- lag/queue snapshot לצורך correlation

Local close categories:

| Category | Existing close path tagged |
|---|---|
| `LOCAL_CLOSE_BEST_PRICE_MISMATCH` | terminal `BEST_PRICE_MISMATCH` resync |
| `LOCAL_CLOSE_INGRESS_SATURATION` | ingress queue saturation |
| `LOCAL_CLOSE_SHUTDOWN` | `MarketWebSocketManager.stop()` |
| `LOCAL_CLOSE_RECONNECT_POLICY` | dynamic subscribe timeout, non-best integrity resync, pipeline-controlled reconnect close |
| `LOCAL_CLOSE_OTHER` | bounded fallback עבור reason לא מוכר |

Remote/network exceptions אינם מקבלים local reason. `no close frame` אינו מסווג אוטומטית כ־heartbeat timeout. קוד 1013 אינו מסווג אוטומטית כתקלה של Polymarket.

### Ping/Pong configuration בפועל

- `ping_interval=None` ב־`websockets.connect`.
- `ping_timeout` אינו מוגדר מפורשות; value ציבורי מה־connection נחשף ב־STATUS. כאשר `ping_interval=None`, keepalive האוטומטי של הספרייה אינו פעיל.
- `close_timeout=5` seconds.
- application heartbeat שולח string `PING` כל 10 seconds ומעדכן `last_ping_at`; `PONG` מעדכן `last_pong_at`.
- `max_queue=(256, 64)` כברירת מחדל בפועל, ללא שינוי.
- לא שונה אף ערך.

## C. Existing vs New Metrics

ערכי historical קיימים נלקחו רק מ־`HOT_PATH_DIAGNOSTIC_REPORT_HE.md`. אין reconstruction או baseline משוער.

| Metric | Existing Before Patch? | Baseline Available? |
|---|---:|---|
| `messages_received` | YES | 18,108,972 ב־snapshot המתועד |
| `rejection_reasons.STALE_EXCHANGE_TIMESTAMP` | YES | 13,810,002 ב־snapshot המתועד |
| `best_price_mismatches` / resync | YES | 82 cumulative ב־snapshot המתועד |
| legacy `reconnect_attempts` | YES | 258; הדוח מזהיר שזה אינו בהכרח disconnect count |
| `max_ingress_queue_depth` | YES | 16 ב־snapshot המתועד |
| strategy frames coalesced | YES | 3,134,591 ב־snapshot המתועד |
| diagnostic `ws_internal_queue_depth` | YES, opt-in/internal | p95 345, max 437 בהיסטוריה; לא baseline לשמות החדשים |
| `ws_library_queue_depth_current/high_watermark/p50/p95/p99` | NO | **NO BASELINE — NEW METRIC** |
| `consecutive_recv_drain_count` | NO | **NO BASELINE — NEW METRIC** |
| `max_consecutive_immediate_recv` | NO | **NO BASELINE — NEW METRIC** |
| `recv_to_recv_gap_ms` | NO | **NO BASELINE — NEW METRIC** |
| diagnostic `event_loop_lag_ms` | YES, opt-in | historical p50 14.6ms, p95 198.8ms, p99 726.2ms, max 76,296ms; sampling שונה |
| always-on rolling `event_loop_lag_ms` + buckets | NO | **NO BASELINE — NEW METRIC** |
| `exchange_age_at_reader_ms` | NO (diagnostic predecessor existed) | **NO BASELINE — NEW METRIC** |
| `ingress_queue_wait_ms` rolling STATUS | NO (diagnostic predecessor existed) | **NO BASELINE — NEW METRIC** |
| `market_processing_ms` rolling STATUS | NO (single current/max existed) | **NO BASELINE — NEW METRIC** |
| `exchange_age_at_processing_ms` rolling STATUS | NO | **NO BASELINE — NEW METRIC** |
| reconnect-age message/stale counters | NO | **NO BASELINE — NEW METRIC** |
| `disconnects_total` | NO | **NO BASELINE — NEW METRIC** |
| `connection_attempts_total` | NO | **NO BASELINE — NEW METRIC** |
| `reconnect_attempts_total` (new semantic counter) | NO | **NO BASELINE — NEW METRIC** |
| `successful_connections_total` | NO | **NO BASELINE — NEW METRIC** |
| `connection_generations_total` | NO | **NO BASELINE — NEW METRIC** |
| `connection_lifetime_seconds` | NO | **NO BASELINE — NEW METRIC** |
| disconnect classification/history | NO | **NO BASELINE — NEW METRIC** |
| `heartbeat_timeout_disconnects` | NO | **NO BASELINE — NEW METRIC** |

## D. Files Changed

קבצים שנגעו בהם במסגרת Run A:

1. `polymarket-collector/live/market_websocket.py` — instrumentation, rolling metrics, lifecycle counters, close tagging, health exposure.
2. `polymarket-collector/tests/test_market_ws_telemetry.py` — focused tests חדשים.
3. `HOT_PATH_TELEMETRY_IMPLEMENTATION_REPORT_HE.md` — דוח זה.

`market_websocket.py` היה modified לפני תחילת המשימה. נשמרו השינויים הקיימים, ולכן cumulative diff מול HEAD כולל גם עבודה קודמת שאינה שייכת ל־Run A. ה־worktree כולו היה dirty מראש עם קבצים רבים נוספים; הם לא נערכו במסגרת המשימה.

FILES TOUCHED OUTSIDE EXPECTED/ALLOWED LIST:  
NONE

לא נוצר commit: worktree dirty מראש ובו overlap ב־`market_websocket.py`, ולכן commit ממוקד ובטוח אינו מוצדק ללא review אנושי.

## E. Tests

| Test / command | Result | What it proves |
|---|---:|---|
| `tests/test_market_ws_telemetry.py` כחלק מהרצת focused | PASS | local/remote disconnect classification, reconnect buckets, deterministic lag buckets, timing boundaries, non-negative queue wait, ordering/semantics unchanged |
| `tests/test_market_ws_telemetry.py tests/test_market_data_safety.py` | 32 PASS | telemetry + market freshness, ingress, resync, persistence safety |
| focused + existing watchdog | 33 PASS | watchdog עדיין מסוגל stack capture כאשר heavy diagnostics מבוקש |
| `tests/test_live_full_strategy.py` | 69 PASS | order-book, readiness, entry/exit ו־strategy regressions |
| `py_compile` | PASS | syntax תקין |
| `git diff --check` | PASS | אין whitespace errors |

הערת infrastructure: `test_fifty_dashboard_refreshes_do_not_stall_trader_event_loop` נכשל פעמיים בתוך suite עקב lag של 142.7–151.5ms מול threshold של 100ms. הבדיקה אינה יוצרת `MarketWebSocketManager` ואינה עוברת בקוד ששונה. היא עברה בהרצה מבודדת: 1 PASS ב־0.55s. הכשל מתועד ולא תוקן במסגרת scope זה.

### Performance Safety

ה־synthetic safety test הקיים `test_slow_sqlite_writer_does_not_delay_memory_book_or_grow_unbounded` עבר כחלק מ־`test_market_data_safety.py`; הוא מעבד 1,000 messages ומאמת guard של פחות מ־0.5s לצד bounded persistence queue.

TELEMETRY OVERHEAD: **NOT MEASURED**

לא קיים before/after benchmark מבודד אמין עבור patch זה, ולכן לא מוצג אחוז overhead. ה־synthetic regression עבר, אך אינו משמש כהערכת overhead אינקרמנטלי.

## F. Git Diff

### `git diff --stat` של ה־worktree המלא

הפלט כולל עבודה שהייתה קיימת לפני Run A:

```text
 polymarket-collector/deploy/live.env.example       |   4 +
 polymarket-collector/live/config.py                |  16 +
 polymarket-collector/live/dashboard_read_model.py  |  27 +-
 polymarket-collector/live/market_websocket.py      | 489 +++++++++++-
 polymarket-collector/live/pause_recovery.py        | 761 +++++++++++++++---
 polymarket-collector/live/reconciliation.py        | 205 ++++-
 polymarket-collector/live/repository.py            |  48 +-
 polymarket-collector/live/router.py                |  37 +-
 polymarket-collector/live/strategy_repository.py   | 872 +++++++++++++++++++--
 polymarket-collector/live/strategy_runtime.py      | 124 ++-
 polymarket-collector/live/trader_commands.py       |  34 +-
 .../tests/test_architecture_isolation.py           |  39 +-
 .../tests/test_live_full_strategy.py               | 154 ++++
 .../tests/test_market_data_safety.py               |  26 +
 polymarket-collector/tests/test_pause_recovery.py  | 680 +++++++++++++---
 polymarket-collector/trader_app.py                 |  86 +-
 16 files changed, 3209 insertions(+), 393 deletions(-)
```

הקבצים החדשים untracked (`test_market_ws_telemetry.py` ודוח זה) אינם נכללים אוטומטית ב־`git diff --stat`.

### Scoped cumulative diff מול HEAD

```text
 polymarket-collector/live/market_websocket.py | 489 ++++++++++++++++++++++++--
 1 file changed, 457 insertions(+), 32 deletions(-)
```

גם נתון scoped זה כולל שינויים pre-existing באותו קובץ. סיכום Run A בלבד: rolling telemetry חסום, backlog/proxy metrics, always-on lightweight lag watchdog, stage timing, reconnect-age stale counters, connection/disconnect separation, local close tags, disconnect snapshots ו־focused tests.

## G. Behavior Changes

MARKET WS LIFECYCLE CHANGED: NO  
RECONCILIATION BEHAVIOR CHANGED: NO  
ORDER BOOK SEMANTICS CHANGED: NO  
ENTRY/EXIT LOGIC CHANGED: NO  
TRADING CONFIG CHANGED: NO

פרטים:

- `await self.on_reconnect()` נשאר לפני `_run_ingress_pipeline(ws)`.
- אותם callsites יוזמים close; רק נוספה עטיפה שמתעדת reason לפני אותו `await ws.close()`.
- queue capacities, freshness/future thresholds, BEST_PRICE_MISMATCH policy ו־reconnect backoff לא שונו.
- watchdog lightweight נוסף כ־telemetry task; stack capture ו־diagnostic file נשארו opt-in.
- health payload הורחב read-only.

## H. Deployment

LIVE DEPLOYMENT: NOT PERFORMED  
SERVICE RESTART: NOT PERFORMED  
LIVE CONFIG CHANGES: NONE

בנוסף:

- systemd לא הופעל.
- לא בוצע deploy.
- לא בוצע restart.
- לא בוצעה כתיבה ל־LIVE DB.
- לא הופעל feature flag.
- לא הוחלף קובץ runtime.
- לא בוצעה פעולה מסחרית.

## I. Final Verdict

IMPLEMENTATION: PASS  
TESTS: PASS  
SCOPE: CLEAN  
READY FOR REVIEW: YES

ה־PASS מתייחס לבדיקות הממוקדות והרלוונטיות. ה־dashboard multiprocessing timing failure הסביבתי מתועד במלואו לעיל ועבר standalone.

READY FOR REVIEW  
Telemetry patch is implemented and tested.  
No LIVE deployment, restart, configuration change, or trading behavior change was performed.
