# תכנית לייצוב תקלות ה־Dashboard המחזוריות

תאריך: 2026-08-19  
מערכת: `https://live-poly.dvirtechnologies.com/live-status/`  
סטטוס המסמך: תכנון בלבד — לא בוצעו שינויי קוד, schema, deployment או restart במסגרת האבחון

## 1. תקציר מנהלים

ה־Dashboard אינו מושבת לחלוטין. התקלה היא כשל מחזורי של `GET /live/dashboard/v1/overview`, שמחזיר לעיתים `500 Internal Server Error` עקב חריגה מתקציב שאילתת SQLite של שתי שניות. יתר ה־endpoints שנבדקו ממשיכים להחזיר `200` באותו מחזור.

הכשל הנקודתי הופך לכשל של המסך כולו משום שה־frontend טוען את `overview`, סדרת ה־P&L, העסקאות, התשתית וה־session באמצעות `Promise.all`. דחייה של בקשה אחת דוחה את כל ה־snapshot, ולכן המשתמש מקבל הודעת רענון שנכשל או נתונים קודמים במקום תצוגה חלקית.

המלצת המסמך היא ליישם פתרון בשתי שכבות:

1. תיקון ביצועי ממוקד ב־SQLite ובשאילתת ה־account snapshot, עם migration additive ויכולת rollback.
2. בידוד תקלות ב־frontend וב־API, כך שכשל ברכיב אחד לא ישבית את כל ה־Dashboard.

אין המלצה להסתפק בהגדלת timeout או ב־restart. פעולות אלה עשויות להסתיר את הבעיה לזמן קצר, אך אינן מתקנות את תוכנית השאילתה או את coupling בצד הלקוח.

## 2. השפעה על המשתמש

- המסך נטען לסירוגין או מציג הודעת שגיאת רענון.
- רכיבים שעבורם התקבל `200` אינם מוצגים, מפני שה־snapshot הכולל נכשל.
- התקלה עשויה להיעלם במחזור polling אחד ולחזור במחזור הבא, ולכן היא נראית אקראית.
- אין ראיה שהתקלה עצרה את שירות המסחר, את nginx או את שירות ה־Dashboard עצמו.
- אין ראיה למחסור בדיסק או למחיקת קבצי frontend.

## 3. ממצאים מאומתים

### 3.1 זמינות השירותים

- `nginx.service`: פעיל.
- `polymarket-dashboard.service`: פעיל מאז 2026-08-12, ללא אינדיקציה לקריסה בתהליך.
- `/live-status/` הציבורי מחזיר `302` למסך login ללא session, כמתוכנן.
- API מקומי ללא session מחזיר `401`, כמתוכנן.
- קבצי ה־static קיימים תחת `/var/www/live-status`.
- בדיסק כ־19GB פנויים; שימוש של כ־52%.

### 3.2 דפוס הכשל

בלוגים מאותו browser session הראו באותו מחזור:

- `session`: `200`
- `pnl/timeseries`: `200`
- `trades`: `200`
- `infrastructure`: `200`
- `overview`: לעיתים `500`, ולעיתים `200`

ה־traceback של `overview` מסתיים ב:

```text
sqlite3.OperationalError: interrupted
DashboardQueryError: dashboard query exceeded its time budget
```

מקור החריגה הוא `_execute()` ב־`dashboard_read_model.py`, שמתקין progress handler ומפסיק כל שאילתה שעוברת `query_timeout_seconds`. ערך ברירת המחדל הוא 2.0 שניות.

### 3.3 מאפייני מסד הנתונים ותוכנית השאילתה

- גודל `poly_live.sqlite3`: כ־2.1GB.
- `live_account_snapshots`: כ־117,981 רשומות בזמן הבדיקה.
- השאילתה ב־`account_equity()` מחפשת snapshot אחרון לפי environment, execution mode, verification status ו־cutover, ואז ממיינת `ORDER BY id DESC LIMIT 1`.
- ה־index הקיים הוא על:

```text
(environment, execution_mode, verification_status, ingested_at)
```

- `EXPLAIN QUERY PLAN` הראה שימוש ב־index, ובנוסף:

```text
USE TEMP B-TREE FOR ORDER BY
```

כלומר ה־index אינו מכסה היטב את סדר `id DESC`, והשימוש ב־`IN ('VERIFIED','RECONCILED')` יחד עם `COALESCE(ingested_at,sampled_at)` מקשה על הפקת הרשומה האחרונה ללא מיון נוסף.

### 3.4 coupling בצד ה־frontend

`fetchDashboardSnapshot()` מפעיל במקביל חמש בקשות בתוך `Promise.all`. כל rejection מבטל לוגית את ה־snapshot כולו, גם אם ארבע בקשות הצליחו. אין כרגע envelope פר־section שמאפשר לשמור נתון קודם, להציג stale, או להציג שגיאה מקומית בלבד.

### 3.5 עומס מיותר ב־overview

`overview()` אינו endpoint קטן. הוא מרכיב ברצף:

- account equity
- P&L לטווח
- P&L מצטבר מאז cutover
- positions
- orders
- markets
- activity
- alerts
- health
- metadata ו־reconciliation lookups חוזרים

חלק מהמידע נטען גם ב־endpoints אחרים באותו polling cycle. לכן יש גם כפילות שאילתות וגם blast radius גדול.

## 4. ניתוח סיבה

### סיבה ישירה

שאילתת SQLite בתוך `account_equity()` חורגת לעיתים מתקציב שתי השניות ונקטעת על ידי progress handler.

### גורמים תורמים

1. index שאינו תואם ל־filter ול־ordering של “הרשומה האחרונה”.
2. טבלה שגדלה באופן מתמשך ומסד נתונים בגודל 2.1GB.
3. ביטוי `COALESCE(...)` בתנאי הטווח, שאינו מיטבי ל־index רגיל.
4. `overview` רחב שמבצע שרשרת שאילתות ומגדיל את הסיכוי שכשל יחיד יפיל את התגובה.
5. cache קצר של שתי שניות בלבד; כישלון loader אינו יכול להשתמש אוטומטית בערך stale אחרון.
6. `Promise.all` ב־frontend הופך תקלה מקומית לכשל מסך מלא.

### מה אינו סיבת השורש

- nginx אינו כבוי.
- שירות ה־Dashboard אינו כבוי.
- אין מחסור בדיסק.
- authentication עובד לפי החוזה.
- restart לבדו אינו משנה index או query plan.

## 5. מטרות התיקון

- `overview` יעמוד ב־p95 של פחות מ־300ms וב־p99 של פחות משנייה תחת עומס ה־production הנוכחי.
- אפס תוצאות `500` עקב `DashboardQueryError` בחלון soak של 24 שעות.
- כשל של section אחד לא ימנע הצגה של sections אחרים.
- נתון אחרון מוצלח יישמר ויוצג כ־`STALE` עם גיל וסיבת כשל מפורשים.
- לא להגדיל write contention מול trader ולא לבצע migration חוסם ללא חלון מבוקר.
- לא להציג `REAL` כאשר המקור stale, חלקי או לא זמין.

## 6. חלופות פתרון

### חלופה A — index מותאם ושכתוב השאילתה

#### הצעה

להחליף את דפוס השאילתה כך שיוכל לבחור את הרשומה האחרונה ישירות מה־index. יש לבחון שתי וריאציות באמצעות נתוני production clone:

1. index המתאים לסדר:

```sql
CREATE INDEX ... ON live_account_snapshots(
  environment,
  execution_mode,
  verification_status,
  id DESC
);
```

2. index לפי זמן ingestion וסיום המיון לפי זמן, אם semantics מאשרים ש־`ingested_at` הוא מפתח ה־freshness הקנוני:

```sql
CREATE INDEX ... ON live_account_snapshots(
  environment,
  execution_mode,
  verification_status,
  ingested_at DESC
);
```

מומלץ להימנע מ־`COALESCE` במסלול החם. מאחר שרשומות post-cutover אמורות להכיל `ingested_at`, אפשר להשתמש ב־`ingested_at >= ?` ולשמור fallback נפרד רק עבור legacy אם הוא נדרש בפועל.

#### יתרונות

- מטפל בסיבה הישירה.
- שינוי קטן יחסית בקוד.
- שומר על חוזה API קיים.

#### סיכונים

- בניית index על production DB בגודל 2.1GB עלולה לצרוך זמן, I/O ושטח זמני.
- index נוסף מגדיל write amplification.
- index לא נכון עשוי לשפר query אחד אך לפגוע בכתיבה ללא תועלת מספקת.

#### החלטה נדרשת

לבחור את הווריאציה רק לאחר `EXPLAIN QUERY PLAN` ו־benchmark על clone עדכני. תנאי קבלה: אין `TEMP B-TREE`, וזמן חיפוש snapshot אחרון יציב בהרצות cold/warm.

### חלופה B — snapshot/current-state table ייעודית

#### הצעה

להחזיק טבלה קטנה של state נוכחי, למשל `live_dashboard_current_account`, עם שורה אחת לכל `(environment, execution_mode)`. כתיבת account snapshot תעדכן גם את הטבלה באמצעות transaction או projector אמין.

#### יתרונות

- קריאה קבועה ומהירה ללא תלות בגודל ההיסטוריה.
- מפריד בין historical store לבין operational read model.
- מתאים לטווח ארוך אם טבלאות telemetry ממשיכות לגדול.

#### סיכונים

- מוסיף write path ו־consistency responsibility.
- דורש backfill, provenance, reconciliation ובדיקת atomicity.
- שינוי רחב יותר מ־index.

#### שימוש מומלץ

פתרון שלב שני אם index בלבד אינו עומד ב־SLO, או כחלק מתכנון read model ייעודי לכל ה־Dashboard.

### חלופה C — stale-if-error ב־ResponseCache

#### הצעה

להרחיב את `ResponseCache` כך שישמור גם timestamp ויחזיר last-known-good בעת `DashboardQueryError`, עם metadata מפורש:

- `stale=true`
- `freshness_seconds`
- `quality=STALE` או `PARTIAL`
- reason יציב כגון `QUERY_TIMEOUT_USING_LAST_KNOWN_GOOD`

יש לקבוע hard maximum age; לאחריו אין להחזיר cache כאילו הוא שימושי.

#### יתרונות

- מונע outage ויזואלי בזמן timeout רגעי.
- שינוי backend קטן יחסית.
- מספק graceful degradation עוד לפני שינוי frontend מלא.

#### סיכונים

- עלול להסתיר הידרדרות מתמשכת ללא alerting.
- אסור לסמן cached response כ־REAL ללא גיל וסיבת stale.
- cache בזיכרון נעלם ב־restart ואינו פתרון לביצועים.

### חלופה D — בידוד תקלות ב־frontend

#### הצעה

להחליף `Promise.all` באחת משתי גישות:

1. `Promise.allSettled` עם state נפרד לכל section.
2. hooks/stores נפרדים לכל endpoint, עם last-known-good פר section.

יש לשמור את הנתון הקודם בעת כשל refresh, לסמן אותו stale, ולהציג הודעת שגיאה מקומית בכרטיס שנפגע. auth failure נשאר global; שגיאת data endpoint אינה global.

#### יתרונות

- מפחית משמעותית blast radius.
- המשתמש ממשיך לראות עסקאות, תשתית ו־P&L גם אם overview נכשל.
- מאפשר retry ותדירות polling מותאמת לכל מקור.

#### סיכונים

- דורש model state מפורט יותר ובדיקות UI נוספות.
- חייבים למנוע ערבוב שקט בין timestamps שונים.

### חלופה E — פירוק או הקטנת overview

#### הצעה

להפסיק להחזיר דרך `overview` נתונים שכבר נטענים בנפרד, או לפצל את ה־endpoint ל־summary קטן ו־sections עצמאיים. אפשר להשאיר `overview` לתאימות זמנית ולסמן deprecation פנימי.

#### יתרונות

- פחות שאילתות כפולות.
- latency וצימוד נמוכים יותר.
- failure domain קטן יותר.

#### סיכונים

- שינוי חוזה בין frontend ל־backend.
- דורש rollout תואם גרסאות.

### חלופה F — הגדלת timeout

#### הצעה

להעלות זמנית את `query_timeout_seconds` משתי שניות לערך גבוה יותר.

#### הערכה

לא מומלץ כפתרון קבוע. ניתן לשקול רק mitigaton קצר לאחר מדידת השפעה, משום שהוא מגדיל זמן המתנה, thread occupancy וסיכון ל־request pile-up בלי לשפר את השאילתה.

### חלופה G — restart תקופתי

#### הערכה

אינה פתרון. restart עשוי לשנות זמנית cache או memory pressure, אך ה־query plan והגידול במסד נשארים. אין להשתמש ב־restart loop כדי למסך את התקלה.

## 7. פתרון מומלץ וסדר הטמעה

### שלב 0 — baseline ויכולת rollback

1. ליצור backup עקבי ומאומת של DB/config לפני migration.
2. לבצע `PRAGMA quick_check` ולתעד DB/WAL size.
3. ללכוד baseline של latency ו־query plan עבור כל שאילתה מתוך `overview`.
4. למדוד p50/p95/p99, timeout count, CPU, I/O, WAL growth ו־DB busy rate.
5. לבצע benchmark על clone, לא על production trader, עבור בניית index ו־query variants.

### שלב 1 — תיקון מסלול הקריאה החם

1. להוסיף migration additive ו־idempotent עבור ה־index שנבחר.
2. לשכתב את account snapshot query כדי להשתמש בעמודת זמן קנונית ללא `COALESCE`, אם invariant הנתונים מאומת.
3. להוסיף regression test שמייצר לפחות 100k snapshots ומוודא query plan ללא temp sort.
4. להוסיף performance guard שאינו נשען על timing קשיח בלבד: בדיקת plan ובדיקת מספר opcode/progress callbacks או benchmark עם margin.
5. לפרוס תחילה migration, לאמת, ורק אחר כך לטעון קוד runtime אם נדרש.

### שלב 2 — graceful degradation ב־backend

1. להוסיף stale-if-error ל־cache עבור `DashboardQueryError` בלבד.
2. לא להסתיר authentication, validation או programming errors.
3. להחזיר metadata פר section או response-level degradation מפורש.
4. להוסיף metric/alert לכל fallback, כדי שהמערכת לא תיראה בריאה בזמן שהיא משרתת cache ישן.

### שלב 3 — בידוד תקלות ב־frontend

1. להחליף snapshot אטומי ב־state פר endpoint.
2. לשמור last-known-good ולהציג גיל נתון.
3. להפוך רק auth/session failure ל־global blocking state.
4. להציג error מקומי ב־overview תוך המשך הצגת trades, timeseries ו־infrastructure.
5. להוסיף retry עם backoff ו־jitter ל־endpoint שנכשל; אין להגביר polling בעת כשל.

### שלב 4 — צמצום overview

1. למפות אילו שדות באמת נצרכים מה־overview.
2. להסיר כפילויות מול endpoints שכבר נטענים בנפרד.
3. לשקול `summary` קטן או current-state read model ייעודי.
4. לשמור תאימות עד שה־frontend החדש פרוס ומאומת.

### שלב 5 — מדיניות retention/read model ארוכת טווח

מסד של 2.1GB אינו בהכרח תקלה, אך הוא סימן שיש להפריד בין היסטוריה תפעולית לבין queries של current state. יש להחליט על:

- retention לכל טבלת telemetry.
- archive מאומת לפני purge.
- downsampling לסדרות זמן.
- current-state tables לרכיבים חמים.
- `VACUUM` רק בתכנון offline/controlled מתאים; לא כתגובה אוטומטית לתקלה.

## 8. שינויים מוצעים בקוד וב־schema

### Backend

- `live/dashboard_read_model.py`
  - שכתוב שאילתת snapshot אחרון.
  - optional instrumentation לפי query name.
  - שמירת fail-closed semantics של quality/provenance.
- `live/dashboard_api.py`
  - stale-if-error ו־metadata מפורש.
  - צמצום כפילות ב־overview.
- `live/dashboard_schema.py`
  - migration additive ל־index.
- `tests/test_dashboard_v1.py`
  - query-plan regression.
  - timeout fallback.
  - stale age ו־quality.
  - partial endpoint failure.

### Frontend

- `app/live-data.ts`
  - מעבר מ־`Promise.all` לטעינה מבודדת.
  - result type לכל endpoint: data/error/receivedAt.
- `app/page.tsx`
  - error boundary/state מקומי לכל section.
  - last-known-good עם badge `STALE`.
- בדיקות contract
  - endpoint יחיד נכשל והשאר מוצגים.
  - `401/403` עדיין מעביר למסך auth.
  - refresh כושל אינו מוחק נתונים קודמים.

## 9. תכנית בדיקות

### בדיקות יחידה ואינטגרציה

- 100k+ account snapshots עם שילוב statuses.
- הרשומה האחרונה תיבחר נכון עבור `LIVE/REAL_TRADING` בלבד.
- רשומות legacy ללא `ingested_at` לא יקודמו בטעות ל־REAL.
- timeout מחזיר stale cache רק כאשר קיים last-known-good תקף.
- cache שעבר max age מחזיר unavailable מפורש.
- כשל `/overview` אינו מונע הצגת `/trades` ו־`/infrastructure`.
- אין שינוי ב־auth, rate limiting או CSRF/logout.

### בדיקות ביצועים

- cold cache ו־warm cache.
- polling יחיד ומספר sessions מקבילים.
- trader כותב בזמן dashboard קורא.
- בדיקת WAL growth ו־write latency לאחר הוספת index.
- `EXPLAIN QUERY PLAN` ללא `USE TEMP B-TREE FOR ORDER BY` במסלול החם.

### canary ו־soak

- canary של dashboard בלבד; אין צורך לגעת ב־trader אם migration כבר הוחל בבטחה.
- 30 דקות smoke/canary ולאחר מכן soak של 24 שעות.
- מעקב אחר `overview` status distribution, latency, fallback count ו־data freshness.
- rollback אם יש עלייה ב־write latency, DB busy, trader loop lag או mismatch בנתונים.

## 10. תכנית rollout ו־rollback

### rollout

1. backup + verification.
2. migration על clone ומדידת משך/שטח.
3. migration מבוקר ב־production בחלון מאושר.
4. אימות index ו־query plan.
5. rollout backend עם canary.
6. rollout frontend resilient.
7. soak ומדדי קבלה.

### rollback

- שינוי query: rollback לקוד קודם.
- frontend: החזרת static artifact קודם באופן אטומי.
- index חדש: בדרך כלל ניתן להשאירו בזמן rollback קוד; להסיר רק בחלון נפרד ולא כחלק מ־rollback לחוץ.
- אין לבצע restore DB מלא אלא אם migration גרם לנזק ממשי, ורק כאשר תהליכי הכתיבה נעצרו לפי נוהל קיים.

## 11. Observability נדרשת

להוסיף או לחשוף:

- latency לפי endpoint ולפי query name.
- `dashboard_query_timeout_total`.
- `dashboard_stale_fallback_total` וגיל הערך שהוחזר.
- cache hit/miss/load wait.
- SQLite busy/interrupted counts.
- DB/WAL size וקצב גידול.
- frontend section failure rate.
- alert כאשר fallback נמשך יותר משני מחזורי polling או כאשר freshness חוצה threshold.

אין לרשום SQL params רגישים, session tokens או payload מלא בלוגים.

## 12. קריטריוני קבלה

- אין `500` של `/overview` ב־24 שעות soak.
- p95 קטן מ־300ms ו־p99 קטן משנייה בתנאי production רגילים.
- query plan של snapshot אחרון אינו משתמש במיון temp.
- תוספת ה־index אינה מעלה באופן מהותי את latency של כתיבות trader.
- כשל מכוון של `/overview` משאיר את שאר המסך שמיש.
- נתון cached מסומן `STALE`, כולל age ו־reason.
- auth failure עדיין אינו חושף נתונים.
- כל בדיקות backend/frontend וה־static build עוברים.

## 13. החלטה מומלצת

לאשר יישום מדורג של A + C + D:

1. A — index ושכתוב query, לאחר benchmark על clone.
2. C — stale-if-error עם observability ו־hard max age.
3. D — state מבודד ב־frontend במקום `Promise.all` אטומי.

לאחר soak יש להחליט אם נדרש B, current-state table ייעודית. חלופות F ו־G אינן מומלצות כפתרון קבוע.

## 14. שאלות פתוחות לפני יישום

1. האם `ingested_at` מובטח לכל account snapshot post-cutover, כך שניתן להסיר `COALESCE` מהמסלול החם?
2. מהו חלון התחזוקה המאושר לבניית index על DB בגודל 2.1GB?
3. מהו max age שמותר להציג עבור equity, positions, health ו־markets? אין להשתמש באותו threshold לכל המקורות.
4. האם קיימת דרישה עסקית להשאיר `overview` כחוזה ציבורי, או שניתן להפוך אותו ל־summary מצומצם?
5. מהי מדיניות retention הרצויה עבור account snapshots ו־market snapshots?

