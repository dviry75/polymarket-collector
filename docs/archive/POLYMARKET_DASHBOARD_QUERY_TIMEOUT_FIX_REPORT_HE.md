# דוח תיקון — Dashboard query timeout

תאריך: 2026-08-19 UTC
סביבה: `LIVE / REAL_TRADING`
Endpoint: `GET /live/dashboard/v1/overview`

## Root cause

מנגנון ההגנה ב-`DashboardReadModel._execute()` מתקין SQLite progress handler ובתקציב של 2.0 שניות מפסיק query עם `sqlite3.OperationalError: interrupted`, שמומר ל-`DashboardQueryError`. ה-API לא הסתיר את החריגה ולכן החזיר 500.

נמצאו שני hot paths לא יעילים בתוך `overview()`:

1. `account_equity()` סינן באמצעות `COALESCE(ingested_at,sampled_at)` ומיין `id DESC`. ה-index הקיים הסתיים ב-`ingested_at`, ולכן התוכנית כללה `USE TEMP B-TREE FOR ORDER BY`. cold run על clone ארך 4.348 שניות.
2. לאחר תיקון הראשון, Production verification חשף bottleneck נוסף ב-`recent_activity()`: על יותר ממיליון audit rows ה-planner בחר provenance index וביצע temp sort. השאילתה נקטעה אחרי 2 שניות.

## Measurements before

- DB: כ-2.1GiB; WAL: כ-8.7MiB; SHM: 32KiB.
- `live_account_snapshots`: 112,130 ב-backup שנמדד; 122,752 במדידה הסופית ב-LIVE.
- distribution ב-backup: 29,167 `LIVE/REAL_TRADING/VERIFIED`; 82,963 legacy `UNKNOWN` בכל שלושת ממדי provenance.
- index קיים: `idx_live_account_snapshots_dashboard_provenance(environment,execution_mode,verification_status,ingested_at)`, כ-4.55MB ב-clone.
- baseline account query: cold 4,347.987ms; warm בדרך כלל 49–210ms; temp B-tree.
- `live_audit_timeline`: 1,049,074 rows בעת האבחון; 313,403 רלוונטיות post-cutover; temp B-tree.
- timeout: 2.0s, progress callback בכל 10,000 SQLite opcodes.

### הוכחת `ingested_at`

ב-backup: כל 29,167 account rows הרלוונטיות post-cutover הכילו `ingested_at` ו-`sampled_at`.
ב-LIVE: כל 39,629 account rows הרלוונטיות בעת הבדיקה הכילו את שניהם.
ב-audit timeline: כל 313,403 הרשומות הרלוונטיות הכילו `ingested_at`.

לכן ניתן להסיר `COALESCE` בשני המסלולים החמים של נתוני LIVE/REAL_TRADING post-cutover. rows ללא `ingested_at` נכשלות סגור ואינן מוצגות כנתון מאומת עכשווי.

## Alternatives tested

| חלופה | build / size ב-clone | latency | plan / החלטה |
|---|---:|---:|---|
| index קיים + COALESCE | קיים / 4.55MB | cold 4.348s; warm 49–210ms | temp sort; נדחה |
| full `(environment,execution_mode,verification_status,id DESC)` | 995ms / 4.10MB | 0.059–0.244ms | מהיר, אך write amplification וגודל גבוהים יותר |
| `ingested_at DESC` בלבד | 221ms / 1.70MB | 62–226ms | temp sort נשאר; נדחה |
| partial account `(environment,execution_mode,id DESC)` | 414ms / 0.885MB | 0.042–0.191ms | ללא temp sort; נבחר |
| latest-then-freshness | תלוי ב-index | מהיר רק עם full index | לא נתן יתרון על partial hot-path |
| partial audit index חדש | 19.833s / 8.32MB | מהיר | זמן בנייה/חסימת writer לא רצויים; נדחה |
| audit query עם `idx_live_timeline_time` הקיים | ללא build / ללא index חדש | 0.080–0.309ms | ללא sort; נבחר |

## Decision

נבחר התיקון הקטן ביותר שמסיר את עבודת הסריקה/מיון:

- partial index additive קטן ל-account snapshots בלבד.
- שימוש ב-`ingested_at` במסלול post-cutover המוכח.
- `recent_activity()` משתמש במפורש ב-index `id DESC` שכבר קיים, כדי לקרוא את עשר הרשומות האחרונות ללא temp sort.
- תקציב 2 שניות נשאר ללא שינוי; לא נוסף current-state table ולא stale-if-error, משום שלא נדרשו לאחר תיקון התוכנית.

## Changes performed

- `dashboard_read_model.py`: account query עבר מ-`COALESCE` ל-`ingested_at`; audit query עבר ל-`ingested_at` עם `INDEXED BY idx_live_timeline_time`.
- `dashboard_schema.py`: schema version 6; migration additive/idempotent ל-`idx_live_account_snapshots_dashboard_current`.
- `test_dashboard_v1.py`: migration count עודכן ונוספו בדיקות לכך שה-partial index נבחר ללא temp sort וש-row ללא `ingested_at` אינה נחשבת current.
- LIVE DB: index v6 ורשומת migration נוספו; רק `polymarket-dashboard.service` הופעל מחדש. ה-trader לא הופעל מחדש.

## Measurements after

- account partial index ב-LIVE: כ-1.21MB במדידה הסופית.
- account plan: `SEARCH ... USING INDEX idx_live_account_snapshots_dashboard_current`; אין temp B-tree.
- audit plan: `SCAN ... USING INDEX idx_live_timeline_time`; אין temp B-tree.
- endpoint polling: 50/50 responses היו 200; median 57.9ms, min 28.4ms, max 1,701.6ms.
- concurrency: 12/12 responses היו 200 בארבעה workers; median 463.5ms, max 2,078.2ms.
- account quality בכל המחזורים: `REAL`.
- WAL נשאר כ-8.7MiB לאחר המדידה; לא בוצע VACUUM.

## Production verification

- Dashboard health: `{"status":"ok"}`.
- `polymarket-dashboard.service`: active לאחר restart מבוקר.
- `polymarket-trader.service` היה active לאורך המיגרציה, ה-restart של Dashboard ובדיקות ה-polling הראשונות. הוא נעצר נקי ב-17:29:21 UTC, הופעל שוב מחוץ לשרשרת התיקון ב-18:05:51 UTC, ונעצר שוב בצורה נקייה ב-2026-08-20 07:32:30 UTC. מצב המסירה הסופי הוא `inactive/dead`; לא הופעל מחדש במסגרת השלמת ה-Frontend.
- probe סופי לאחר פריסת ה-Frontend: חמשת endpoints החזירו 200; סדרה נוספת של 10 קריאות overview החזירה 10/10 תגובות 200, עם 19.6–374.2ms.
- לאחר התיקון לא נמצאו בלוג Dashboard: `interrupted`, `busy`, `locked`, query-budget error או 500.
- לא נמצאו שגיאות SQLite מקבילות ב-trader בזמן שהיה פעיל. stress polling יצר מספר `BrokenPipeError` ב-IPC `STATUS` בעת סגירת חיבור client; זו אינה שגיאת DB או trading path והשירות נשאר active.
- suite ממוקד: 21 passed.
- suite מלא: 268 passed, 2 timing failures; שני הכשלים עברו מיד ב-rerun מבודד (2 passed in 2.19s), ולכן סווגו כ-resource-contention flaky ולא regression פונקציונלי.
- clone regression כלל 112,130 account rows ו-1,011,525 audit rows, ולכן כיסה 100k+ rows.

## Frontend resilience

מקור ה-Next.js אותר ב-`/home/dvir/polymarket-dashboard-preview` וב-repository `dviry75/polymarket-dashboard`. יושם state נפרד לכל endpoint (`data/loading/error/lastSuccessAt/lastAttemptAt`) באמצעות `Promise.allSettled`: כשל של endpoint יחיד שומר את ה-last-known-good שלו, מציג אזהרת STALE וגיל מקומי, ומאפשר לשאר המקטעים להתעדכן. כשל 401/403 נשאר חסימה גלובלית מכוונת.

נוספו בדיקות fault injection לכשל `overview` ולפקיעת session. התוצאה הסופית: 7/7 בדיקות Frontend עברו, lint עבר, build production עבר, וה-bundle החדש נפרס אטומית ב-2026-08-20 07:40 UTC תחת `/var/www/live-status`. הגרסה הקודמת נשמרה ב-`/var/www/live-status.rollback-20260820T074047Z`. Nginx config עבר אימות, ה-bundle המוגש מכיל את מנגנון הבידוד החדש, ולקוח לא מאומת מופנה ל-login כמצופה.

## Remaining risks

- polling מקביל העלה latency עד כ-2.08s, אף שכל הבקשות הצליחו; cache single-flight מצמצם זאת ב-polling רגיל.
- ה-IPC server רושם BrokenPipe בעת stress client disconnect; ראוי לטפל בנפרד, ללא קשר ל-SQLite.
- תקלה מלאה בו-זמנית בכל endpoints שאינה auth תשאיר last-known-good עם אזהרות מקומיות; אם אין snapshot קודם יוצגו ערכים לא זמינים. זהו fail-visible מכוון.
- index provenance הישן נשאר כי הוא משרת queries נוספים; לא הוסר דבר.

## Rollback

1. להחזיר את שתי השאילתות ב-`dashboard_read_model.py` לגרסה הקודמת ולהפעיל מחדש רק Dashboard.
2. להסיר את index v6: `DROP INDEX IF EXISTS idx_live_account_snapshots_dashboard_current;`.
3. אופציונלית להסיר migration row 6 ולעדכן `dashboard_schema_version` ל-5 רק אם הקוד הישן נפרס במקביל.

ה-index additive ואינו משנה historical data. rollback אינו דורש restart ל-trader.

## Git

- Branch: `feature/complete-live-dashboard-real-data-20260812`
- Code commit: `efa2b7c`
- Backend documentation commits: `74b5cc1`, `baa1aad`
- Frontend commit: `0b3e149` (`dviry75/polymarket-dashboard`)
- Push status: כל ארבעת ה-commits נדחפו ל-branch המרוחק המתאים.
