# דוח סגירה סופי — לוחות חוסר פעילות של חוקים

## 1. תקציר מנהלים

התקלה גרמה לחלונות שנראו למשתמש כחלונות חוסר פעילות לא לחסום כניסות, משום שה־UI הציג `active/inactive` ללא הסבר ו־14 חלונות חוק 11 נשמרו כ־`inactive` — כלומר החלון עצמו מושבת. התיקון הקודם הוסיף שער שרת מרכזי, אך עדיין לא היה ב־main הפעיל, לא השתמש בזמן ההחלטה הסופי, לא כלל עריכת חוק ולא נפרס לשירות. בסגירה זו התיקון הובא ל־main, הושלם, נבדק, גובה המסד, הופעלו חלונות חוק 11, השירות הופעל מחדש ובוצע E2E מול השירות והמסד הפעילים. התקלה נסגרה בפועל ב־TST עבור מנוע DEMO.

## 2. סביבת השרת

* Hostname: `polymarket-btc-tst`.
* timezone שרת: `Etc/UTC`; השעון מסונכרן ב־NTP.
* משתמש: `dvir`.
* נתיב פרויקט פעיל: `/opt/polymarket-btc`.
* שירות: `polymarket.service`.
* WorkingDirectory: `/opt/polymarket-btc/polymarket-collector`.
* ExecStart: `/opt/polymarket-btc/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000`.
* Python: `/opt/polymarket-btc/.venv/bin/python3` (ה־exe של התהליך הוא `/usr/bin/python3.13`).
* EnvironmentFile: `/opt/polymarket-btc/.env`; לא הודפסו ערכים רגישים.
* מסד פעיל: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3`, בבעלות `dvir:dvir`, הרשאה `0644`.
* ה־health החזיר את אותו נתיב DB, ולכן ה־UI והמנוע משתמשים במסד שנבדק.

## 3. מצב Git

* הענף המקורי של התיקון: `agent/live-auth-flow`, commit `4ae19cba14b3e0e322ea319d5d7b3fa9a28be3fb`.
* ענף הפריסה והשירות: `main` ב־checkout `/opt/polymarket-btc`.
* main לפני העבודה: `3f02c043a8f004fe021b07723b0fc17b83379d79`.
* ה־cherry-pick הבטוח של התיקון הקודם ב־main: `f4df4b6`.
* commit הקוד החדש: `8b7a71689b349ce383f35ce25106ca66d7d70d87`.
* Git SHA שרץ לאחר Restart: `8b7a71689b349ce383f35ce25106ca66d7d70d87` — זה היה HEAD של אותו WorkingDirectory בעת ה־Restart.
* `origin/main` אומת באותו SHA לפני ה־Restart.
* התיקון נמצא ב־main ונעשה Push ללא force.

## 4. תיקוני הקוד

* הסינון המוקדם והבדיקה הסופית משתמשים ב־`now_utc()`, שמחזירה datetime מודע ל־UTC וניתנת ל־mock.
* זמן דגימת Orderbook נשאר זמן נתון השוק ואינו משמש עוד כהכרעה סופית של schedule.
* `can_rule_open_new_deal` טוענת מחדש מה־DB את החוק והחלונות הפעילים מיד לפני INSERT.
* הבדיקה הסופית וה־INSERT נעשים בתוך ה־`BEGIN IMMEDIATE` הקיים במחזור Orderbook.
* הלוג כולל reason, rule_id, name, decision_time, timezone, day_of_week והטווח שהתאים.
* `PUT /rules/{rule_id}` נוסף לעדכון חוק וחלונות בהחלפה טרנזקציונית מלאה; כשל INSERT מחזיר rollback מלא.
* נוספה ולידציית timezone IANA, יום 0–6/`all`, שעה, status וכפילויות מדויקות.
* ה־UI כולל Edit Rule, טוען חלונות קיימים, מוסיף/מסיר חלונות ושומר ב־PUT.
* ברירת מחדל של חלון חדש היא `active`, והטקסט הוא `Enabled — blocks new entries` / `Disabled — does not block`.
* קבצים ששונו: `polymarket-collector/app.py`, `polymarket-collector/tests/test_rules_deals.py`, שני דוחות ה־Markdown בשורש.

## 5. תיקון חוק 11

| שדה | לפני | אחרי |
| --- | --- | --- |
| Rule ID | 11 | 11 |
| שם החוק | רובוט משוגע ניסיון | רובוט משוגע ניסיון |
| סטטוס החוק | active | active |
| Timezone | Asia/Jerusalem | Asia/Jerusalem |
| מספר חלונות | 14 | 14 |
| סטטוס החלונות | 14 inactive, 0 active | 0 inactive, 14 active |

החלונות לאחר העדכון, לכל יום 0–6:

* `00:00:00–16:00:00`, status `active`.
* `23:30:00–23:59:59`, status `active`.

העדכון שינה 14 רשומות של `rule_id=11` בלבד. ספירות שאר החוקים נשארו ללא שינוי: לפני ואחרי `active=5`, `inactive=28`. עסקאות היסטוריות לא נמחקו.

## 6. גיבוי מסד הנתונים

* מקור: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3`.
* גיבוי: `/home/dvir/tst-db-backups/poly_data_before_rule_schedule_fix_20260730_040606.sqlite3`.
* זמן: `2026-07-30 04:06:21 UTC`.
* גודל: `382,877,696` bytes.
* הרשאה: `0600`, מחוץ ל־Git.
* SQLite Backup API שימש בזמן שהשירות פעיל.
* `PRAGMA integrity_check`: `ok`.
* הגיבוי מכיל 11 חוקים ו־14 חלונות לחוק 11.
* שחזור מלא: לעצור `polymarket.service`, לשמור עותק של המסד הנוכחי, לשחזר באמצעות SQLite Backup API מהגיבוי לנתיב הפעיל, לוודא `integrity_check=ok`, להפעיל את השירות ולבדוק `/health`. שחזור מלא יחזיר גם נתוני שוק/עסקאות לנקודת 04:06:21; ל־rollback ממוקד עדיף להשבית רק את 14 החלונות בטרנזקציה.

## 7. בדיקות אוטומטיות

| פקודה | תוצאה | פלט מרכזי |
| --- | --- | --- |
| `/opt/polymarket-btc/.venv/bin/python -m pytest tests/test_rules_deals.py -q` | PASS | `24 passed, 9 subtests passed` |
| suite של כל קובצי הבדיקה העקובים ב־Git | PASS | `64 passed, 9 subtests passed` |
| `/opt/polymarket-btc/.venv/bin/python -m py_compile app.py` | PASS | ללא פלט שגיאה |
| `git diff --check` | PASS | ללא שגיאות whitespace |

נותרו 7 אזהרות deprecation קיימות של FastAPI/Starlette. הרצה מילולית של `pytest -q` אוספת גם קובץ מפעיל untracked בשם `tests/test_polymarket_adapter.py`; ארבע בדיקות בו נכשלות מול API LIVE שלא קיים ב־main. הקובץ אינו חלק מהריפו, לא שונה ולא נכלל ב־commit. כל 64 הבדיקות הקיימות והחדשות העקובות ב־Git עברו.

בדיקות הרגרסיה מכסות: active/inactive, Enabled/Disabled, start/end, חצות, יום מלא, ראשון/שני, Asia/Jerusalem, רענון DB, ללא cache, ברירת מחדל active, PUT, מחיקה, rollback, race של schedule, sampled-before/decision-inside, sampled-inside/decision-after, SL, TP וסגירת Event.

## 8. פריסת השירות

* PID לפני: `120709`.
* PID אחרי: `225307`.
* Restart: `2026-07-30 04:07:39 UTC`.
* מצב: `active (running)`; process יחיד על port 8000.
* startup: `Application startup complete`, ללא שגיאות DB או migration.
* WorkingDirectory ו־ExecStart תואמים לקוד שנבדק.
* Health מקומי: HTTP 200, `{"ok":true,...}`, עם נתיב DB הפעיל.
* `/rules`: HTTP 200 והחזיר את 14 חלונות חוק 11 כ־active.

## 9. בדיקת חוק 11

הבדיקה קראה את אותו `app.py` ואת המסד הפעיל:

* בתוך: `2026-07-30T05:00:00+03:00` — `allowed=False`, reason `rule_in_inactive_schedule`.
* בדיוק start: `2026-07-30T00:00:00+03:00` — חסום.
* בדיוק end: `2026-07-30T16:00:00+03:00` — schedule אינו תואם, `allowed=True`.
* מחוץ: `2026-07-30T17:00:00+03:00` — `allowed=True`, reason ריק.
* start מאוחר: `23:30:00` — חסום; end `23:59:59` — מותר.
* אין לחוק 11 טווח שחוצה חצות; התרחיש מכוסה בבדיקות האוטומטיות.
* לחוק לא הייתה עסקה פתוחה בזמן בדיקת השער.

בנוסף, לאחר ה־Restart השירות עצמו כתב לוג מחזור רגיל לחוק 11, למשל ב־04:07:51 UTC, עם reason, decision_time, timezone, יום וטווח `00:00:00-16:00:00`.

## 10. End-to-End

* שם: `__TST_INACTIVE_SCHEDULE_E2E_TEST__`.
* rule_id: `12`.
* בטיחות: DEMO, מכסת YES/NO שווה 0, ללא פקודת Polymarket.
* חלון: כל הימים, `00:00:00–00:00:00`, Enabled (יום מלא), Asia/Jerusalem.
* זמן תחילת תצפית: UTC `2026-07-30T04:08:32.290802+00:00`; מקומי `07:08:32+03:00`.
* Deals לפני: 0; אחרי 12 מחזורי Orderbook: 0.
* הוכחת לוג: ב־04:08:33, 04:08:35, 04:08:37 ו־04:08:39 הופיע `reason=rule_in_inactive_schedule`, rule_id=12, name, decision_time, timezone, day ו־window.
* לאחר PUT ששינה את כל שבעת החלונות ל־inactive, השער החזיר `allowed=True`, reason ריק, window None.
* שורת update נרשמה ב־04:09:01; עד ההשבתה ב־04:09:08 לא נרשם לוג schedule נוסף לחוק.
* ניקוי: החוק הושבת דרך endpoint; סטטוס סופי `inactive`; Deals סופיים 0. נשמרה רשומת חוק בדיקה מושבתת לצורכי audit, ולא נמחק נתון של חוק אחר.

## 11. עסקאות קיימות

הבדיקות האוטומטיות הוכיחו:

* Stop Loss ממשיך לפעול על Deal פתוח בזמן שהכניסות חסומות.
* Take Profit ממשיך לפעול.
* Event resolution ממשיך לסגור Deal.
* `process_demo_exits` רץ לפני `process_demo_entries`; שער schedule קיים רק במסלול יצירת Deal חדש.
* בדיקת deactivation/schedule הוכיחה שלא נוצרת כניסה נוספת ושעסקה פתוחה עדיין נסגרת.

## 12. LIVE

* שירות LIVE נפרד: `polymarket-live.service`, port 8001, מסד `/opt/polymarket-btc-live/poly_live.sqlite3`.
* Health: HTTP 200.
* `TRADING_MODE=DEMO`.
* `LIVE_ADAPTER=polymarket`, אך `LIVE_TRADING_ENABLED=false` ו־`LIVE_ORDER_SUBMISSION_ENABLED=false`.
* Kill Switch: `true` גם בקובץ ההגדרות וגם ב־`live_system_state`.
* `live_rules` הוא מודל נפרד ואינו תומך בלוחות חוסר פעילות; אין להציגו כתומך בהם.
* לפני מסחר אמיתי נדרש שער fail-closed מקביל ל־LIVE או איחוד מדיניות מפורש. במשימה זו לא הוסר Kill Switch ולא נשלחה כתיבה ל־Polymarket.

## 13. סיכונים שנותרו

* לוחות חוסר הפעילות אינם נאכפים בישות `live_rules`; מסחר LIVE ממילא כבוי ו־Kill Switch פעיל.
* קובץ בדיקה untracked של המפעיל נכשל מסיבות LIVE שאינן חלק מה־main; יש ליישר אותו עם ענף LIVE נפרד לפני שמצרפים אותו לריפו.
* לוג החסימה נכתב בכל מחזור עבור חוק חסום ועלול להיות רועש; מומלץ throttling עתידי.
* עריכת UI בוחרת חוק לפי prompt של ID; היא פונקציונלית אך ניתן לשפר לבחירה ישירה מהטבלה.

## 14. Rollback

1. קוד: לבצע `git revert 8b7a716` ולאחריו `git revert f4df4b6` לפי הצורך, לבצע Push רגיל ל־main — ללא reset/force push.
2. נתונים ממוקדים: ב־`BEGIN IMMEDIATE` לעדכן רק `rule_inactive_windows WHERE rule_id=11` חזרה ל־`inactive`, ואז commit ולבדוק ספירות.
3. שחזור מלא: לעצור `polymarket.service`; לגבות את המסד הנוכחי; לשחזר מהקובץ בסעיף 6 באמצעות SQLite Backup API; להריץ `PRAGMA integrity_check`; להפעיל מחדש.
4. Restart: `sudo systemctl restart polymarket.service`.
5. אימות: `systemctl status`, PID חדש, journal ללא שגיאות ו־`curl http://127.0.0.1:8000/health` שמחזיר HTTP 200.

## 15. פרטי Git

* Branch: `main`.
* Commit הבאת התיקון הקודם: `f4df4b6` — `Fix rule inactive schedule enforcement`.
* Commit קוד ובדיקות: `8b7a71689b349ce383f35ce25106ca66d7d70d87` — `Complete inactive schedule enforcement and editing`.
* Commit דוח: ה־commit שמוסיף קובץ זה, בהודעה `Add inactive schedule final closure report`; SHA מדויק מופיע ב־Git history ובמסירת הסיום (commit אינו יכול לכלול את מזהה עצמו בתוכנו).
* Push: commits הקוד אומתו ב־`origin/main`; commit הדוח יאומת לאחר יצירתו.
* staging בוצע בשמות קבצים מפורשים. `.env`, מסדי SQLite, WAL/SHM, גיבויים, credentials, logs וקבצים זמניים לא נכללו.
* לא בוצע Force Push.
