# דוח תקלה — שעות חוסר פעילות של חוקים

## 1. תקציר מנהלים

חוק עם שעות חוסר פעילות המשיך לפתוח עסקאות DEMO בזמן החסום. במסד ה־TST נמצא שהטווחים נשמרו במצב `inactive`, ולכן השרת התעלם מהם. ה־UI הציג בתוך "Inactive window" שדה עמום בשם Status, כך שניתן היה להבין בטעות ש־inactive הוא המצב הרצוי. לאחר התיקון ה־UI מסביר אם הטווח מופעל וחוסם, קיימת נקודת הכרעה מרכזית בשרת, ונעשית בדיקה חוזרת מיד לפני יצירת העסקה.

## 2. שורש התקלה

הראיה מה־TST: לחוק 11 (`רובוט משוגע ניסיון`) היו טווחים 00:00–16:00, אך כולם נשמרו עם `rule_inactive_windows.status='inactive'`. למרות זאת נרשמו עסקאות, למשל עסקה 5833 ב־`2026-07-28T02:04:01Z` — 05:04 ב־`Asia/Jerusalem`, בתוך הטווח שהוצג למשתמש.

ב־`polymarket-collector/app.py`, הפונקציה הישנה `rule_in_inactive_window` טענה רק `status='active'`. רשומה מושבתת לא חסמה, כמתוכנן במודל, אך ה־UI לא הסביר את משמעות הסטטוס. בנוסף, הבדיקה בוצעה רק בתחילת `process_demo_entries` ולא סמוך ל־`INSERT INTO deals`.

לא נמצאה סטיית timezone או מיפוי ימים: השרת משתמש ב־UTC מודע ל־timezone וממיר ל־IANA timezone של החוק; Python `weekday()` וה־UI משתמשים ב־0=יום שני ו־6=יום ראשון. הבדיקות הקודמות בדקו רק טווח `active` ובדיקה מוקדמת אחת, ולכן לא גילו את הכשל.

## 3. השינוי שבוצע

* `polymarket-collector/app.py` — נוספה `matching_inactive_window`; נוספה `can_rule_open_new_deal`, הבודקת מה־DB קיום חוק, סטטוס ראשי, טווח מופעל ועסקה פתוחה; נוספה בדיקה סופית לפני INSERT; נוספו לוגים; `00:00–00:00` מוגדר כיום מלא; נוסח ה־UI הובהר.
* `polymarket-collector/tests/test_rules_deals.py` — נוספו בדיקות גבולות, ימים, timezone, חצות, יום מלא, רשומה מושבתת, רענון DB ושינוי רגע לפני INSERT.
* `RULE_INACTIVE_SCHEDULE_BUG_REPORT.md` — דוח זה.

עסקאות פתוחות מטופלות קודם ב־`process_demo_exits`; השער החדש נקרא רק בכניסה ולכן Stop Loss, Take Profit וסגירת Event ממשיכים לעבוד.

## 4. התנהגות לפני ואחרי

| מצב | לפני התיקון | אחרי התיקון |
| --- | --- | --- |
| חוק בתוך טווח חוסר פעילות | טווח שהושבת בטעות לא חסם; ללא שער סופי | נחסם בשרת בשער מוקדם וסופי |
| חוק מחוץ לטווח | פעיל | פעיל |
| עסקה פתוחה בתחילת הטווח | המשיכה להתנהל | ממשיכה להתנהל, כולל SL/TP/Event |
| טווח שחוצה חצות | נתמך בבדיקה מוקדמת | נתמך ונבדק בשער המרכזי |
| שינוי לוח זמנים דרך UI | סטטוס עמום | משמעות Enabled/Disabled מפורשת; קריאה סופית מה־DB ללא cache |

## 5. בדיקות שבוצעו

| פקודה/בדיקה | תרחיש | תוצאה |
| --- | --- | --- |
| שחזור read-only במסד TST | טווחי חוק 11 מול עסקאות שנפתחו | PASS — התקלה שוחזרה |
| `pytest tests/test_rules_deals.py -q` | DEMO, API, SL/TP/Event ולוחות | PASS — 21 passed, 9 subtests |
| `pytest -q` | כלל בדיקות DEMO ו־LIVE | PASS — 62 passed, 9 subtests |
| `test_inactive_schedule_gate_boundaries_days_status_and_refresh` | גבולות, ימים, חצות, יום מלא, disabled ורענון | PASS |
| `test_final_gate_reloads_schedule_before_insert` | שינוי schedule ממש לפני INSERT | PASS — 0 עסקאות |
| `test_inactive_window_crossing_midnight_blocks_only_new_entries` | חסימה וחזרה אוטומטית | PASS |
| בדיקת `py_compile` | תחביר | PASS |

פלט מרכזי: `62 passed, 7 warnings, 9 subtests passed`. האזהרות הן deprecation קיימות של FastAPI/Starlette.

## 6. בדיקות שרת TST

* Git commit: הקומיט המופיע בסעיף 9 ובתשובת המסירה.
* שירותים: מנוע DEMO וכל suite ה־LIVE במצב mock/fail-closed; לא נשלחה פקודת מסחר אמיתית.
* timezone שרת: `Etc/UTC`; timezone חוק: `Asia/Jerusalem`.
* מסד: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3` בקריאה בלבד לשחזור; התיקון נבדק במסדי SQLite זמניים.
* Restart: לא בוצע שינוי בשירות הרץ; תרחיש התיקון מבודד כדי למנוע מסחר אמיתי.
* תוצאה: לפני התיקון הוכחו עסקאות בתוך הטווח; לאחריו תנאי כניסה תואמים יצרו 0 עסקאות בזמן החסום ואפשרו כניסה לאחר הסיום.
* הוכחה: בדיקות `test_final_gate_reloads_schedule_before_insert` ו־`test_inactive_window_crossing_midnight_blocks_only_new_entries` עברו.

## 7. סיכונים שנותרו

מערכת LIVE משתמשת ב־`live_rules` ובמסד נפרדים ואינה כוללת כיום לוחות חוסר פעילות; היא נבדקה mock/fail-closed, אך לא אוחדו לתוכה לוחות DEMO כדי לא לשנות ארכיטקטורה ללא אפיון. אין כיום endpoint לעריכת חוק קיים. רשומות TST היסטוריות עם `status='inactive'` נשארות מושבתות במכוון; יש להפעיל רק טווחים שהמפעיל מאשר כחוסמים.

## 8. המלצות להמשך

* להציג "חסום זמנית" ואת הטווח המתאים בדאשבורד.
* להוסיף audit log לשינוי טווחים.
* להוסיף endpoint עריכה עם החלפה טרנזקציונית.
* לנטר כניסות שנופלות בדיעבד בתוך טווח מופעל.
* אם נדרש schedule ל־`live_rules`, להוסיף fail-closed לפני submission אמיתי.

## 9. פרטי Git

* Branch: `agent/live-auth-flow`.
* Commit: בתשובת המסירה וב־Git history; commit אינו יכול להפנות למזהה של עצמו מתוך תוכנו.
* הודעה: `Fix rule inactive schedule enforcement`.
* Repository: `dviry75/polymarket-collector`, branch `agent/live-auth-flow`.
* Push: מתועד בתשובת המסירה.
