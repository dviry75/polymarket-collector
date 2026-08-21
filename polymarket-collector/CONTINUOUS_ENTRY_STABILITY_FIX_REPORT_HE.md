# דוח תיקון יציבות ורציפות כניסות — Polymarket Robot

תאריך ביצוע: 18 באוגוסט 2026  
סביבה: LIVE / REAL_TRADING  
שירות: `polymarket-trader.service`

## תקציר מנהלים

הרובוט לא הפסיק להיכנס בגלל מחסור באותות. אותות כניסה תקינים המשיכו להגיע, אך פוזיציית עבר במצב `DUST` נשארה עם `closed_at=NULL`. מנגנון הבטיחות פירש אותה כפוזיציה פעילה, ולכן חסם כל כניסה חדשה באמצעות `ACTIVE_ENTRY_SLOT_OCCUPIED`. לאחר תיקון זה נחשף חסם שני: reconciliation שתפס מצב ביניים תקין בזמן שליחת פקודה הפעיל pause ידני, שנשאר נעול גם לאחר שהחשבון חזר להתאמה מלאה.

תוקנו מחזור החיים של DUST סופי, רשומות עבר, יישור transient של ספר הפקודות, וההבחנה בין handoff חי לבין מצב לא־ידוע לאחר crash. שתי עסקאות LIVE חדשות בוצעו לאחר התיקונים. העסקה השנייה יצרה פער reconciliation קטן וצפוי, השתחררה אוטומטית לאחר מעבר נקי, ונסגרה בלי להשאיר intent, pause או סלוט תפוס.

> "כניסה רציפה" פירושה שהרובוט חופשי לבדוק ולהיכנס בכל חלון מסחר שבו כללי האסטרטגיה והבטיחות מאשרים כניסה. התיקון אינו מכריח עסקה ללא אות, אינו עוקף מגבלות סיכון ואינו מבטיח רווח או זמינות מוחלטת.

## סיבת השורש

1. העסקה האחרונה לפני התקלה, בשוק `btc-updown-5m-1787002500`, הסתיימה ב־`STOP_066` עם שארית לא־סחירה של `0.0057` מניות.
2. מצב הפוזיציה עודכן ל־`DUST`, אך `closed_at` נשאר ריק.
3. בדיקת סלוט הכניסה מתייחסת לכל `OPEN` או `DUST` ללא `closed_at` כפוזיציה פעילה, בכוונה בטיחותית.
4. כתוצאה מכך מאות אותות כניסה חוקיים נעצרו אחרי אישור האסטרטגיה, לפני יצירת intent, עם `ACTIVE_ENTRY_SLOT_OCCUPIED`.
5. בזמן העסקה הראשונה שאחרי התיקון, reconciliation ראה תיקון כמות זעיר של `0.0042` מניות ומאוחר יותר intent במצב `RESERVED` לפני קבלת remote order ID. שני המצבים היו זמניים, אך הראשון הפעיל `RECONCILIATION_GAP` לא־בר־שחרור.
6. גם אחרי עשרות reconciliation נקיים, ה־pause נשאר בבעלות `RECONCILIATION` עם `pause_auto_recoverable=false`, ולכן חסם את חלון השוק הבא.
7. אירועי `BOOK_NOT_READY` ו־`BEST_PRICE_PENDING_DEPTH` קצרים ממשיכים להיכשל סגור ולהשתחרר כשהספר חוזר למצב תקין; הם אינם משאירים pause קבוע.

## תיקוני קוד שבוצעו

### 1. סגירה נכונה של DUST סופי

ב־`live/strategy_repository.py`, פעולת `apply_exit_fill` קובעת כעת `closed_at` גם כאשר מצב הסיום הוא `DUST`, ולא רק ב־`CLOSED`. השינוי מתבצע רק אחרי fill של יציאה. פוזיציית DUST שנוצרה בזמן כניסה עדיין נשארת פתוחה וחוסמת כניסה, כנדרש לבטיחות.

### 2. תיקון בטוח לרשומות עבר

נוספה `repair_terminal_dust_slots()` שמתקנת רק רשומה העומדת בכל התנאים הבאים:

- מצב הפוזיציה `DUST` ו־`closed_at` ריק.
- הכמות הניתנת למכירה היא אפס.
- ה־deal המקביל כבר סופי, במצב `DUST`, וכולל זמן סגירה.
- אין intent לא־סופי המשויך לפוזיציה.

כל תיקון נכתב ל־audit ול־timeline. התיקון רץ רק לאחר reconciliation נקי בזמן עליית השירות; אם תוקנה רשומה, מתבצע reconciliation נוסף לפני הפעלת לולאות המסחר וה־WebSocket.

### 3. אבחון סלוט כניסה

נוספה `entry_slot_status()` והמידע שולב ב־health של מנוע האסטרטגיה. ניתן לראות האם הסלוט זמין, ואם לא — איזו פוזיציה, מצב ואירוע חוסמים אותו.

### 4. הקשחת יישור נתוני שוק בזמן כניסה

אות כניסה מדויק שכבר ננעל יכול היה להיעצר עקב `BEST_PRICE_PENDING_DEPTH` רגעי בין עדכוני WebSocket. נוסף grace מוגבל של 250ms:

- חל רק על trigger קריטי שכבר ננעל.
- ממתין רק לסיבות transient מוגדרות: `BEST_PRICE_PENDING_DEPTH`, `BOOK_NOT_READY`, `FRAME_SUPERSEDED`.
- חוסר התאמה, נתון ישן או ניתוק עדיין נכשלים מיד ובמצב fail-closed.
- גיל ה־trigger ממשיך להיות מוגבל; אין שימוש באות ישן.
- נוספו מוני waits, recoveries ו־timeouts ל־health.

### 5. שחרור בטוח של pause זמני מ־reconciliation

- reservation שנוצר באותו תהליך מסומן כ־handoff בזיכרון בלבד למשך עד 15 שניות. reconciliation מקביל אינו מסווג אותו כפער לפני שהבקשה הספיקה לקבל remote order ID.
- הסמן אינו נשמר במסד. אחרי crash או restart הוא נעלם, ולכן reservation ללא remote ID מזוהה מיד כמצב לא־ידוע ונשאר fail-closed.
- תיקון פוזיציה מסומן בר־שחרור אוטומטי רק אם הפוזיציה נוצרה ב־15 השניות האחרונות, אין יציאה מאושרת, והפרש הכמות אינו עולה על `0.01` מניה.
- פער ישן, פער מהותי, remote order לא מוכר או מצב `RECONCILIATION_REQUIRED` נשארים pause ידני.
- גם פער בר־שחרור משתחרר רק לאחר reconciliation נקי ורק אם WebSocket, freshness, readiness, heartbeat, kill switch ו־unresolved intents כולם נקיים.

## בדיקות שבוצעו

- בדיקות ממוקדות למסלול DUST, תיקון legacy, alignment grace, handoff חי, handoff שפג, פער קטן בר־שחרור, פער אמיתי ותרחישי crash/restart: עברו.
- כל חבילת הבדיקות לאחר התיקון הסופי: `268 passed`, בנוסף `9 subtests passed`; זמן ריצה `82.19s`.
- `python -m py_compile`: עבר.
- `git diff --check`: עבר ללא שגיאות whitespace.
- האזהרות היחידות בחבילה הן deprecation קיימות של FastAPI/Starlette; אין כשל פונקציונלי.

## גיבוי ופריסה

לפני שינוי המסד והפעלת הקוד נוצר גיבוי דחוס:

- קובץ: `/opt/polymarket-btc-live/backups/poly_live_20260818_224657.sqlite3.gz`
- SHA-256: `1d2a0f0c9c7a3c7b8c1f275cb534bdf7137d1d85ff9256e28eff1d87d413a222`
- בדיקת `gzip -t`: עברה.
- רשומת הגיבוי במסד: status `ok`.

לפני הפריסה הסופית נוצר ואומת snapshot נוסף:

- קובץ: `/opt/polymarket-btc-live/backups/poly_live_20260818_231335.sqlite3.gz`
- גודל: `264844512` בתים.
- SHA-256: `236b0e576f7eba8de5039ce837959d565261bf56dd628c71e13890ad88757a81`
- בדיקת `gzip -t`: עברה; status במסד: `ok`.
- מדיניות retention הסירה את הגיבוי הישן מ־17 באוגוסט ושמרה את שני snapshots החדשים.

השירות `polymarket-trader.service` הופעל מחדש בלבד. לאחר העלייה:

- startup reconciliation: תקין.
- תיקון רשומת ה־DUST הישנה: הושלם.
- post-repair reconciliation: תקין.
- kill switch: כבוי.
- pause entries: כבוי.
- user WebSocket: מחובר.
- market WebSocket: מחובר.
- reconciliation blocker: כבוי.

## הוכחת LIVE לאחר התיקון

מיד לאחר הפריסה נפתחה עסקה אמיתית חדשה בשוק `btc-updown-5m-1787093400`:

- ENTRY נוצר: `2026-08-18T22:53:04.010708+00:00`.
- ENTRY הסתיים `FILLED / MATCHED`: `2026-08-18T22:53:05.988421+00:00`.
- יציאת `STOP_066` הסתיימה `PARTIAL_FINAL / matched`: `2026-08-18T22:53:16.364827+00:00`.
- הפוזיציה הסתיימה `DUST`, עם שארית `0.0072`, sellable `0`, ו־`closed_at` שנקבע מיד.

מצב לאחר העסקה:

- unresolved intents: `0`.
- active slot positions: `0`.
- open DUST slots: `0`.

מכאן שסיבת החסימה הישנה תוקנה גם ברשומת העבר וגם במסלול העסקה החדשה.

לאחר הפריסה הסופית בוצעה עסקת LIVE שנייה בשוק `btc-updown-5m-1787095500`:

- החלטת ENTRY `ALLOWED / ENTRY_PRICE_EXACT`: `2026-08-18T23:28:22+00:00`.
- ENTRY הסתיים `FILLED / MATCHED`: `2026-08-18T23:28:24.005552+00:00`.
- יציאת `STOP_066` הסתיימה `PARTIAL_FINAL / matched`: `2026-08-18T23:28:30.925274+00:00`.
- פער זמני `remote_position_corrected_local` זוהה בזמן העסקה; הריצות הבאות היו נקיות וה־pause השתחרר אוטומטית.
- הפוזיציה הסתיימה `DUST` עם `closed_at` מיידי; לאחר מכן: unresolved intents `0`, active slot positions `0`, open DUST slots `0`.
- שלוש ריצות reconciliation הסופיות היו `ok / 0 gaps`, השירות נשאר `active`, ו־`pause_entries=false`.

## מה נשאר בכוונה ללא שינוי

- תנאי אות הכניסה והמחיר המדויק.
- מגבלות סכום, הפסד, מספר עסקאות וחשיפה.
- kill switch, pause, reconciliation, freshness ו־WebSocket safety gates.
- שעות וימי המסחר המוגדרים במערכת.
- מנגנון יציאה, take-profit ו־stop-loss.

כלומר, הרובוט לא ייכנס "בכל מחיר"; הוא יוכל להיכנס ברצף כאשר קיים אות תקין וכל שערי הבטיחות מאשרים זאת.

## פעולות המשך מומלצות ליציבות תפעולית

1. אחסון הגיבויים הגיע לכ־97% לאחר ה־snapshot הסופי. יש להעביר גיבויים ישנים לאחסון חיצוני או להגדיל מכסה בהקדם, לאחר אימות מדיניות השחזור.
2. מסד הנתונים החי הוא בערך 2.09GB. מומלץ להגדיר ארכוב תקופתי לטבלאות telemetry/audit הגדולות ולנטר קצב גידול.
3. להגדיר התראה אם `entry_slot.available=false` נמשך יותר מחלון שוק אחד, או אם קיימת `DUST + closed_at=NULL` לאחר reconciliation.
4. להציג בדשבורד את `entry_slot`, מוני alignment grace ואת סיבת שער הכניסה האחרונה.
5. לבצע תרגיל שחזור תקופתי מהגיבוי, לא רק בדיקת gzip.
6. להעביר בהמשך את hooks של FastAPI מ־`on_event` ל־lifespan כדי להסיר חוב deprecation לפני שדרוג תלויות.

## קבצים ששונו במסגרת תיקון זה

- `live/strategy_repository.py`
- `live/strategy_runtime.py`
- `live/reconciliation.py`
- `trader_app.py`
- `tests/test_live_full_strategy.py`
- `tests/test_pause_recovery.py`

בתיקיית העבודה קיימים שינויים וקבצים נוספים שהיו שם לפני תיקון זה; הם לא נדרסו ולא נכללים בהצהרת השינויים לעיל.
