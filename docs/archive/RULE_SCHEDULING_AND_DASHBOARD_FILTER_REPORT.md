# דוח סיום: חלונות פעילות לחוקים וסינון דאשבורד לפי חוק

## תקציר מנהלים

נוספה תמיכה בהגדרת חלון כניסה יחסי לסוף אירוע לכל חוק, חלונות אי-פעילות לפי יום ושעה, שמירת מספר השניות שנותרו עד סוף האירוע בזמן פתיחת עסקה, וסינון דאשבורד לפי חוק.

המשתמש מגדיר את השדות החדשים בטופס יצירת חוק. בדאשבורד ניתן לבחור חוק מתוך רשימת החוקים; הבחירה עוברת ב-URL ונשמרת גם ב-localStorage כברירת מחדל כאשר אין פרמטר URL.

הבדיקות המקומיות עברו. לא בוצעה פריסה ל-TST מתוך סביבת העבודה המקומית הזו.

## מבנה בסיס הנתונים

בטבלת `rules` נוספו:

* `entry_window_start_seconds_before_end INTEGER`
* `entry_window_end_seconds_before_end INTEGER`
* `schedule_timezone TEXT NOT NULL DEFAULT 'Asia/Jerusalem'`

בטבלת `deals` נוספה:

* `entry_seconds_before_event_end INTEGER`

נוספה טבלה חדשה:

```text
rule_inactive_windows
id INTEGER PRIMARY KEY AUTOINCREMENT
rule_id INTEGER NOT NULL
day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6)
start_time TEXT NOT NULL
end_time TEXT NOT NULL
status TEXT NOT NULL CHECK (status IN ('active', 'inactive'))
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE
```

נוספו אינדקסים:

* `idx_rule_inactive_windows_rule_day_status`
* `idx_deals_entry_at`
* `idx_deals_exit_at`

ברירת המחדל לחוקים קיימים היא `schedule_timezone = Asia/Jerusalem`, וחלון כניסה ריק (`NULL`, `NULL`) שומר על ההתנהגות הישנה ללא הגבלת זמן יחסית לסוף האירוע.

## לוגיקת מנוע החוקים

לפני פתיחת עסקה חדשה המנוע בודק:

1. החוק פעיל.
2. החוק עבר את שער `eligible_after_event_id`.
3. אין חלון אי-פעילות פעיל לפי `schedule_timezone`.
4. מחיר הכניסה מתאים.
5. אין עסקה פתוחה שחוסמת כניסה.
6. מכסת הכניסות לצד לא נחצתה.
7. אם הוגדר חלון כניסה, `events.end_time - orderbook.sampled_at` נמצא בתחום המותר.

חלון הכניסה משתמש בזמן סוף האירוע האמיתי מתוך `events.end_time`. אם הוגדר חלון כניסה אך אין סוף אירוע אמין, העסקה לא נפתחת. חלונות אי-פעילות שחוצים חצות נתמכים, למשל Monday 22:00-02:00 חוסם גם ביום שני אחרי 22:00 וגם ביום שלישי לפני 02:00.

עסקאות שכבר פתוחות ממשיכות להיסגר לפי stop loss, take profit או event resolution גם אם החוק הפך ללא פעיל או נכנס לחלון אי-פעילות.

## שינויים ב-UI

בטופס יצירת חוק נוספו:

* `Entry window start, seconds before event end`
* `Entry window end, seconds before event end`
* `Schedule timezone`
* אזור להוספת חלונות אי-פעילות מרובים

השדות נשלחים ל-`POST /rules`, וה-API מחזיר גם את `inactive_windows` לכל חוק.

## סינון הדאשבורד

נוסף פילטר גלובלי לפי חוק באמצעות `rule_filter`:

* `all` מציג את כל החוקים.
* מזהה חוק יחיד מסנן את מדדי ה-KPI, ביצועי חוקים, מגמות, תמונת סיכון, תנאי שוק, טבלת עסקאות ובדיקות איכות רלוונטיות.
* קיימת תמיכה בסיסית גם ברשימת מזהים מופרדת בפסיקים בשכבת השרת.
* URL גובר על ברירת המחדל.
* כאשר אין `rule_filter` ב-URL, הבחירה שנשמרה ב-localStorage נטענת כברירת מחדל.

## Backfill

נוסף backfill עבור `entry_seconds_before_event_end` בתוך `init_db`.

הערך מתעדכן רק כאשר קיימים:

* `deals.entry_at`
* `deals.event_id`
* התאמה אמינה ל-`events.event_slug`
* `events.end_time`

אם אחד מהם חסר, הערך נשאר `NULL`. בבדיקות המקומיות נבדק חישוב של 83 שניות לפני סוף אירוע. לא בוצע backfill על בסיס נתוני TST או ייצור מתוך הסביבה הזו.

## בדיקות

| שם הבדיקה | סוג | תוצאה | הערות |
|---|---|---|---|
| `python -m py_compile polymarket-collector\app.py` | קומפילציה | PASS | אין שגיאות תחביר |
| `python -m unittest polymarket-collector.tests.test_rules_deals` | Unit/Integration | PASS | 19 בדיקות |
| `python -m unittest discover -s polymarket-collector\tests` | Regression | PASS | 46 בדיקות |
| `npm test` | לא רלוונטי | FAIL | אין `package.json` בשורש הפרויקט |
| Migration על DB זמני | Integration | PASS | `init_db` יוצר עמודות וטבלה חדשה |
| UI rule creation | API/HTML | PASS חלקי | נבדק דרך TestClient ו-HTML, לא בדיקה ידנית בדפדפן |
| Dashboard filter tests | Unit/Integration | PASS חלקי | כוסה בשכבת שרת; לא בוצעה בדיקת דפדפן מלאה |
| TST smoke tests | TST | NOT RUN | לא בוצעה פריסה לשרת TST |

## בדיקות שרת

לא בוצעה בדיקת שרת TST.

* hostname: לא נבדק
* branch: `main`
* commit hash בתחילת העבודה: `497f03c497d0c71e55e5b36717a69535de875665`
* שירות שנבדק: מקומי בלבד
* Health Check ב-TST: לא נבדק
* Migration ב-TST: לא נבדק
* Restart ב-TST: לא בוצע
* לוגים ב-TST: לא נבדקו
* לא בוצעה פעילות מסחר אמיתית

## קבצים ששונו

* `polymarket-collector/app.py` - schema, API, מנוע חוקים, UI, פילטר דאשבורד, ייצוא Excel ו-backfill.
* `polymarket-collector/tests/test_rules_deals.py` - בדיקות לחלונות כניסה, חלונות אי-פעילות, API וייצוא.
* `polymarket-collector/tests/test_coinbase_volume.py` - התאמת בדיקות ייצוא לגיליון החדש.
* `RULE_SCHEDULING_AND_DASHBOARD_FILTER_REPORT.md` - דוח הסיום.

## בעיות ומגבלות

* לא בוצעה בדיקה ידנית בדפדפן.
* לא בוצעה פריסה ל-TST.
* פילטר UI מאפשר בחירה יחידה, בעוד ששכבת השרת תומכת גם ברשימת מזהים מופרדת בפסיקים.
* אם חוק מוגבל בחלון כניסה אך האירוע חסר `end_time`, המערכת חוסמת פתיחה כדי לא להמציא זמן.

## Git

* branch: `main`
* commit hash לפני עבודה: `497f03c497d0c71e55e5b36717a69535de875665`
* commit hash: יש לבדוק ב-`git log -1`; hash משתנה בכל amend ולכן לא מוטבע בדוח עצמו.
* הודעת commit: `Add rule scheduling and dashboard rule filters`
* push: לא בוצע; ניסיון `git push origin main` נחסם על ידי מדיניות האבטחה של סביבת Codex בגלל ייצוא ל-GitHub remote חיצוני.

## מסקנה

השינוי מוכן לבדיקה מקומית ומוכן לשלב הבא של בדיקת TST. כל בדיקות ה-Python המקומיות עברו, ולא נמצאה סכנה לשינוי נתוני עסקאות קיימים מעבר להוספת עמודות וטבלת חלונות, ו-backfill שמעדכן רק ערכים שניתנים לחישוב ודאי.
