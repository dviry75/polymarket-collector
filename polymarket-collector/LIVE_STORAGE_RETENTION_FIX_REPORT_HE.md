# דוח תיקון גידול האחסון — Polymarket Live

תאריך ביצוע: 2026-08-03 (UTC)  
ענף: agent/live-storage-retention  
סביבת יעד: polymarket-live.service ב־PAPER TRADING בלבד

## 1. תקציר מנהלים

התיקון הושלם, נבדק ונפרס בפועל. קצב גידול קובץ מסד הלייב ירד במדידה חיה של 10.91 דקות מכ־2.55GB ליום לכ־51.9MB ליום — ירידה של 97.96%.

המערכת ממשיכה לעבד כל הודעת Market WebSocket בזמן אמת. רק שמירת ההיסטוריה הוגבלה: אירועי raw כבויים כברירת מחדל, snapshots נשמרים לכל token לכל היותר פעם בשנייה ורק לאחר שינוי משמעותי, ו־snapshot עסקי שמוביל לפתיחה/סגירה של עסקת דמו נשמר בכפייה לפני הפעולה.

שני השירותים פעילים, שני חיבורי ה־WebSocket מחוברים, health מקומי וציבורי מחזיר 200, המסחר האמיתי כבוי וה־Kill Switch נשאר true.

## 2. חומרת הבעיה לפני התיקון

באירוע האחסון הראשוני קובץ poly_live.sqlite3 הגיע לכ־8.76GB והדיסק הראשי התקרב ל־99%. בנוסף, /tmp היה מלא עקב חמישה קובצי openpyxl ישנים בהיקף של כ־980MB.

לאחר ניקוי החירום שבוצע לבקשת המשתמש, אך לפני תיקון הקוד, נמדד שוב קצב גידול של 31,498,240 בתים ב־1,068 שניות: כ־29,493 בתים בשנייה, או כ־2.55GB ליום. בקצב זה ההגעה ל־80% הייתה צפויה בתוך כ־1.9 ימים ול־90% בתוך כ־2.7 ימים לפי מצב הדיסק הנוכחי.

## 3. שורש הבעיה שנמצא בפועל

ה־WebSocket עצמו אינו צורך אחסון משמעותי. הבעיה הייתה שרשרת כתיבות לכל הודעה:

- שמירת ההודעה הגולמית ב־live_websocket_events.
- יצירת snapshot אחד או יותר ב־live_market_snapshots.
- כתיבת state של last_message_at כמעט לכל הודעה.
- יצירת audit טכני עבור שינויי health/state שגרתיים.
- היעדר retention אוטומטי.
- Excel ב־write-only mode יצר קובצי openpyxl ב־/tmp; כאשר export ננטש או נכשל, לא היה workspace ייעודי שמבטיח ניקוי.
- נעילת SQLite זמנית במסלול health/reconnect הייתה יכולה להפיל את task של ה־WebSocket במקום לאפשר התאוששות.

לא נמצאה הצדקה עסקית לשמירת כל הודעה גולמית או כל snapshot, אך מנוע החוקים כן זקוק לכל העדכונים בזיכרון.

## 4. זרימת השמירה לפני ואחרי

לפני:

WebSocket → raw event DB → snapshot(s) DB → state/audit DB → מנוע חוקים

אחרי:

WebSocket → normalization בזיכרון → מנוע החוקים בכל הודעה

ובמסלול נפרד:

מצב שוק שהשתנה + חלפה שנייה → snapshot מצומצם DB

אם מתקבלת החלטה עסקית לפתוח או לסגור עסקת PAPER, ה־snapshot המדויק נשמר בכפייה ורק לאחר מכן נרשמות evaluation/deal. כך תדירות העיבוד אינה תלויה בתדירות השמירה.

## 5. קבצים ורכיבים ששונו

- .env.example — משתני storage/retention בטוחים.
- app.py — workspace ייעודי, lock וניקוי export של המערכת הראשית.
- live/config.py — הגדרות ו־validation.
- live/market_websocket.py — הפרדת עיבוד משמירה, סינון technical/raw, counters, throttle ל־health ו־reconnect עמיד לנעילות.
- live/paper_trading.py — cache עדכני בזיכרון ו־force persistence לאירועים עסקיים.
- live/repository.py — throttle per token, fingerprint יציב, audit category, indexes ו־SQLite busy timeout.
- live/retention.py — מנגנון retention חדש.
- live/router.py — health/preview מאומתים ו־Excel export בטוח.
- live_app.py — task מתוזמן ו־public storage health מצומצם.
- בדיקות: tests/test_live_storage_retention.py, tests/test_live_system.py, tests/test_coinbase_volume.py.

שינוי קיים בשרת שמפנה משתמש לא מחובר אל /live/login נשמר ולא נדרס. שינויים מקומיים אחרים שאינם קשורים למשימה לא נכללו ב־commit.

## 6. שינויים בסכימת בסיס הנתונים

נוסף ל־live_audit_log השדה category TEXT NOT NULL DEFAULT UNCLASSIFIED.

ברירת המחדל UNCLASSIFIED מכוונת ושמרנית: רשומה ישנה או לא מסווגת לעולם אינה נמחקת אוטומטית.

נוספו indexes לזמן ב־live_websocket_events וב־live_market_snapshots; ל־category וזמן ב־live_audit_log; ל־market_snapshot_id ב־live_rule_evaluations; ול־entry_snapshot_id ו־exit_snapshot_id ב־live_deals.

EXPLAIN QUERY PLAN בייצור אישר שימוש בכל האינדקסים הרלוונטיים, לרבות MULTI-INDEX OR עבור snapshots שמופנים מעסקאות.

## 7. מדיניות ה־retention הסופית

| סוג מידע | ברירת מחדל | התנהגות |
|---|---:|---|
| WebSocket raw | 48 שעות | raw כבוי כברירת מחדל; אם יופעל, יש retention |
| Market snapshots | 30 ימים | מחיקה רק אם ישנים ואינם מופנים מ־evaluation או deal |
| Audit מסוג TECHNICAL | 14 ימים | מחיקה ב־batches |
| BUSINESS / ADMIN / TRADING / SYSTEM | ללא מחיקה | נשמר |
| UNCLASSIFIED | ללא מחיקה | נשמר |
| rules, deals, orders, fills, positions | ללא מחיקה | נשמר |

ה־retention רץ מיד בעליית השירות ולאחר מכן כל שעה, ב־batches של 2,000. הוא idempotent, אינו רץ במקביל לעצמו, משחרר lock גם אם כתיבת health נכשלת, וכשל בו אינו מפיל את השירות.

## 8. אילו אירועים נשמרים ואילו לא

ממשיכים להישמר:

- פעולות חוק ומנהל.
- פתיחה וסגירה של עסקה.
- orders, cancels, fills ו־positions.
- Stop Loss, Take Profit וסיום אירוע.
- שינוי הגדרות, Kill Switch ושגיאות משמעותיות.
- שינוי חיבור משמעותי.
- snapshot מדוגם שהשתנה.
- snapshot מדויק שגרם להחלטה עסקית.

אינם נשמרים כהיסטוריה רגילה:

- heartbeat, ping/pong ו־subscription טכני.
- raw market message שגרתי כאשר LIVE_WS_RAW_EVENTS_ENABLED=false.
- snapshot זהה או snapshot נוסף לאותו market/token בתוך חלון השנייה.
- עדכון מחיר שגרתי כ־audit.
- last_seen, timestamp או health שגרתי כ־audit.

## 9. שמירת תגובת החוקים בזמן אמת

כל הודעה מנורמלת ונשלחת ל־PaperTradingEngine.process_snapshot, גם אם לא נשמרה למסד. המנוע מחזיק את מצב ה־token האחרון בזיכרון ומשתמש בו להשוואת שני צדי השוק.

כאשר אות transient גורם ל־OPEN, Stop Loss, Take Profit או סגירה, המנוע שומר את ה־snapshot העסקי בכפייה לפני יצירת evaluation/deal. בדיקת regression מוכיחה שאות כניסה שמגיע בתוך חלון ה־throttle עדיין פותח עסקת דמו.

## 10. הטיפול ב־Audit

Audit קיבל category מפורש: BUSINESS, ADMIN, TRADING, SYSTEM, TECHNICAL ו־UNCLASSIFIED.

עדכון state זהה אינו כותב row חדש, timestamp/health שגרתי אינו יוצר audit, ו־health של Market WS נכתב לכל היותר פעם בשנייה. retention מוחק רק category השווה במפורש ל־TECHNICAL; הוא אינו משתמש בתנאי שלילה ולכן רשומות לא מסווגות מוגנות.

## 11. Excel ו־/tmp

שני מסלולי ה־Excel משתמשים כעת ב־TemporaryDirectory ייעודי בתוך תיקיית output, משנים את tempfile.tempdir רק בתוך ה־export, סוגרים workbook ב־finally, ומבצעים atomic rename לקובץ הסופי.

נוסף lock שמונע exports כבדים מקבילים. שמות workspace ייחודיים; ניקוי stale מוגבל רק ל־prefix של המערכת ולפריטים ישנים מ־24 שעות.

לפני המחיקה נבדקו חמשת קובצי openpyxl: ownership, timestamps והיעדר file handles פתוחים. נמחקו רק אותם קבצים. שוחררו כ־978MB ב־/tmp; כעת /tmp בשימוש 1% (כ־3.7MB) ואין קובצי openpyxl.

בדיקת export חיה יצרה קובץ תקין של 11,945,937 בתים, 14 sheets ו־91,406 rows. הקובץ נפתח באמצעות openpyxl, נסגר ונמחק; מספר שאריות ה־workspace היה אפס.

## 12. הגיבוי ואימותו

נוצר גיבוי עקבי כאשר שירות הלייב עצור:

/opt/polymarket-btc-live/backups/storage-fix-20260803T090100Z/poly_live.sqlite3

- זמן: 2026-08-03 09:00 UTC.
- גודל: 175,435,776 בתים.
- SHA-256: 990ddcb333daa79f709be0eec76af485f3aa2db3e9e8305cbd98e91bedee342b.
- PRAGMA integrity_check: ok.
- הגיבוי נפתח בהצלחה.
- נבדקו counts קריטיים: rule אחד נשמר; deals/orders/fills/positions היו אפס ולא נמחקו.
- הגיבוי נמצא מחוץ ל־Git. נפח תיקיית הגיבויים כעת כ־227MB.

ניסיון ראשון לבצע SQLite Backup API בזמן כתיבה רציפה לא התקדם; התהליך הספציפי הופסק והקובץ החלקי והלא־תקין הוסר. לאחר מכן בוצע הגיבוי העקבי בחלון עצירה מבוקר.

## 13. Cleanup למסד

כן. בניקוי החירום שאושר קודם על ידי המשתמש הוסרו נתוני telemetry חיים עתירי נפח והקובץ ירד מכ־8.76GB לכ־119MB. לא נמחקו rules, deals, orders, fills או positions.

לאחר פריסת ה־retention, preview החזיר אפס מועמדים בכל שלוש הקטגוריות, מפני שהמידע הישן כבר נוקה והמידע הנוכחי חדש מה־cutoff. לכן לא בוצעה מחיקה נוספת ומיותרת.

## 14. WAL checkpoint

מסד הייצור משתמש ב־journal_mode=delete; אין קובץ WAL פעיל. PRAGMA wal_checkpoint(PASSIVE) החזיר (0, -1, -1), כלומר checkpoint אינו רלוונטי למצב journal זה. WAL בגודל 0 תועד גם בתחילת וגם בסוף המדידה.

## 15. VACUUM

VACUUM בוצע בניקוי החירום המבוקר שקדם לפריסה, לאחר עצירת writes ובדיקות בטיחות, ולכן קובץ ה־8.76GB אכן הצטמצם פיזית. בפריסה הנוכחית לא הורץ VACUUM נוסף: אין freelist פנוי בסוף המדידה, אין מחיקה חדשה שמצדיקה אותו, והרצה נוספת הייתה מוסיפה סיכון והשבתה ללא תועלת.

## 16. נפחי דיסק, DB, WAL ו־/tmp

| נקודה | DB | WAL | דיסק ראשי | /tmp |
|---|---:|---:|---:|---:|
| אירוע האחסון | כ־8.76GB | לא מהותי | קרוב ל־99% | 100%, כ־980MB openpyxl |
| אחרי ניקוי חירום | כ־119MB | 0 | כ־55%, כ־8.4GB שוחררו | 1%, כ־978MB שוחררו |
| T0 מדידה סופית | 180,137,984 B | 0 | כ־55.37% | 1% |
| T1 אחרי 10.91 דקות | 180,531,200 B | 0 | 55.38% | 1%, 3.7MB |

ה־DB גדל ב־393,216 בתים בחלון. תנודות כלל־מערכת בדיסק כוללות שירותים אחרים, ולכן תחזית הקיבולת מבוססת על קצב קובץ ה־DB ולא על sample קצר של כל filesystem.

## 17. מספר רשומות לפני ואחרי

מדידת קוד לפני התיקון, 08:54:47 UTC:

- live_websocket_events: 10,081.
- live_market_snapshots: 35,513.
- live_audit_log: 20,577.
- live_rule_evaluations: 16,179.

חלון המדידה הסופי:

| טבלה | T0 | T1 | תוספת |
|---|---:|---:|---:|
| live_websocket_events | 11,666 | 11,666 | 0 |
| live_market_snapshots | 39,412 | 39,787 | 375 |
| live_audit_log | 23,846 | 23,850 | 4 TECHNICAL |
| live_rule_evaluations | 16,179 | 16,179 | 0 |
| live_deals | 0 | 0 | 0 |
| live_orders | 0 | 0 | 0 |
| live_order_fills | 0 | 0 | 0 |
| live_positions | 0 | 0 | 0 |

ארבע רשומות ה־audit הן שינויי חיבור משמעותיים/מעברי שוק, לא heartbeat או מחיר שגרתי.

## 18. קצב יצירת רשומות לפני ואחרי

| מדד | לפני | אחרי | שינוי |
|---|---:|---:|---:|
| הודעות WebSocket שנכנסו | לא היה counter אמין | 1,046.97 לדקה | כל ההודעות עובדו |
| raw WebSocket rows | 355.0 לדקה | 0 לדקה | ירידה 100% |
| snapshot candidates למנוע | לא נמדד בנפרד | 2,021.59 לדקה | עיבוד בזמן אמת |
| snapshots שנשמרו | 680.0 לדקה | 34.38 לדקה | ירידה 94.94% |
| audit rows | 721.46 לדקה | 0.37 לדקה | ירידה 99.95% |

יחס הודעות נכנסות ל־snapshot שנשמר: 30.45:1.  
יחס snapshot candidates שעובדו ל־snapshot שנשמר: 58.80:1.

## 19. קצב גידול יומי ותחזית קיבולת

- לפני: כ־2.55GB ליום.
- אחרי: כ־51.9MB ליום (0.048GiB ליום).
- ירידה בפועל: 97.96%.

לפי נפח filesystem של 19,594,608,640 בתים ושימוש של 10,851,225,600 בתים בסוף המדידה:

- זמן משוער ל־80%: כ־92.9 ימים.
- זמן משוער ל־90%: כ־130.7 ימים.
- זמן משוער למילוי מלא: כ־168.4 ימים.

זו אקסטרפולציה מחלון של 10.91 דקות. היא טובה להשוואת לפני/אחרי, אך תחזית שדרוג אמינה יותר דורשת sample נוסף לאחר 24 שעות.

## 20. תוצאות הבדיקות

- כל המערך: 88 passed.
- תתי־בדיקות: 9 passed.
- warnings: 7 אזהרות deprecation קיימות של FastAPI/Starlette; אין כשל.
- py_compile לכל קובצי האפליקציה והלייב: עבר.
- git diff --check: עבר.
- בדיקות WebSocket/snapshot: realtime לכל הודעה, throttle per token, שינוי/זהות, heartbeat, concurrency ו־force persistence — עברו.
- בדיקות audit ו־retention: category שמרני, cutoffs, batching, idempotence, מניעת ריצה מקבילה, שחרור lock בכשל ושמירת business data — עברו.
- בדיקות Excel: הצלחה, exception cleanup, workbook close, workspace cleanup ו־concurrency — עברו.
- סריקת staged files: אין DB, backup, export, .env, log או secret ב־commit.

## 21. בדיקות לייב לאחר הפריסה

- health מקומי ללייב: 200.
- health מקומי למערכת הראשית: 200.
- health דרך https://live-poly.dvirtechnologies.com/health: 200 ו־status ok.
- מסך login ציבורי: 200.
- retention preview מאומת: 200.
- export חי: תקין, 14 sheets, 91,406 rows, ללא leftovers.
- PRAGMA quick_check: ok.
- תוכניות retention משתמשות באינדקסים.
- אין exception חוזרת בלוגים מאז העלייה הסופית.

## 22. מצב השירות וה־WebSocket

- polymarket-live.service: active.
- polymarket.service: active.
- Market WS בסוף המדידה: CONNECTED, לא stale, error ריק.
- User WS: CONNECTED, לא stale, error ריק.
- Market WS עיבד בחלון 11,419 הודעות ו־22,049 snapshot candidates.
- נרשמו שני reconnects לאורך 10.91 דקות עקב rotation מתוכנן של שוקי BTC 5m; החיבור היה CONNECTED בסיום, ללא error וללא reconnect loop.
- מנוע PAPER המשיך לקבל כל עדכון. לא הייתה הפעלה של מסחר אמיתי.

## 23. משתני הסביבה החדשים

| משתנה | ברירת מחדל |
|---|---|
| LIVE_WS_RAW_EVENTS_ENABLED | false |
| LIVE_WS_EVENT_RETENTION_HOURS | 48 |
| LIVE_SNAPSHOT_MIN_INTERVAL_MS | 1000 |
| LIVE_SNAPSHOT_SAVE_ONLY_ON_CHANGE | true |
| LIVE_SNAPSHOT_RAW_PAYLOAD_ENABLED | false |
| LIVE_SNAPSHOT_RETENTION_DAYS | 30 |
| LIVE_TECHNICAL_AUDIT_RETENTION_DAYS | 14 |
| LIVE_RETENTION_INTERVAL_SECONDS | 3600 |
| LIVE_RETENTION_BATCH_SIZE | 2000 |
| LIVE_DISK_WARNING_PERCENT | 70 |
| LIVE_DISK_CRITICAL_PERCENT | 80 |
| LIVE_DISK_EMERGENCY_PERCENT | 90 |

הערכים הוחלו בקובץ environment של השירות בלי להדפיס או להכניס את תוכנו ל־Git.

## 24. תוכנית Rollback

1. לעצור את polymarket-live.service בחלון מבוקר.
2. לשמור עותק נוסף של מסד המצב הנוכחי.
3. להחזיר את קוד הלייב ל־baseline 3b75eb019ee8d77a1bba29522e3798466072fd1f, תוך שמירת התאמת redirect ל־login הקיימת בשרת.
4. אם rollback של DB נדרש, לאמת שוב את הגיבוי storage-fix-20260803T090100Z ולהחליף את DB רק כאשר כל ה־writes עצורים.
5. משתני ה־environment החדשים בטוחים ואינם מפעילים מסחר; קוד ישן שאינו מכיר אותם יתעלם מהם.
6. להפעיל את השירות, לבדוק health, integrity, שני WebSockets ומצב Kill Switch.

אין לבצע rollback של DB באופן שידרוס עסקאות חדשות בלי גיבוי נוסף והשוואת נתונים.

## 25. בעיות או מגבלות שלא נפתרו

- אין אינטגרציית alert חיצונית מוגדרת; ההתראות קיימות ב־health וב־journald בלבד. לא הומצא Slack/Email ללא ערוץ מאושר.
- חלון של 10.91 דקות אינו תחליף למדידה של 24 שעות.
- latency_ms ב־snapshots החדשים היה בממוצע כ־113.9 שניות ומקסימום כ־262.1 שניות, אך זהו גיל timestamp שמגיע מהמקור ולא זמן העיבוד של המנוע. אין כרגע instrumentation נפרד ל־end-to-end rule evaluation latency, ולכן לא נטען שיפור/הרעה מספריים במדד זה.
- קיימות שבע אזהרות deprecation של FastAPI/Starlette; הן אינן קשורות לאחסון ולא גרמו לכשל.
- במהלך בדיקת export/migration מקביל התגלתה נעילת SQLite זמנית. בעקבותיה נוספו busy timeout ומסלולי reconnect/health שאינם נופלים מנעילה; הבדיקות והחלון הסופי מאשרים התאוששות ויציבות.
- כלי apply_patch נכשל בסביבת Codex עקב bwrap: loopback: Failed RTM_NEWADDR; השינויים בוצעו באמצעות החלפות מדויקות ומאומתות, ולא הייתה לכך השפעה על השרת או על הקוד הסופי.

## 26. המלצות להמשך

- לבצע מדידת T0/T1 נוספת לאחר 24 שעות ולחשב שוב DB bytes, row deltas ו־filesystem usage.
- להגדיר התראה חיצונית אמיתית לספי 70/80/90 דרך מערכת הניטור הארגונית.
- לעקוב במיוחד אחרי מספר snapshots לדקה; בקונפיגורציה הנוכחית הוא אמור להישאר בסדר גודל של עשרות ולא מאות.
- לא להפעיל raw events לאורך זמן. אם נדרש debug, להפעיל לזמן מוגבל ולוודא retention של 48 שעות.
- לשקול instrumentation ייעודי לזמן עיבוד מנוע החוקים, בנפרד מ־timestamp המקור.
- לבדוק את אזהרות FastAPI/Starlette במשימה נפרדת.

## 27. Commit hash

Commit המימוש:

c2bc5c921657ed42bf3470bea6c6a644b28a66d2

קישור: https://github.com/dviry75/polymarket-collector/commit/c2bc5c921657ed42bf3470bea6c6a644b28a66d2

הדוח עצמו נוסף ב־commit תיעוד נפרד באותו PR, כדי שה־commit של המימוש יוכל להופיע בתוך הדוח ללא תלות מעגלית.

## 28. קישורי GitHub

- Draft Pull Request: https://github.com/dviry75/polymarket-collector/pull/5
- ענף: https://github.com/dviry75/polymarket-collector/tree/agent/live-storage-retention
- קישור ישיר לדוח: https://github.com/dviry75/polymarket-collector/blob/agent/live-storage-retention/polymarket-collector/LIVE_STORAGE_RETENTION_FIX_REPORT_HE.md

ה־PR נשאר Draft ולא בוצע merge.
