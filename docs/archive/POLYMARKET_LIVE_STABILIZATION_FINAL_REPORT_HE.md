# דוח ייצוב סופי — Polymarket LIVE

תאריך: 2026-08-25 UTC
סטטוס כולל: **PARTIAL** — כל מנגנוני היציבות והניטור נפרסו והמסחר פעיל; שליחת דוא״ל חיצוני נשארה חסומה עד הרשאה מפורשת ליעד ולתוכן.

# Executive Summary

- CURRENT TRADING STATE: `TRADING`
- SERVICE: `polymarket-trader.service` — `active/running`, PID `1744645`,‏ `NRestarts=0`
- DEPLOYED SHA: `47f08d3e70d464644a6313f9ec6559d72c220f11`
- DASHBOARD: `active/running`, PID `1744850`,‏ `NRestarts=0`
- 24H SOAK: `active/running`, PID `1744753`,‏ `NRestarts=0`
- מצב פיננסי בזמן המסירה: exposure מרוחק `0`, פקודות פתוחות `0`, intents לא־סופיים `0`, quarantine פתוח `0`.
- לא בוצעו כתיבות גולמיות למסד, לא שונו secrets/thresholds/trade size/daily loss cap, ולא בוצע push.

# Initial Root Causes

1. ביטול coroutine יכול היה להשאיר reconciliation במצב `running` ולשבש את cadence.
2. כמה triggers יכלו להתלכד בצורה לא מסודרת או להמתין לאותה משימה בלי בעלות lifecycle ברורה.
3. backoff הושפע ממצב היסטורי במקום מ־fingerprint של הראיה הפעילה.
4. propagation lag לאחר `matched` יכול היה להיראות כ־zero-fill או contradiction מוקדם מדי.
5. remote absence טופל בעבר כראיה חזקה מדי בשוק פתור.
6. contradiction ממוקד ותקלה account-wide חלקו יותר מדי semantics של pause גלובלי.
7. repair של EXIT סמכותי לא היה שלם ואידמפוטנטי לכל linkage/fill/dust.
8. UNKNOWN ותשובות API זמניות יכלו להפוך ל־MANUAL_ONLY מתמשך ללא classification מאוחר.
9. alerts היסטוריים לא ניהלו lifecycle מלא, וה־watchdog הקריטי לא היה מיידי.
10. ה־dashboard וה־soak לא הציגו את כל חוזה recovery/incident הנדרש.

## D1–D10

| דרישה | יושמה | בדיקות עיקריות | commit |
|---|---:|---|---|
| D1 — cancellation terminal | כן | `test_reconciliation_cancellation.py` | `f81bf30` |
| D2 — single-flight/coalescing | כן | `test_reconciliation_coordinator.py` | `3318a81` |
| D3 — cadence ללא stuck loop | כן | coordinator/stuck cadence tests | `3318a81` |
| D4 — bounded evidence backoff | כן | gap fingerprint/backoff tests | `3318a81` |
| D5 — ENTRY/EXIT propagation | כן | delayed fill, restart, partial, duplicate prevention | `f81bf30`, `726c9b0` |
| D6 — resolved market truth | כן | winner/loser/balance/grace fail-closed tests | `f81bf30` |
| D7 — scoped quarantine | כן | `test_scoped_quarantine_repair.py` | `ba044f5` |
| D8 — authoritative repair | כן | repair arithmetic, idempotence, unsafe evidence | `ba044f5`, `794d291` |
| D9 — alerts/observability | חלקי רק בשל sender חיצוני | lifecycle, watchdogs, dashboard, soak smoke | `b5207b0`, `fed5929`, `038c3a0`, `47f08d3` |
| D10 — recovery/UNKNOWN | כן | `test_pause_recovery.py` | `ba044f5`, `726c9b0`, `3c30e3d` |

# Architecture

## Transient Global Block

אירוע זמני חוסם entries באופן fail-closed, מפעיל classification/reconciliation, וממתין לחלון יציבות. כאשר כל הראיות נקיות, השחרור מתבצע אוטומטית וב־CAS על generation. יציאות, stop, accounting ו־reconciliation ממשיכים לפעול בזמן חסימת entries.

## Scoped Quarantine

POSITION/TOKEN/EVENT מוכח מבודד רק את האובייקט. האובייקט נשאר גלוי למסד, risk, accounting, dashboard ו־reconciliation. אירועים בטוחים אחרים אינם נחסמים. quarantine אינו מוחק state ואינו נהפך אוטומטית לעצירה גלובלית.

## Global Manual Hard Stop

Kill switch, מגבלת הפסד יומי, כשל זהות/הרשאות, אמת חשבון לא זמינה ו־contradiction account/global נשארים `MANUAL_ONLY`. גם reconciliation נקי יחיד אינו משחרר אותם.

## Unknown Classification

UNKNOWN מתחיל ב־temporary global block. המערכת מנסה לסווג ולהשיג אמת סמכותית. הוכחת scope מקומי מעבירה ל־quarantine; UNKNOWN מתמשך מסלים ל־operator action ול־global manual halt.

# D1 — Cancellation

התנהגות ישנה: ביטול באמצע fetch/processing היה עלול להשאיר run פתוח או להיבלע כשגיאה כללית.
התנהגות חדשה: `CancelledError` נרשם כתוצאה טרמינלית, cleanup מתבצע, והביטול נזרק מחדש. shutdown הוא הגורם היחיד שמבטל את ה־worker; ביטול waiter אינו מבטל את הריצה המשותפת.

# D5 — ENTRY/EXIT Propagation Semantics

- `matched` עם fill שעדיין לא הופיע נשאר propagation-pending בתוך grace; אינו מסומן ZERO_FILL מוקדם.
- reconciliation יכול לקשור delayed trade/position לאותו intent ו־remote order.
- EXIT propagation-pending חוסם EXIT כפול ושורד restart.
- partial fill משתמש רק בכמות שנצפתה; cancelled/rejected נשארים terminal.
- מעבר ל־grace ללא הסבר נשאר fail-closed.

# D6 — Resolved Market Handling

- loser עם balance סמכותי אפס נסגר כ־`RESOLVED_LOSER`.
- winner עם balance חיובי עובר `REDEEM_PENDING`.
- public absence לבדה אינה סוגרת position; grace ותצפית balance נדרשים.
- winner עם balance אפס או סתירה לא מוסברת נשאר fail-closed.

# Reconciliation Coordinator

Coordinator יחיד מחזיק לכל היותר ריצה אחת (`max_concurrency=1`). triggers בזמן ריצה מתאחדים ל־follow-up יחיד; waiter cancellation אינו מבטל את הריצה; shutdown כן. תוצאה מדור קודם אינה מתפרסמת כמצב עדכני.

בעלייה נוקו פעם אחת 14,429 runs יתומים ל־`failed/ORPHANED_PREVIOUS_PROCESS`. לאחר מכן מספר הריצות `running` התקועות מעל חמש דקות הוא 0.

# Backoff

ה־backoff מחושב לפי fingerprint של gaps פעילים ולא לפי position היסטורי. הרצף חסום (`3, 3, 5, 10, 15, 30, 60` שניות); ראיה חדשה מאפסת את הרצף, gap לא קשור אינו מזהם אותו, וריצה נקייה מאפסת אותו.

# Quarantine

נשארים גלויים: position, token/event linkage, remaining/sellable shares, audit, alerts וראיות authoritative.
מפסיק לחסום: entries של אירועים אחרים כאשר ה־scope מוכח כמקומי.
Risk: exposure לא נעלם מהחשבונאות; unsafe evidence משאיר quarantine או global fail-closed לפי scope.

# Remote Truth

Remote presence היא ראיה חזקה. Remote absence היא ראיה חלשה ונבדקת שוב אחרי grace. מסלול ה־repair דורש order `matched`, attempt linkage, balance סמכותי, לפחות שלוש תצפיות וחלון של כ־15 שניות. אין תיקון על סמך public API absence בלבד.

# Unknown Policy

`UNKNOWN → TRANSIENT_GLOBAL_BLOCK → CLASSIFY/RECONCILE`
`→ proven scoped → QUARANTINE_AND_VERIFY`
`→ proven clean → stability window → AUTO_RESUME`
`→ still unknown/account/global → MANUAL_ONLY + OPERATOR_ACTION_REQUIRED`.

# Operator vs Global Halt

מקור האמת נשאר `live_system_state` דרך שכבת repository:

- `operator_action_required` / `operator_action_reason`
- `global_entry_halt_required` / `global_entry_halt_reason`
- `incident_scope`
- `quarantined_positions_count` / `quarantine_last_at`
- `auto_repair_last_at` / `auto_repair_count_24h`

פעולת operator אינה שקולה אוטומטית ל־global halt: quarantine עשוי לדרוש review בלי לחסום את כל החשבון.

# Alerts

- lifecycle: `OPEN`, `RESOLVED`, `ACKNOWLEDGED`, occurrence, recurrence ו־dedup.
- 56 alerts היסטוריים נורמלו ונפתרו בלי למחוק audit.
- `operator_action_required=true` פותח מיד `[CRITICAL ACTION]` ו־email outbox; אין עוד המתנה של חמש דקות.
- reconciliation שאינו נקי מעל חמש דקות פותח watchdog עם dedup ונסגר עם הצלחה חדשה.
- ה־dashboard מציג trading tier, operator/global state, scope, quarantine, reconciliation age/running/stuck, auto-repair ו־recovery lifecycle.
- חסם אמיתי שנותר: אין sender חיצוני פעיל, משום שאין הרשאה מפורשת לכתובת יעד ולייצוא תוכן LIVE. ה־outbox נשמר ומוצג ללא flood.

# Tests

- Total collected ב־snapshot הנקי האחרון: 359 tests, ועוד 9 subtests.
- Result: 357 passed; שני מבחני timing נכשלו תחת עומס host.
- `test_slow_sqlite_writer...` עבר מיד בהרצה מבודדת.
- `test_fifty_dashboard_refreshes...` נכשל גם ב־SHA ההתחלתי (`b01cb8`) בטווח 128–194ms מול סף 100ms; לכן אינו רגרסיה של היישום. המבחן יוצר ארבעה processes על host LIVE עמוס ומודד scheduling spike. לא שונה הסף ולא הוחלש המבחן.
- בדיקות פונקציונליות ממוקדות: alerts 5/5; dashboard/recovery 64/64; חבילות reconciliation/propagation/quarantine/recovery שנבדקו — כולן עברו.
- אין כשל פונקציונלי ידוע ב־D1–D10.

# Git

- Initial SHA: `b01cb800c29a738677c92fa430f463efced20734`
- Commits:
  - `f81bf30` Fix reconciliation cancellation and propagation recovery
  - `3318a81` Decouple and bound reconciliation scheduling
  - `ba044f5` Add scoped quarantine and authoritative recovery
  - `794d291` Make authoritative exit repair idempotent
  - `b5207b0` Add alert lifecycle and startup watchdogs
  - `fed5929` Complete alert lifecycle reconciliation watchdog
  - `726c9b0` Recover transient reconciliation response errors
  - `3c30e3d` Clear incident flags on safe pause release
  - `ad05f3f` Add independent 24h soak service
  - `d4b66bc` Document live stability operations and handoff
  - `038c3a0` Expose complete recovery state in dashboard
  - `47f08d3` Complete independent soak incident coverage
- Final implementation SHA: `47f08d3e70d464644a6313f9ec6559d72c220f11`
- Push performed: **NO**
- שינויי משתמש קיימים נשארו מחוץ ל־commits הממוקדים.

# Production Deployments

- Trader deployment: 2026-08-25 10:56:28 UTC, PID `1744645`,‏ `NRestarts=0`.
- Soak deployment: 2026-08-25 10:56:39 UTC, PID `1744753`,‏ `NRestarts=0`.
- Dashboard deployment: 2026-08-25 10:56:52 UTC, PID `1744850`,‏ `NRestarts=0`.
- הגיבוי שלפני הפריסה: record `37`, status `ok`,‏ 380,293,009 bytes, SHA-256 `a74578f0f0c27f09a95221d9e1d574c9d25ea70cbb200c684654c5fb72c76b9c`, path `/opt/polymarket-btc-live/backups/poly_live_20260825_104219.sqlite3.gz`.
- אחרי startup hold היה disconnect יחיד ו־BEST_PRICE mismatch; המערכת נשארה fail-closed, ביצעה reconciliation, שחררה אוטומטית וחזרה ל־TRADING. המנטר תיעד את הרצף.

# State Repair

- Performed: **YES**, דרך application/repository בלבד.
- סיבה: EXIT remote `matched` עם balance סמכותי `0.006664`, בעוד state מקומי היסטורי היה `QUARANTINED` עם remaining `0`.
- Repository method: authoritative matched-exit rebuild/repair במסלול reconciliation.
- Audit ID עיקרי: `4775432` (`authoritative_exit_auto_repair`); rebuild מלא: `4775424`; quarantine resolution: `4775431`.
- Position: `724dd085-5552-5eb2-b33c-895c358773e3`; intent: `330c9108-8442-5e3d-9464-1d759b0c2048`.
- Before: state `QUARANTINED`, remaining `0`, dust `0`, exit value `2.73599856`, realized PnL `-1.06399944`.
- After: state `DUST`, remaining/dust `0.006664`, sellable `0`, exit value `2.7324`, realized PnL `-1.0626`.
- Evidence: matched remote order, linked attempt, three balance observations, 15.438s elapsed, proof valid.

# Current Financial State

נכון לתצפית האחרונה לאחר הפריסה:

- Active strategy positions: `0`; ה־position האחרון נסגר ל־DUST `0.005478` שאינו sellable.
- Open orders: `0`.
- Unresolved intents: `0`.
- Open quarantines: `0`.
- Remote public positions: `3`, remote public value/exposure: `0`.
- Verified account balance: `34.201846` USDC; account identity `VERIFIED`.
- Daily realized PnL שנצפה ב־STATUS: `2.931`.

# Reconciliation

- Readiness: `READY`.
- ריצות אחרונות: `ok`, gaps `[]`.
- Running: `0` בנקודת המסירה; stuck מעל חמש דקות: `0`.
- success age: נשאר מתחת ל־30 שניות בדגימות המסירה.
- אין open order או unresolved intent עם duplicate risk.

# Hot Path

- Market WS ו־User WS מחוברים לאחר ההתאוששות; שני ספרי החובה חוזרים ל־READY לפני שחרור entries.
- ingress queue חסום ל־32; לא נצפתה saturation ולא נזרקו critical frames.
- persistence queue חסום ל־2; אין persistence failures.
- disconnect יחיד לאחר restart תועד והתחבר מחדש; לא נצפתה WS storm.
- spikes היסטוריים מה־startup/backup נשמרים במדדים; ה־soak יבדוק bounded queues, exchange age, CPU/RSS/WAL ודיסק לאורך 24 שעות.

# Soak

- Service: `polymarket-soak-24h.service` — `ACTIVE`.
- Start UTC: `2026-08-25T10:56:39.768160Z`.
- Expected end UTC: `2026-08-26T10:56:39.768160Z`.
- Artifact directory: `/opt/polymarket-btc-live/soak/soak_24h_20260825T104014Z`.
- קצב: STATUS כל 10s, resources כל 60s, DB read-only כל 45s.
- המנטר הוא stdlib-only, פותח SQLite ב־`mode=ro` וב־`query_only=ON`, ושולח רק IPC `STATUS`.
- אירועים נתמכים: `REMOTE_MATCHED_ZERO_FILL`, `REMOTE_MATCHED_EXIT_PROPAGATION`, `RECONCILIATION_FAILED`, `RECONCILIATION_CONTRADICTION`, `QUARANTINE`, `AUTO_REPAIR`, `UNKNOWN_CAUSE`, `GLOBAL_HALT`, `AUTO_RECOVERY_STUCK`, `SERVICE_PID_CHANGED`, `WS_STORM`, `STUCK_RECONCILIATION`, pause/WS/entry/exit transitions.
- האימות הראשוני הוכיח דגימות STATUS/DB/resources, `running=0`,‏ `stuck=0`,‏ quarantine/alerts/orders/unresolved intents = 0, וכן תיעוד pause→global halt→auto release.

# Remaining Risks

1. תוצאת ה־soak המלאה תהיה זמינה רק ב־2026-08-26 10:56:39 UTC; זהו סיכון תצפיתי, לא חסם מסחר נוכחי.
2. שליחת דוא״ל חיצוני קריטי אינה פעילה ללא הרשאה מפורשת ליעד ולתוכן. ה־outbox וה־lifecycle פעילים, אך D9 נשאר PARTIAL.
3. מבחן ProcessPool timing תלוי scheduling ונכשל גם ב־baseline; מדדי ה־soak החי הם הראיה התפעולית הנמשכת.

RUNBOOK: `/opt/polymarket-btc-live/repo/POLYMARKET_LIVE_STABILITY_RUNBOOK_HE.md`
FINAL REPORT: `/opt/polymarket-btc-live/repo/POLYMARKET_LIVE_STABILIZATION_FINAL_REPORT_HE.md`

**SYSTEM STABILIZATION PARTIAL — SAFE BLOCKER REMAINS**
