# דוח מסירה סופי — מנגנון Auto Recovery

תאריך rollout: 20.08.2026 (UTC)

## תוצאה

מנגנון ההחייאה שוכתב כמכונת מצבים fail-closed ונפרס ל־LIVE. נכון לאימות הסופי השירות `polymarket-trader.service` פעיל, `pause_entries=false`, המצב `TRADING`, מנגנון ההחייאה `HEALTHY`, ה־strategy וה־reconciliation במצב `READY`, ה־kill switch כבוי, ואין intents לא פתורים או פוזיציות פעילות.

לא בוצעה פתיחה ידנית של pause ולא בוצע שינוי SQL כדי לכפות חזרה למסחר. ההחייאה שנצפתה ב־rollout בוצעה דרך המנגנון עצמו.

## מודל המצבים

- `TRADING`: אין pause עמיד. כל שערי הכניסה עדיין נבדקים בכל החלטת כניסה.
- `PAUSED_RECOVERING`: קיימת תקלה שמותר למערכת לשקם אוטומטית, אך טרם נאספו כל הראיות.
- `PAUSED_WAITING_STABILITY`: כל השערים נקיים; המערכת מחכה לחלון יציבות רציף.
- `PAUSED_MANUAL_ONLY`: הסיבה אינה בטוחה לשחרור אוטומטי ודורשת מפעיל.

כל pause מקבל `pause_generation`. שחרור מתבצע ב־CAS רק אם ה־generation וה־owner שנבדקו עדיין זהים בזמן הכתיבה. שינוי מקביל או pause חדש מבטלים את ניסיון השחרור הישן.

## מדיניות השחרור

1. `AUTO_WHEN_CLEAN` — תקלות תשתית זמניות, כגון ניתוק WS, stale market data, book לא מוכן, readiness זמני, heartbeat זמני או שגיאת reconciliation זמנית. השחרור מותר רק לאחר שכל השערים נקיים וחלון היציבות הושלם.
2. `AUTO_AFTER_REPAIR_AND_VERIFICATION` — פער כספי בר־תיקון. נדרשת ריצת reconciliation חדשה ונקייה שמאמתת את אותו pause generation; לאחר מכן נדרש גם חלון יציבות.
3. `MANUAL_ONLY` — operator pause, startup/manual state, kill switch, סתירה לא מסווגת, או כל reason לא מוכר. ברירת המחדל לסיבה חדשה היא fail-closed: אין החייאה אוטומטית.

## מקור אמת יחיד לשחרור

`EntryReleaseEvaluator` בודק יחד:

- Market WS מחובר, books מוכנים ונתוני השוק בתוך סף ה־freshness המוגדר.
- User WS מחובר ומצב המשתמש טרי.
- strategy readiness ו־reconciliation readiness הם `READY`.
- reconciliation אינו חוסם LIVE, ויש ראיה נקייה חדשה כאשר נדרש תיקון.
- heartbeat תקין; ב־pause מסוג heartbeat נדרשת הצלחה חדשה יותר מזמן רכישת ה־pause.
- kill switch כבוי.
- אין intent לא פתור, fill/cancel לא ודאי או חשיפה לא ודאית.
- בדיקת geographic availability תקפה ולא פגת תוקף.
- מנוע ההחייאה עצמו אינו `DEGRADED`.

כל blocker נשמר ומוצג עם source, details ו־age כאשר רלוונטי. exception במנוע מסומן באופן גלוי ומעביר את המערכת למצב חסום.

## מניעת flapping בלי לפגוע בבטיחות

סף freshness ב־LIVE הוא שנייה אחת, ולכן רעשי timestamp קצרים יצרו בעבר הרבה דורות `BOOK_NOT_READY`. נוסף detection debounce של שתי שניות רק לפני יצירת pause עמיד עבור blockers קצרים של book/freshness/readiness.

בזמן ה־debounce הכניסה עדיין חסומה מיד ומצב התצוגה הוא `GATED/DETECTING`; אין חלון שבו מותר להכניס עסקה על מידע stale. אם התקלה נעלמת לפני שתי שניות לא נכתב pause עמיד ולא נוצר audit storm. אם היא נמשכת, נרכש pause רגיל וההחייאה מתבצעת לפי כל השערים וחלון היציבות. ניתוק WS, heartbeat, reconciliation, config, kill switch וסיבות ידניות נשארו מיידיים ללא debounce.

## persistence, restart ותחרות

- state, owner, cause, policy, generation, acquisition evidence, stability evidence ו־financial proof נשמרים ב־SQLite.
- restart אינו מאפס pause ידני ואינו מאבד ראיות של pause recoverable.
- state וה־audit נכתבים באותה עסקה במסלולים הקריטיים.
- release ישן אינו יכול לעקוף pause חדש או שינוי owner.
- manual resume עובר דרך אותו evaluator ואינו עוקף kill switch, reconciliation או חשיפה לא ודאית.

## תיקונים שהתגלו בזמן rollout

- `strategy_readiness=NOT_READY` עם reason ריק סווג בעבר בטעות כ־`STRATEGY_NOT_READY/MANUAL_ONLY`. כעת הוא ממופה ל־`MARKET_DATA_NOT_READY/AUTO_WHEN_CLEAN`, ונוספה migration מצומצמת לתיקון הרשומה הישנה בלבד.
- תוקנה שגיאת shutdown של `_geographic_task` ב־`trader_app.py`.
- תוקן תרחיש regression מדויק שבו reconciliation 137741 מצא gap וריצה 137742 נקייה הייתה צריכה לקדם את ה־financial proof ולאפשר recovery.
- נוסף detection debounce כדי למנוע generation/audit flapping מרעשי freshness קצרים.

## אימות שבוצע

- לפני ההנחיה שלא להריץ בדיקות ארוכות: suite מלא — `296 passed` ועוד 9 subtests.
- Dashboard: build, TypeScript, lint ו־7 בדיקות עברו.
- לאחר תיקוני rollout: suites ממוקדים של מנגנון ההחייאה והבידוד עברו; בריצה האחרונה 41 בדיקות עברו ובדיקת תזמון אחת נכשלה עקב spike חד־פעמי. אותה בדיקה הורצה לבדה ועברה (`1 passed`), ובדיקת ה־debounce החדשה עברה (`1 passed`).
- `py_compile` לקבצי config/recovery עבר.
- smoke לאחר restart: השירות `active/running`, endpoint הבריאות החזיר `status=ok` ו־`strategy_readiness=READY`.
- snapshot סופי: `TRADING`, `pause_entries=false`, `recovery_status=HEALTHY`, שני ה־readiness הם `READY`, reconciliation אינו חוסם, kill switch כבוי, 0 unresolved intents ו־0 active positions.
- לאחר העלייה דור ה־pause נשאר 132 במשך יותר מחמש דקות, בניגוד ל־flapping שנצפה לפני ה־debounce.

## Rollback

- גיבוי DB מאומת: `/opt/polymarket-btc-live/backups/poly_live_20260820_162654.sqlite3.gz`
- SHA-256: `748444e84ee2ed9395e99833dd3a55f72587baf17ac3de86050c0b150a864fca`
- rollback של ה־dashboard הסטטי: `/var/www/live-status.rollback.20260820T170943Z`

## מה נשאר לביצוע מאוחר יותר

הבדיקות הארוכות לא הורצו בהתאם להנחיה. רשימת ה־24h soak, fault injection, restart endurance וסריקת security diff מלאה נמצאת ב־`POST_ROLLOUT_LONG_TESTS_HE.md`. מחבר Codex Security Access לא היה זמין בפריסה, ולכן אין לטעון שבוצעה סריקת Security מלאה; בוצעה רק בדיקת לוגיקה ממוקדת והסריקה המלאה נשארה כבדיקת המשך.

## מסקנה

התיקון הנקודתי הושלם ופעיל ב־LIVE. אין כרגע תקלה פתוחה במנגנון ההחייאה. הסיכון שנותר הוא סיכון תפעולי ארוך־טווח בלבד, והוא מכוסה ברשימת הבדיקות שנדחתה לביצוע מבוקר.
