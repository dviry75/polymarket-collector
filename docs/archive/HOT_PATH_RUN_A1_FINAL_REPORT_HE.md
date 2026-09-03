# Run A.1 FINAL — Safe Snapshot, Runtime Provenance ו־Baseline Telemetry

## A. Executive Summary

WIP PRESERVATION SNAPSHOT CREATED: YES

WIP SNAPSHOT SHA: `0a6a780c6203311213c8c8cc402b93ed8c07ac90`

WIP SNAPSHOT TAG: `wip-snapshot-2026-08-21`

WIP SNAPSHOT PUSHED: NO

SECRETS FOUND IN SNAPSHOT: NO

GITIGNORE COVERS SECRETS/DB/RUNTIME ARTIFACTS: PARTIAL

LIVE IS RUNNING UNCOMMITTED CODE: YES

CLEAN RUN A TELEMETRY PATCH: PARTIAL

CLEAN TELEMETRY COMMIT: `0ac131de8d9f38acf786ac670587838b95c78789`

READY FOR LIVE DEPLOYMENT: NO

**IMPORTANT HEARTBEAT FINDING: APPLICATION HEARTBEAT OBSERVES PONG BUT DOES NOT ENFORCE LIVENESS.**

ה־preservation anchor הוא צילום WIP בלתי־הרסני, לא `KNOWN-GOOD LIVE BASELINE`. אין ולא נוצר ref של `PRE_RUN_A_HEAD`. השירות החי כנראה טען חלק ניכר מעבודת ה־WIP שהייתה uncommitted בזמן ההפעלה, אך לא טען את Run A/A.1 שנכתב לאחר תחילת התהליך. לכן חל CASE A והבחירה בין baseline של ה־WIP לבין baseline committed נשארת להחלטה אנושית.

## B. Runtime

| Field | Value |
|---|---|
| SERVICE | `polymarket-trader.service` — active/running |
| PID | `1209339` |
| PROCESS START TIME | `2026-08-20 17:51:13 UTC` |
| SYSTEMD EXEC START TIME | `2026-08-20 17:51:13 UTC` |
| WORKING DIRECTORY | `/opt/polymarket-btc-live/repo/polymarket-collector` |
| PYTHON EXECUTABLE | `/opt/polymarket-btc-live/.venv/bin/python` (process executable resolves to `/usr/bin/python3.12`) |
| WEBSOCKETS VERSION | `15.0.1` |
| HEAD BEFORE SNAPSHOT | `e1717577315ecad201ee2beea311fe12e8d58743` |
| WIP SNAPSHOT SHA | `0a6a780c6203311213c8c8cc402b93ed8c07ac90` |

ה־unit מריץ את ה־checkout ישירות באמצעות venv `uvicorn`, עם `WorkingDirectory` לעיל. קובץ ה־EnvironmentFile לא נקרא במסגרת הבדיקה.

## C. Runtime Provenance

`mtime` הוא evidence בלבד ואינו הוכחה מוחלטת. המונח “loaded” בטבלה מתייחס לגרסה שהייתה על הדיסק בעת תחילת התהליך; גרסאות Run A/A.1 המאוחרות אינן נחשבות טעונות.

| File | Change Size מול original HEAD | Likely Running in Current Process? | Evidence | Confidence |
|---|---:|---|---|---|
| `live/market_websocket.py` | +457/−32 ב־snapshot; עוד +115/−6 ב־A.1 | UNLIKELY LOADED (Run A/A.1); pre-Run-A WIP possibly loaded | process התחיל 20/8 17:51; mtime של Run A/A.1 הוא 21/8; STATUS החי אינו מכיל `hot_path_telemetry`; journal משתמש בפורמט disconnect הישן | HIGH |
| `live/reconciliation.py` | +178/−27 | LIKELY LOADED BY CURRENT PROCESS (pre-A.1 revision) | mtime 20/8 15:15 לפני process start; imported during service construction; A.1 mtime 21/8 אינו טעון | HIGH |
| `live/strategy_runtime.py` | +109/−15 | LIKELY LOADED BY CURRENT PROCESS | mtime 20/8 14:16 לפני start; service constructs strategy runtime at startup | HIGH |
| `live/strategy_repository.py` | +809/−63 | LIKELY LOADED BY CURRENT PROCESS | mtime 20/8 16:57 לפני start; imported/constructed by runtime | HIGH |
| `live/pause_recovery.py` | +670/−91 | LIKELY LOADED BY CURRENT PROCESS | mtime 20/8 17:30 לפני start; STATUS/recovery path imports it | MEDIUM-HIGH |
| `trader_app.py` | +66/−20 | LIKELY LOADED BY CURRENT PROCESS | mtime 20/8 16:59 לפני start; זהו module ה־uvicorn הראשי | HIGH |

קביעה מרכזית: **LIVE IS RUNNING UNCOMMITTED CODE: YES.** הסיבה היא שהשירות התחיל אחרי mtimes של כמה קובצי WIP מרכזיים ומריץ את אותו checkout. במקביל, ראיות runtime מראות ש־Run A עצמו לא נטען. אין שימוש ב־`PROVEN` על בסיס mtime בלבד.

## D. Deployment Branch Analysis

**CASE A — LIVE LIKELY RUNS UNCOMMITTED WIP**

ISOLATION MAY BE UNNECESSARY FOR DEPLOYMENT — CURRENT LIVE MAY ALREADY INCLUDE PRE-EXISTING UNCOMMITTED WORK.

OPTION A: Accept reviewed WIP snapshot as explicit baseline, then layer telemetry on top.

- יתרון: מתאים ככל הנראה לקוד ה־WIP שכבר פעיל ומונע הסרה מקרית של תיקונים פעילים.
- סיכון: ה־snapshot גדול (`6,690` additions, `393` deletions), אינו known-good baseline ודורש review אנושי מפורש.

OPTION B: Deploy telemetry from committed baseline only, deliberately excluding WIP currently likely running.

- יתרון: יחידת שינוי עקרונית קטנה יותר מול היסטוריה committed.
- סיכון: עלול להסיר בכוונה קוד WIP שככל הנראה פעיל; Run A אינו מבודד במלואו אוטומטית בגלל mixed hunks, ולכן נדרשת reconstruction/review מודעת.

לא נבחרה אפשרות במסגרת משימה זו.

## E. Semantic Run A Classification

הסיווג בוצע על `git diff -U10 e171757... 0a6a780... -- polymarket-collector/live/market_websocket.py`, מול checklist של Section A בדוח Run A. זהו סיווג סמנטי בלבד; אין ref היסטורי “אחרי WIP ולפני Run A”.

| Hunk / Function | Classification | Evidence |
|---|---|---|
| 01 constants + `_RollingMetric` | TELEMETRY — RUN A | rings, buckets ו־local-close categories בלבד |
| 02 constructor metrics + diagnostics flag | MIXED / AMBIGUOUS | counters של Run A יחד עם diagnostics opt-in שהיה WIP קודם |
| 03 `start`/`stop` watchdog and close | MIXED / AMBIGUOUS | always-on lightweight watchdog/local-close tagging יחד עם gating של diagnostics |
| 04 `run` attempt/connect counters | TELEMETRY — RUN A | הפרדת attempt/success/generation ומצב classification בלבד |
| 05 `run` exception handling | TELEMETRY — RUN A | evidence ו־count semantics ל־disconnect בלבד |
| 06 smoke/local reconnect close | TELEMETRY — RUN A | החלפת close ישיר ב־wrapper עם אותו timing והחלטה |
| 07 reader receive observations | TELEMETRY — RUN A | queue depth, recv gap ו־drain proxy |
| 08 ingress saturation close label | TELEMETRY — RUN A | תיוג close קיים בלבד |
| 09 processor queue wait/resync reason | TELEMETRY — RUN A | clamp למדידה ותיוג סיבה; החלטת resync לא השתנתה |
| 10 subscription reconnect close label | TELEMETRY — RUN A | תיוג close קיים בלבד |
| 11 smoke `connected` evidence | TELEMETRY — RUN A | מונע ספירת disconnect ללא connection |
| 12 process timing + diagnostics gating | MIXED / AMBIGUOUS | stage timing של Run A יחד עם opt-in diagnostic WIP |
| 13 book history gating | PRE-EXISTING | שינוי overhead diagnostics, לא metric Run A |
| 14 processing/reconnect-age timing | TELEMETRY — RUN A | finish timestamp, duration ו־stale bucket |
| 15 telemetry helpers/snapshot | TELEMETRY — RUN A | כל metric families ו־disconnect schema |
| 16 event-loop watchdog | MIXED / AMBIGUOUS | metric recording יחד עם שינוי cadence/stack-capture של diagnostics |
| 17 `mark_disconnect` | TELEMETRY — RUN A | classification, lifetime ו־disconnect snapshot |
| 18 health diagnostics fields | PRE-EXISTING | exposure של diagnostic opt-in קודם |
| 19 `hot_path_telemetry` health | TELEMETRY — RUN A | read-only exposure של Run A |
| 20 User WS last-message state | PRE-EXISTING | אינו Market WS telemetry |

Totals: TELEMETRY — RUN A: **13**; PRE-EXISTING: **3**; MIXED / AMBIGUOUS: **4**.

## F. Run A Patch

RUN_A_TELEMETRY_ONLY.patch: **PARTIAL**

PATCH CREATED FOR REVIEW; MAY NOT BE THE CORRECT DEPLOYMENT UNIT IF LIVE ALREADY RUNS THE WIP STATE.

FILES INCLUDED:

- `polymarket-collector/live/market_websocket.py`
- `polymarket-collector/live/reconciliation.py`
- `polymarket-collector/live/trader_commands.py`
- `polymarket-collector/tests/test_market_ws_telemetry.py`

ה־patch הוא diff תקני מ־preservation anchor `0a6a780...` אל clean A.1 commit `0ac131d...`; reverse-check מול ה־tree הנוכחי עבר. הוא מכיל את שכבת A.1 הנקייה בלבד.

AMBIGUOUS HUNKS EXCLUDED: ארבעת mixed hunks של Run A core (02, 03, 12, 16), וכן כל pre-existing hunk.

LIMITATIONS: אין ב־patch את מלוא Run A core, שכבר נמצא ב־parent WIP snapshot. לכן הוא clean A.1 review unit, אך **אינו standalone Run A deployment patch מול original HEAD**.

## G. Added Baseline Metrics

### Reconciliation

`reconciliation_duration_ms`:

- IMPLEMENTED: YES
- measurement point: בתוך ה־lock, מיד לפני ואחרי `_run_once_serialized`; backoff short-circuit אינו reconciliation run ולכן אינו נספר.
- SUCCESS/FAILURE SPLIT: `success` רק ל־status `ok`; `failure` ל־`gaps`, `failed` או חריגה propagating.
- ROLLING STATS: `count`, `current`, `p50`, `p95`, `p99`, `max`; ring של 1,024 לכל outcome. Sorting מתרחש רק בקריאת STATUS.
- timestamps: `reconciliation_started_at`, `reconciliation_finished_at`.
- exposure: `TraderCommandHandler STATUS["reconciliation"]`.
- behavior: ה־result והחריגה המקוריים נשמרים; אין retry/lock/DB/network/readiness change.
- BASELINE: **NO BASELINE — NEW METRIC**

### Market generation warmup

נוספו השמות המדויקים:

- `generation_connected_at`
- `subscription_sent_at`
- `reconciliation_started_at`
- `reconciliation_finished_at`
- `generation_to_first_book_slot_1_ms`
- `generation_to_first_book_slot_2_ms`
- `generation_to_first_required_books_ms`

כל duration metric מכיל `current/p50/p95/p99/max/sample_count` על ring של 4,096. ה־slots נקבעים מסדר רשימת ה־required assets בתחילת generation ואינם משתמשים ב־asset IDs כ־labels. state מתאפס בכל connection generation; frame עם generation ישן אינו יכול לעדכן את הדור החדש. “first required books” הוא זמן עד שנצפה book ראשון לכל required asset המקורי, לא readiness חדשה ולא הבטחת freshness.

GENERATION_TO_MARKET_DATA_READY: **NOT MEASURABLE WITH CURRENT SEMANTICS**. הערך נחשף כך במפורש; לא נוצרה semantic readiness חדשה.

BASELINE לכל המדדים לעיל: **NO BASELINE — NEW METRIC**.

### Keepalive metric clarification

`heartbeat_timeout_disconnects` שונה, לפני deployment ראשון וללא consumer runtime קיים, ל־`websockets_keepalive_timeout_disconnects`. STATUS מציין `automatic_websockets_keepalive_enabled=false` ואת העובדה ש־`ping_interval=None`; counter זה צפוי להישאר אפס ואינו evidence לבריאות heartbeat אפליקטיבי.

## H. Heartbeat Audit

| Item | Result |
|---|---|
| AUTOMATIC WEBSOCKETS KEEPALIVE | DISABLED — `ping_interval=None`; `ping_timeout` לא מוגדר; `close_timeout=5` |
| APPLICATION PING INTERVAL | 10 seconds |
| APPLICATION PING SENDER | `MarketWebSocketManager._heartbeat`, task שנוצר לאחר reconciliation |
| APPLICATION PONG TRACKING | YES — reader מזהה raw `PONG` ומעדכן `last_pong_at` |
| LAST_PONG_TRACKED | YES |
| PONG AGE AVAILABLE | NO — timestamp חשוף, אין metric age/threshold |
| PONG TIMEOUT | NO |
| MISSING PONG ACTION | no action בגלל PONG חסר בלבד; silence כללי ב־`ws.recv()` כן גורר timeout אחרי `max(15, stale_after_seconds)` ואז reconnect |
| HALF-OPEN RISK | YES — אם data ממשיך להגיע אך PONG חסר, אין enforcement וניתן להישאר connected |
| HEARTBEAT LOOP OWNERSHIP | אותו main event loop |

APPLICATION PING SENDER: `MarketWebSocketManager._heartbeat`

PONG DETECTION: equality ל־`"PONG"` או `b"PONG"` ב־reader

IF PONG IS MISSING: אין reconnect, אין NOT_READY ואין warning שמבוססים על PONG age; רק receive silence הכללי עשוי להפעיל reconnect.

HALF-OPEN CONNECTION CAN REMAIN CONNECTED: YES

HEARTBEAT TASK SHARES MAIN EVENT LOOP: YES

לא בוצע heartbeat behavior fix.

## I. Queue Measurement Limitation

Queue-depth percentiles are only observed when recv() returns. They do not continuously observe backlog during an event-loop stall.

במהלך stall שבו ה־reader אינו מקבל CPU: **NO QUEUE DEPTH SAMPLES ARE TAKEN**. לכן p50/p95/p99 הם observed sample distribution בלבד ומהווים lower-bound view של backlog. יש לקרוא אותם יחד עם `event_loop_lag_ms`, `ws_library_queue_depth_high_watermark`, `max_consecutive_immediate_recv`, `recv_to_recv_gap_ms` ו־`exchange_age_at_reader_ms`.

הגישה היא `len(ws.recv_messages.frames)` ב־websockets 15.0.1. היא best-effort על מבנה ספרייה שאינו API ציבורי יציב; כשל גישה נספר ב־`ws_library_queue_depth_unavailable_samples`, ללא המצאת ערך.

## J. Benchmark

| Field | Result |
|---|---|
| METHOD | local synthetic `process_message` benchmark; temporary SQLite; recorder methods monkeypatched ל־no-op; ללא production flag וללא LIVE I/O |
| MESSAGES | 100,000 לכל trial; 4 trials / 400,000 processed בסך הכול |
| MIX | 60,000 `price_change`, 30,000 `best_bid_ask`, 10,000 `book` בכל trial |
| TELEMETRY ON | 17.283494s ו־17.977106s; average **17.630300s**, כ־**5,672 msg/s** |
| RECORDER NO-OP | 16.555899s ו־15.292647s; average **15.924273s**, כ־**6,279 msg/s** |
| RELATIVE DELTA | **+10.713% runtime**; כ־17.06µs/message absolute במבחן זה |
| RELIABILITY | MEDIUM — alternating order ושני trials לכל mode, אך fixtures סינתטיים והבדיקה אינה כוללת socket recv/JSON parse/ingress scheduling או production contention |

TELEMETRY OVERHEAD: **MEASURED — SYNTHETIC, MEDIUM RELIABILITY**. לא בוצע load test על LIVE.

## K. Dashboard Timing Test

| Field | Result |
|---|---|
| WS MANAGER PRESENT | NO |
| WATCHDOG PRESENT | NO |
| TELEMETRY INVOLVED | NO |
| CONCLUSION | test יוצר heartbeat מקומי משלו ו־`ProcessPoolExecutor`; אין import/instantiation של `MarketWebSocketManager` במסלול הבדיקה. בהרצה standalone הנוכחית נכשל עם max lag `130.06ms` מול threshold `100ms`. הסף לא שונה והכשל אינו מוסתר. |

## L. Tests

| Test | Result | Meaning |
|---|---|---|
| `tests/test_market_ws_telemetry.py` | PASS — 9/9 | disconnect classification, reconnect buckets, deterministic lag, stage timing, unchanged ordering, reconciliation success/failure/bounds, generation reset/old-generation isolation, keepalive naming |
| mandatory combined: telemetry + `test_market_data_safety.py` + `test_live_full_strategy.py` | PASS — 105/105 | Market WS/order-book/strategy/entry-exit regression coverage |
| reconciliation/status extra suites | PASS — 48/48; 1 dashboard test deselected because run separately | reconciliation stability/accounting/pause recovery/status compatibility |
| `test_fifty_dashboard_refreshes_do_not_stall_trader_event_loop` standalone | FAIL — 130.06ms ≥ 100ms | known intermittent infrastructure timing failure; no WS manager/watchdog/telemetry path |
| `py_compile` + `git diff --check` | PASS | syntax and whitespace validation |
| patch reverse apply check | PASS | patch matches post-A.1 tree and is reversible to preservation anchor |

לא הוסתר failure. תוצאת suite הכוללת של הבדיקות שעברו: 153 PASS; בדיקת timing אחת FAIL.

## M. Git / Security

| Field | Value |
|---|---|
| ORIGINAL HEAD | `e1717577315ecad201ee2beea311fe12e8d58743` |
| WIP BRANCH | `wip/pre-cleanup-snapshot-20260821` |
| WIP SNAPSHOT SHA | `0a6a780c6203311213c8c8cc402b93ed8c07ac90` |
| WIP SNAPSHOT TAG | `wip-snapshot-2026-08-21` |
| CLEAN TELEMETRY SHA | `0ac131de8d9f38acf786ac670587838b95c78789` |
| CURRENT STATUS | review report/patch tracked separately; רק pre-existing excluded artifacts נשארים untracked |

PRESERVATION ANCHOR: YES

WIP SNAPSHOT PUSHED: NO

WIP SNAPSHOT TAG PUSHED: NO

### Secret / artifact audit

כל untracked file שהיה קיים לפני staging נבדק לפי path/type/size/source/secret/safety. 18 קובצי source/test/report/system-context שנמצאו safe הוספו מפורשות; לא נעשה `git add -A`. staged snapshot נבדק שוב לפי שמות/גודל/patterns ולא נמצאו credentials, private keys, DB, WAL/SHM, logs או dumps.

| Paths | Type / Source | Possible Secret | Safe to Commit | Action |
|---|---|---|---|---|
| `AGENTS.md`, `SYSTEM_CONTEXT.md` | text / source guidance | NO | YES | individually committed |
| שישה דוחות `*.md` ב־root + collector report | text / reports | NO (hashes שנמצאו הם digests) | YES | individually committed |
| market-resolution service/script/source + reconciliation/recovery source | text / source | NO | YES | individually committed |
| ארבעת test files החדשים | text / tests | NO | YES | individually committed |
| `PRIVATE_KEY_SECURE_SETUP_GUIDE_HE.md` (34,393 bytes) | text / security guide | UNKNOWN | NO | excluded |
| `polymarket-collector/.live-backups/**` (31 source/test copies, 2,006–99,397 bytes) | generated/runtime backups | UNKNOWN | NO | excluded |
| `live/repository.py.bak...` (71,499 bytes) | generated backup | UNKNOWN | NO | excluded |
| `live/router.py.backup...` (83,179 bytes) | generated backup | UNKNOWN | NO | excluded |
| `live/strategy_runtime.py.bak...` (52,865 bytes) | generated backup | UNKNOWN | NO | excluded |

FILES/ARTIFACTS EXCLUDED DUE TO SECRET/RUNTIME RISK:

- `PRIVATE_KEY_SECURE_SETUP_GUIDE_HE.md`
- `polymarket-collector/.live-backups/`
- `polymarket-collector/live/repository.py.bak.20260807_152402`
- `polymarket-collector/live/router.py.backup-before-continuous-resume-20260808-205727`
- `polymarket-collector/live/strategy_runtime.py.bak.20260807_152949`

לא נמצא secret literal מאומת; הפריטים המסומנים UNKNOWN הוחרגו באופן שמרני.

### `.gitignore`

GITIGNORE COVERS SECRETS/DB/RUNTIME ARTIFACTS: **PARTIAL**

הקיים מכסה `.env`, `*.sqlite3`, `*.db`, `*.log`, venv ו־pycache. חסרים patterns מפורשים עבור `.env.*`, `*.sqlite3-wal`, `*.sqlite3-shm`, `*.db-wal`, `*.db-shm`, private-key formats (`*.pem`, `*.key`, `id_*`), `.live-backups/`, `*.bak*`, temporary dumps ו־runtime state כללי. `.gitignore` לא שונה: staging היה explicit והפריטים המסוכנים לא נכנסו ל־snapshot.

## N. Behavior Verification

MARKET WS LIFECYCLE CHANGED: NO

RECONCILIATION BEHAVIOR CHANGED: NO

ORDER BOOK SEMANTICS CHANGED: NO

ENTRY LOGIC CHANGED: NO

EXIT/STOP LOGIC CHANGED: NO

TRADING CONFIG CHANGED: NO

LIVE DATABASE CHANGED: NO

סדר ה־lifecycle נשאר: connect → subscribe → `await on_reconnect()` → reader/ingress pipeline. instrumentation של reconciliation משתמש ב־`try/finally` בלבד ושומר result/exception. לא שונו queue size, freshness, BEST_PRICE_MISMATCH, ping/pong behavior, readiness, strategy או execution.

## O. Next Decision

NEXT STEP: **BLOCKED — LIVE CODE STATE MUST BE CHOSEN EXPLICITLY**

ה־blocker היחיד הוא בחירה אנושית בין Option A (אישור WIP snapshot כ־baseline מפורש והנחת clean A.1 commit מעליו) לבין Option B (שחזור telemetry מעל committed baseline תוך exclusion מכוון של WIP שככל הנראה פעיל). אין צורך ב־audit loop נוסף.

## Deployment

LIVE DEPLOYMENT: NOT PERFORMED

SERVICE RESTART: NOT PERFORMED

LIVE CONFIG CHANGE: NONE

SERVER UPGRADE: NOT PERFORMED
