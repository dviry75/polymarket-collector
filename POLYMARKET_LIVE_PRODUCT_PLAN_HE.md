# תוכנית מוצר — Polymarket LIVE DATA + PAPER TRADING

תאריך Audit: 2026-07-30 UTC
סביבת LIVE: `https://live-poly.dvirtechnologies.com`
סטטוס: מסמך תכנון המבוסס על בדיקות קריאה בלבד של השירותים, הקוד, מסדי הנתונים, ה־Environment והמסכים. הספירות המופיעות במסמך הן תמונת מצב בזמן ה־Audit.

## 1. תקציר מנהלים

1. כיום פועלות שתי מערכות נפרדות: DEMO פעיל ומרכז בקרה LIVE מוגן.
2. DEMO מגלה שוקי BTC ל־5 דקות, קורא Order Book אמיתי, קורא Coinbase, מריץ חוקים ויוצר עסקאות מדומות.
3. LIVE פעיל במצב קריאה בלבד; User WebSocket מחובר ו־Reconciliation מבצע קריאות GET בלבד.
4. User WebSocket מיועד לפעילות האישית בחשבון ואינו מקור מחיר לחוקים.
5. Market WebSocket של LIVE אינו worker רציף; מחירי Best Bid/Ask אמיתיים אינם נשמרים כעת ב־LIVE.
6. גילוי השוק ב־LIVE בוצע רק בעת startup, ולכן רשומות השוק ומנויי User WebSocket אינם מתחלפים עם כל אירוע חדש.
7. קיימים schema, API ומסכים ל־Rules, Deals ו־Orders, אך אין Rules worker ואין Paper Deal lifecycle ב־LIVE.
8. לכן אי אפשר כיום להריץ Paper Trading אוטומטי ואמין בתוך LIVE.
9. מסחר אמיתי חסום באמצעות flags, Kill Switch, בדיקות סיכון ו־adapter שה־write methods שלו חסומים בקוד.
10. הצעד הבא הוא צינור רציף Gamma → Market WebSocket → Paper Rules Engine נפרד → Paper Deals → מסכים ו־Export.

## 2. מטרת המוצר

| מצב | נתונים | חוקים | עסקאות |
|---|---|---|---|
| `READ_ONLY` | אמיתיים | כבויים | אין |
| `PAPER_TRADING` | אמיתיים | פעילים | מדומות |
| `REAL_TRADING` | אמיתיים | פעילים | אמיתיות |

### המצב היום

- LIVE נמצא בפועל ב־`READ_ONLY`.
- ה־Environment מציג `TRADING_MODE=DEMO`, `LIVE_ADAPTER=polymarket`, `LIVE_TRADING_ENABLED=false`, `LIVE_ORDER_SUBMISSION_ENABLED=false` ו־`LIVE_KILL_SWITCH=true`.
- ה־adapter בשם `polymarket` הוא כרגע adapter לקריאות GET מאומתות בלבד. יצירת order וביטול order מחזירים חסימה מתוך הקוד.
- DEMO מתנהג כמערכת Paper Trading נפרדת, אך אינו ה־LIVE DB ואינו מרכז הבקרה LIVE.

### היעד הקרוב

היעד הוא `PAPER_TRADING` בתוך סביבת LIVE:

- נתוני שוק אמיתיים ורציפים.
- חוקים פעילים אוטומטית.
- כניסות, יציאות, Stop Loss ו־Take Profit מדומים בלבד.
- עסקאות Paper נשמרות במסד LIVE ומוצגות בכל מסכי LIVE הרלוונטיים.
- אין CLOB write, אין כסף אמיתי ואין תלות ב־private key.

`REAL_TRADING` אינו חלק משלב העבודה הקרוב ואינו תנאי להשלמתו.

## 3. מפת המערכות

| רכיב | תפקיד בשפה פשוטה | מצב עובדתי היום |
|---|---|---|
| DEMO | מערכת הניסוי הקיימת | פעילה; נתוני שוק אמיתיים, חוקים ועסקאות מדומות |
| LIVE | מרכז הבקרה המוגן שבו רוצים להפעיל Paper Trading | פעיל במצב קריאה בלבד; ללא מנוע Paper פעיל |
| Gamma/API | מגלה את האירוע הנוכחי והבא ואת מזהי השוק והטוקנים | DEMO מרענן כל 5 שניות; LIVE מרענן רק ב־startup |
| Market WebSocket | מספק מחיר, Order Book ואירועי שוק בזמן אמת | קיים client לבדיקה תחומה בלבד; אינו מחובר ברציפות |
| User WebSocket | מספק אירועים אישיים של orders ו־trades בחשבון | מחובר; שומר health ואמור לשמור אירועים אם יתקבלו |
| Coinbase | מספק נפח BTC לתנאי Volume | עובד ב־DEMO דרך REST; אינו מחובר ל־LIVE |
| Rules Engine | בודק אם תנאי כניסה או יציאה התקיימו | עובד ב־DEMO; אינו רץ ב־LIVE |
| Paper Deals | רושם עסקה מדומה ומחשב את מחזור חייה | עובד ב־DEMO; אינו עובד ב־LIVE |
| Real Trading Adapter | שכבת חיבור עתידית למסחר אמיתי | מוגדר לקריאות GET בלבד; כל write חסום בכוונה |
| מסד DEMO | שומר שווקים, Order Book, Coinbase, חוקים ועסקאות DEMO | פעיל ומתעדכן |
| מסד LIVE | שומר מצב תפעולי, שווקים, Audit, WebSocket ומבני LIVE | פעיל; Rules, Deals, Orders ו־Fills ריקים |

ההפרדה העסקית החשובה:

- User WebSocket עונה על השאלה: “מה קרה בחשבון שלי?”
- Market WebSocket עונה על השאלה: “מה מחיר השוק ומה עומק הספר עכשיו?”
- Paper Trading חייב לצרוך מחיר מ־Market WebSocket. User WebSocket אינו תחליף לכך.

### שירותים ו־workers שנבדקו

| שירות/worker | רץ בפועל | מקור | תדירות בפועל | תוצר |
|---|---|---|---:|---|
| `polymarket.service` | כן | שירות DEMO | רציף | Dashboard ו־DB של DEMO |
| גילוי שוק DEMO | כן | Gamma | 5 שניות | `events` והאירוע הפעיל |
| Order Book DEMO | כן | CLOB public REST | כ־2 שניות | `orderbook_log`; מפעיל Rules |
| Coinbase DEMO | כן | Coinbase candles REST | 30 שניות | `btc_volume_log` |
| `polymarket-live.service` | כן | שירות LIVE | רציף | מרכז הבקרה LIVE |
| גילוי שוק LIVE | חלקי | Gamma | startup בלבד | `live_markets` |
| User WebSocket LIVE | כן | Polymarket User WS | רציף עם PING/PONG | state, health ו־Audit |
| Reconciliation LIVE | כן בעת חיבור/חיבור מחדש או ידנית | CLOB/Data API GET | אינו loop עצמאי של 15 שניות | `live_reconciliation_runs` |
| Market WebSocket LIVE | לא | Polymarket Market WS | אין | אין stream פעיל |
| Rules worker LIVE | לא | אמור לצרוך Market WS | אין | אין החלטות |
| Paper Deal worker LIVE | לא | אמור לצרוך החלטות Rules | אין | אין עסקאות |
| Coinbase LIVE | לא | Coinbase | אין | אין Volume ב־LIVE |

### מסדי הנתונים והטבלאות

מסד DEMO הפעיל: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3`

| טבלה | תפקיד | ספירה בזמן ה־Audit | רעננות/הערה |
|---|---|---:|---|
| `events` | אירועי Polymarket | 6,341 | גילוי שוק נראה בלוגים בזמן אמת |
| `orderbook_log` | דגימות Bid/Ask של YES/NO | 696,954 | דגימה אחרונה `2026-07-30T22:45:37Z` |
| `btc_volume_log` | דגימות Coinbase | 42,946 | דגימה אחרונה `2026-07-30T22:45:35Z` |
| `rules` | חוקי DEMO | 12 | 4 active, 8 inactive |
| `rule_inactive_windows` | חלונות אי־פעילות | 54 | משמשים את DEMO |
| `deals` | עסקאות DEMO | 6,956 | 2,608 win ו־4,348 loss; ללא open בזמן הדגימה |
| טבלאות `live_*` | migrations משותפות | קיימות אך כמעט כולן ריקות | אינן מסד LIVE הפעיל |

מסד LIVE הפעיל: `/opt/polymarket-btc-live/poly_live.sqlite3`

| טבלה | תפקיד | ספירה בזמן ה־Audit | רעננות/הערה |
|---|---|---:|---|
| `live_markets` | metadata ו־snapshot שוק | 10 | עדכון אחרון `2026-07-25T15:20:46Z`; Bid/Ask ריקים |
| `live_rules` | חוקי LIVE | 0 | אין Rules |
| `live_deals` | עסקאות LIVE | 0 | אין Paper או Real Deals |
| `live_orders` | Orders מקומיים/אמיתיים | 0 | ריק |
| `live_order_fills` | Fills | 0 | ריק |
| `live_positions` | פוזיציות | 0 | ריק |
| `live_websocket_events` | אירועי Market/User מנורמלים | 0 | אין אירוע שנשמר |
| `live_account_snapshots` | תמונות מצב חשבון | 0 | ריק |
| `live_dry_runs` | תצוגות מקדימות ידניות | 0 | ריק |
| `live_daily_limits` | מגבלות יומיות | 0 | ריק |
| `live_reconciliation_runs` | בדיקות התאמה | 72 | כולן `ok`; אחרונה `2026-07-30T21:26:07Z` |
| `live_audit_log` | יומן מערכת | 141,101 | מתעדכן; רובו health של User WS |
| `live_system_state` | Kill Switch ומצב WS | 5 | Kill Switch פעיל |
| `live_backups` | רישום גיבויים | 0 | ריק |

קיים גם `/opt/polymarket-btc/poly_data.sqlite3`, אך השירות הפעיל אינו משתמש בו. אין לערבב אותו עם מסד DEMO הפעיל.

## 4. מפת המסכים

מסכי LIVE הם views בתוך מרכז בקרה אחד. חלק מהשמות המבוקשים אינם מסכים עצמאיים כיום, ולכן הם מסומנים בהתאם.

| מסך | מטרתו | מה רואים בו | מקור הנתונים | אמיתי/מדומה | מחובר בפועל | מה חסר |
|---|---|---|---|---|---|---|
| Login | כניסה מאובטחת | משתמש, סיסמה ו־session | Auth של LIVE | אמיתי | כן | אין חסם ל־Paper |
| Dashboard / Overview | תמונת מצב מנהלית | mode, Kill Switch, WS, חשיפה, שוק אחרון | config, runtime ו־LIVE DB | אמיתי אך חלקי | כן, חלקית | מחירי שוק ו־Paper KPI |
| Operations | בריאות ותצורת runtime | Market/User WS, flags, reconciliation, export | runtime ו־LIVE DB | אמיתי | כן, חלקית | Market WS ו־Rules worker |
| Risk | חסמים ומגבלות | gates, חשיפה, orders, deals, rules | config ו־LIVE DB | אמיתי אך ריק מפעילות | כן, חלקית | מדדי Paper והפרדה מ־Real |
| Markets / Market Data | שוק וטוקנים | IDs, metadata, Bid/Ask ו־WS health | `live_markets` ו־runtime | Gamma אמיתי; מחיר ריק/ישן | חלקית | discovery רציף, Market WS והיסטוריה |
| Orders | Orders ו־fills | `live_orders`, `live_order_fills` ו־Deals | LIVE DB | כרגע ריק | כן כמסך ריק | תיוג Paper/Real ומודל Paper Orders |
| Deals | מחזור חיי עסקאות | טבלת Deals בתוך Orders | `live_deals` | כרגע ריק | לא כמסך עצמאי | מסך Paper Deals, P&L וסיבות |
| Rules | ניהול חוקים | API, ספירת active וכללי סיכון | `live_rules` | כרגע ריק | אין מסך ניווט מלא | CRUD בטוח, execution mode ו־worker |
| Reconciliation | התאמת פעילות אישית | User WS health וריצות התאמה | state ו־`live_reconciliation_runs` | אמיתי | כן | להבהיר שאינו מקור מחיר ואינו תנאי Paper |
| Audit / Logs | עקיבות ותקלות | Audit, alerts ו־WS events | `live_audit_log`, `live_websocket_events` | אמיתי | Audit כן; WS events ריק | Paper evaluations ו־Market events |
| Account | זהות ופוזיציות | snapshots ו־positions | טבלאות Account/Positions | ריק | לא | אינו נדרש ל־Paper MVP |
| Dry Run | preview ידני | החלטת Risk ללא ביצוע | Dry Run service | מדומה | קיים אך לא הופעל | אינו Paper Trading אוטומטי |
| Deployment | readiness והנחיות | flags וטקסט checklist | config וטקסט סטטי | מעורב | חלקית | חלק מהטקסט מיושן מול השרת |
| Maintenance | drain, readiness וגיבוי | מצב תחזוקה ו־backups | LIVE DB | אמיתי אך ריק | חלקית | מדיניות Paper בזמן drain |
| Settings | הגדרות מוצר | אין מסך עצמאי | — | — | לא קיים | תצוגת `execution_mode` ומדיניות Paper |
| Export | יצירת XLSX | פעולות generate/download; לא view עצמאי | 12 טבלאות LIVE | נתוני DB אמיתיים | קוד קיים, E2E לא הוכח | חסרים market history, evaluations, Coinbase, state ו־rule snapshot |
| DEMO Dashboard | מוצר הניסוי | KPI, Rules, Deals, Market, Volume ו־Export | מסד DEMO | שוק אמיתי + עסקאות מדומות | כן | אינו מחובר ל־LIVE |

מסקנת המסכים: Login, health, User WS, Reconciliation ו־Audit מציגים נתונים אמיתיים. Market Data מציג metadata אמיתי אך ישן ללא מחיר. Rules, Deals, Orders, Fills, Positions, Dry Run ו־Backups מציגים כרגע טבלאות ריקות. חלק מתצוגת Deployment הוא placeholder סטטי.

## 5. מפת החיבורים

| רכיב | מחובר | נשמר במסד | מוצג במסך | משמש את החוקים | מצב |
|---|---|---|---|---|---|
| Gamma market discovery | DEMO רציף; LIVE startup בלבד | כן | כן | DEMO בלבד | עובד חלקית |
| Market WebSocket | לא ב־LIVE רציף | לא | health בלבד | לא | לא מחובר |
| User WebSocket | כן | health/Audit; events אם יתקבלו | כן | לא, ובצדק | עובד |
| Coinbase Volume | DEMO בלבד | DEMO בלבד | DEMO בלבד | DEMO בלבד | עובד חלקית |
| LIVE Database | כן | כן | כן | אין worker שקורא Rules | עובד |
| DEMO Database | כן | כן | כן | כן | עובד |
| Rules Engine | DEMO בלבד | DEMO בלבד | DEMO כן; LIVE ריק | כן ב־DEMO | עובד חלקית |
| Paper Deals | DEMO בלבד | DEMO בלבד | DEMO בלבד | תוצר Rules | עובד חלקית |
| Real Trading Adapter | GET בלבד; writes hard-blocked | Reconciliation בלבד | מוצג כ־adapter | לא | חסום בכוונה |
| Reconciliation | כן, בעת User WS connect/reconnect או ידנית | כן | כן | לא | עובד |
| Dashboard | כן | קורא DB/runtime | כן | לא רלוונטי | עובד חלקית |
| Export | DEMO קיים; LIVE code בלבד | קובץ לפי בקשה | פעולות במסך | לא | עובד חלקית |

## 6. מה עובד מקצה לקצה

### DEMO — Market Data, Rules ו־Paper Deals

- נקודת התחלה: Gamma מגלה שוק BTC Up or Down ל־5 דקות.
- פעולה: CLOB REST נדגם עבור YES ו־NO; Best Bid/Ask נשמרים. כל דגימה מפעילה בדיקת exits ולאחר מכן entries.
- שמירה: `events`, `orderbook_log`, `rules`, `deals`.
- תצוגה: Dashboard, Rules, Deals, Market Data ו־Export של DEMO.
- הוכחה: דגימות טריות, 696,954 רשומות Order Book, 6,956 Deals ולוגים חיים של גילוי שוק והערכת Rules.

### DEMO — Coinbase Volume

- נקודת התחלה: worker מחזורי קורא candles של BTC-USD.
- פעולה: נשמרים volume cumulative ו־delta; בעת כניסה נשמר snapshot בעסקה.
- שמירה: `btc_volume_log` ושדות snapshot ב־`deals`.
- תצוגה: Dashboard ו־Export של DEMO.
- הוכחה: 42,946 דגימות, דגימה טרייה ולוגי הצלחה בזמן ה־Audit.

### LIVE — User WebSocket

- נקודת התחלה: startup של LIVE לאחר גילוי condition IDs.
- פעולה: subscription מאומת, PING/PONG כל 10 שניות ו־reconnect בעת צורך.
- שמירה: `live_system_state` ו־`live_audit_log`; order/trade יישמרו ב־`live_websocket_events` אם יתקבלו.
- תצוגה: Overview, Operations, Reconciliation ו־Logs.
- הוכחה: בדיקת היציבות הידועה עברה 600 שניות, 60 דגימות, 60 PONGs ואפס reconnects בתוך חלון הבדיקה. בזמן ה־Audit הנוכחי הסטטוס היה `CONNECTED` ו־PONG טרי. מונה חיי התהליך הציג 5 reconnects, ולכן “אפס reconnects” מתייחס לבדיקה התחומה ולא לכל חיי השירות.

### LIVE — Reconciliation לקריאה בלבד

- נקודת התחלה: חיבור/חיבור מחדש של User WS או פעולה ידנית.
- פעולה: קריאות GET ל־orders, trades ו־positions והשוואה ל־DB המקומי.
- שמירה: `live_reconciliation_runs`, state ו־Audit.
- תצוגה: Reconciliation ו־Operations.
- הוכחה: 72 ריצות `ok`; ה־adapter שנבחר הוא `polymarket` והמחלקה שלו מממשת GET בלבד.

### LIVE — חסימת מסחר אמיתי

- נקודת התחלה: Environment, Kill Switch ו־Risk Manager.
- פעולה: flags כבויים; Kill Switch פעיל; write methods של ה־adapter מחזירים `blocked`.
- שמירה/תצוגה: מצב החסמים ב־state ובמסכי Overview/Risk.
- הוכחה: `TRADING_MODE=DEMO`, שני flags של trading/submission כבויים, Kill Switch פעיל, וכל טבלאות Orders/Deals/Fills ריקות.

#### רכיבים שנראים קיימים אך לא הוכחו מקצה לקצה

- Market WebSocket: קיים manager, fixture ו־smoke endpoint; אין task רציף ב־startup.
- עיבוד Market WS: message רגיל נשמר כאירוע, אך אינו מעדכן Bid/Ask; רק `market_resolved` מעדכן חלק מ־`live_markets`.
- LIVE discovery: קיים קוד Gamma, אך הוא רץ רק ב־startup. הרשומות נשארו מ־25 ביולי.
- LIVE Rules: יש schema ו־POST, אך אין evaluation loop, last evaluation או decision log.
- LIVE Paper Deals: יש schema ו־Trading Engine כללי, אך אין Paper Engine ואין טריגר אוטומטי.
- Stop Loss/Take Profit ב־LIVE: יש שדות, אך אין worker שבודק אותם.
- User WS order/trade: הצינור קיים, אך אפס אירועים התקבלו ונשמרו; E2E של אירוע אמיתי לא הוכח.
- LIVE Export: קיים writer, אך לא הוכח קובץ LIVE בפועל והוא אינו כולל את נתוני ההחלטה הדרושים ל־Paper.
- Account, Positions, Dry Run ו־Backups: מסכים וטבלאות קיימים, אך ריקים.
- `LIVE_RECONCILIATION_INTERVAL_SECONDS=15` מוגדר, אך אין loop מחזורי עצמאי שמשתמש בו.

## 7. הזרימה העסקית הרצויה

```text
מצב: PAPER_TRADING
שוק: Bitcoin Up or Down — 5 minutes
כניסה: 0.74
Stop Loss: 0.65
Take Profit: 0.97
השקעה מדומה: $1
```

| שלב | עובד היום | עובד חלקית | חסר | כיצד מוכיחים |
|---|---|---|---|---|
| גילוי האירוע הפעיל | ב־DEMO | ב־LIVE רק startup | worker LIVE רציף | מעבר אוטומטי בין 3 אירועים לפחות |
| התחברות ל־Market WebSocket | — | smoke תחום בלבד | connection manager רציף | health חי, reconnect ו־resubscribe |
| Best Bid/Ask אמיתיים ל־YES/NO | ב־DEMO דרך REST | schema קיים ב־LIVE | stream, מיפוי ושמירת history | התאמה ל־snapshot ציבורי ומדדי freshness |
| בדיקת תנאי החוק | ב־DEMO | schema/API ב־LIVE | Rules worker | decision log לכל tick/אירוע משמעותי |
| פתיחת Paper Deal | ב־DEMO | schema ב־LIVE | Paper Engine נפרד | Deal אחד בלבד בלי adapter call |
| חישוב shares מדומה | ב־DEMO | helper כללי ב־LIVE | מודל fill מאושר | תוצאה צפויה עבור `$1 / 0.74` |
| Snapshot מחיר ונזילות | מחיר ב־DEMO | snapshot יחיד ב־LIVE | עומק, כמות, latency וקישור להחלטה | שחזור החלטה מלא מה־DB |
| מעקב אחר Best Bid | ב־DEMO | — | Paper exit worker | replay שמגיע ל־SL/TP |
| סגירה ב־SL/TP/סוף אירוע | ב־DEMO | שדות ב־LIVE | lifecycle ו־resolution | שלושה תרחישי קבלה נפרדים |
| עמלות משוערות | ב־DEMO | שדות aggregate ב־LIVE | policy ו־snapshot | בדיקת חישוב מול דוגמה מאושרת |
| P&L ברוטו ונטו | ב־DEMO | `realized_pnl_usd` ב־LIVE | gross/net/ROI ברורים | חישוב ידני תואם DB ומסך |
| הצגה ב־Deals | ב־DEMO | טבלה ריקה בתוך Orders | מסך Paper Deals | open/closed זהים ל־DB |
| הצגה ב־Rules | ב־DEMO | API בלבד ב־LIVE | מסך, last evaluation ו־reason | אותו rule snapshot במסך וב־Deal |
| הצגה ב־Dashboard | ב־DEMO | shell LIVE קיים | KPI Paper | סכומים תואמים לשאילתות DB |
| שמירה ב־Export | ב־DEMO | writer LIVE חלקי | evaluations, snapshots ו־Coinbase | שחזור Deal מה־XLSX בלבד |
| אפס גישה ל־Real Adapter | — | writes חסומים כיום | הפרדה מבנית Paper/Real | test שמכשיל כל call ל־CLOB write |

הזרימה הנכונה:

1. Gamma מגלה את האירוע הפעיל והבא.
2. Market WebSocket נרשם לשני token IDs ומקבל Bid/Ask ועומק אמיתיים.
3. הנתון נבדק ל־freshness, התאמת token/condition וסיום אירוע.
4. Rules Engine בודק את תנאי החוק לפי policy שאושר.
5. Paper Engine פותח Deal מדומה בלבד ושומר shares, snapshot ומקור מחיר.
6. מנוע היציאה בודק את Best Bid של הצד שנרכש.
7. Deal נסגר ב־Stop Loss, Take Profit או סוף האירוע.
8. מחושבים fees, slippage ו־P&L ברוטו/נטו.
9. אותו lifecycle מוצג ב־Deals, Rules, Dashboard, Audit ו־Export.
10. בשום שלב אין call ל־Real Trading Adapter.

## 8. החלטות עסקיות שכבר התקבלו

| החלטה | מצב במערכת/מסמכים |
|---|---|
| שוקי BTC Up or Down ל־5 דקות בלבד | DEMO ו־LIVE discovery ממוקדים בשוק זה |
| ברירת מחדל `$1` לעסקת דמו | קיים ב־DEMO וב־LIVE config |
| אין כניסה במחיר `0.50` | נאכף ב־DEMO וב־LIVE rule API |
| עסקה פתוחה אחת לכל חוק ואירוע | DEMO מחמיר בפועל לעסקה פתוחה אחת לכל חוק; ל־LIVE נדרש constraint מפורש |
| חוק חדש מתחיל מהאירוע הבא | `eligible_after_event_id` קיים ב־DEMO וב־LIVE schema |
| ביטול/השבתת חוק אינו סוגר עסקה פתוחה | מוכח בלוגיקת DEMO; יש לשמר ב־Paper |
| SL ו־TP נבדקים לפי מחיר הצד שנרכש | DEMO משתמש ב־Best Bid של אותו צד |
| נתוני השוק אמיתיים והעסקה מדומה | יעד מוצר מאושר |
| Real Trading נשאר חסום | Environment, Kill Switch וקוד |
| אם SL ו־TP נראים פגועים באותה בדיקה, SL קודם | זו התנהגות DEMO הקיימת |
| אין כניסה כאשר שני הצדדים תואמים באותו tick | התנהגות DEMO הקיימת |
| אין כניסה חדשה כאשר הנתון הדרוש חסר | DEMO מדלג; נדרש fail-closed גם ב־LIVE |
| חלונות אי־פעילות אינם סוגרים Deal שכבר נפתח | החלטת DEMO קיימת |
| timezone של חלונות DEMO הוא `Asia/Jerusalem` | קיים ב־schema וב־UI של DEMO |
| עמלות DEMO מחושבות באופן שמרני כ־TAKER | policy קיים ב־DEMO; אימוצו ל־LIVE Paper עדיין דורש אישור מפורש |

הערה: התנהגויות DEMO המפורטות כאן הן baseline קיים. כאשר לא נאמר במפורש שהן החלטת בעלים ל־LIVE Paper, יש לאשר אותן לפני העתקה.

## 9. החלטות שעדיין נדרשות

| החלטה | אפשרויות | המלצה | השפעה |
|---|---|---|---|
| טריגר כניסה | שוויון מדויק; נגיעה/חצייה; crossing מהצד הקודם | נגיעה או חצייה של Best Ask, עם deduplication | קובע אם Deals יוחמצו או ייפתחו שוב |
| מחיר כניסה מדומה | threshold; Best Ask; עומק ספר | Best Ask עם walk בעומק לפי `$1` | אמינות fill ו־P&L |
| מילוי | מלא תמיד; לפי נזילות; FOK מדומה | FOK מדומה לפי עומק זמין | מונע תוצאה אופטימית |
| מילוי חלקי | אסור; מותר; תלוי purpose | ללא partial בכניסה הראשונה | מפשט lifecycle |
| מחיר יציאה | threshold; Best Bid; עומק זמין | trigger לפי Bid, fill לפי עומק זמין | משפיע ישירות על הפסד/רווח |
| עמלות | אפס; שיעור קבוע; metadata שוק | metadata בזמן ההחלטה עם fallback מתועד | P&L נטו |
| Slippage | אפס; קבוע; לפי עומק | לפי עומק, עם fallback שמרני | ריאליזם הסימולציה |
| Volume | לא נדרש; Coinbase delta; Polymarket volume; שילוב | להחליט לפני חוק ראשון; אם אינו תנאי בחוק הראשון לדחות | scope ותלות ב־Coinbase |
| זמן כניסה באירוע | כל האירוע; חלון לפני סיום; schedule | להגדיר per rule ולחסום אם end time חסר | מונע כניסה מאוחרת |
| סיום אירוע | Bid אחרון; resolution; payout `0/1`; expiry | resolution רשמי עם מצב pending עד אימות | P&L וסגירת Deals |
| Snapshots | כל tick; כל שינוי; sampling | כל שינוי רלוונטי + snapshot בכל decision | נפח DB מול Audit |
| תדירות Rules | כל message; debounce; interval | event-driven עם debounce ו־freshness guard | latency ועומס |
| ניתוק Market WS | pause; REST fallback לביצוע; REST לתצוגה בלבד | pause entries; REST לתצוגה בלבד | בטיחות מול זמינות |
| Kill Switch ב־Paper | עוצר Paper; עוצר Real בלבד; שני מתגים | מתגים נפרדים וברורים | תפעול ובטיחות |
| Deal אחד | לכל חוק; חוק+אירוע; חוק+אירוע+צד | אחת לכל חוק ואירוע, כפי שהוגדר | מקביליות וחשיפה |
| retention | ללא מחיקה; ימים; aggregation | retention מאושר + archive | גודל DB ויכולת תחקור |

המלצות אלה אינן החלטות בשם הבעלים. יש לאשר אותן לפני מימוש.

## 10. מבנה הנתונים הרצוי

### שדות שכבר קיימים ב־LIVE

- מזהי `event_id`, `condition_id`, `token_id`, YES/NO token IDs.
- מחירי כלל: entry, Stop Loss ו־Take Profit.
- requested amount/size, filled size, average entry/exit price.
- fees ו־slippage כסכומים מצטברים.
- timestamps בסיסיים ליצירה/עדכון ול־orders/fills.
- `best_bid`, `best_ask` ו־orderbook depth ברמת `live_markets` snapshot.
- exit reason, resolved outcome ו־winning asset.
- מקור ברמת market (`source`).

### שדות או ישויות שחסרים ל־Paper Trading אמין

| שדה/ישות | למה נדרש |
|---|---|
| `execution_mode=READ_ONLY/PAPER/REAL` | הפרדה חד־משמעית בכל Rule, Deal, Order ו־Export |
| `price_source` | הוכחה אם המחיר הגיע מ־Market WS, REST או replay |
| `market_timestamp` ו־`received_at` | הבחנה בין זמן המקור לזמן הקבלה |
| `latency_ms` | זיהוי נתון מאוחר |
| Bid/Ask לשני הצדדים בעת decision | שחזור מלא של מצב השוק |
| `available_size` ועומק ששימש ל־fill | בדיקת נזילות |
| `estimated_fees_usd` + rate/source/version | P&L עקבי ומוסבר |
| `estimated_slippage_usd` + model/version | שקיפות מודל הביצוע |
| `entry_reason` ו־`exit_reason` | הסבר עסקי |
| Rule version או snapshot JSON | שמירת החוק כפי שהיה בזמן ההחלטה |
| `market_data_event_id`/snapshot reference | קישור ההחלטה לנתון |
| `decision_id` ו־idempotency key | מניעת פעולה כפולה |
| Paper lifecycle events | audit של pending/open/closed/error |
| gross P&L, net P&L ו־ROI | הפרדת ביצוע מהוצאות |
| `paper_fill_status` | full/unfilled/partial לפי policy |

המלצה: לא להעמיס את כל ההיסטוריה על `live_markets`. יש לשמור metadata נוכחי בנפרד מהיסטוריית market snapshots/updates ומהיסטוריית rule evaluations.

## 11. מנגנוני בטיחות

| מנגנון | קיים היום | מה חסר |
|---|---|---|
| Paper Engine ללא private key | אין private key שנדרש ליעד, אך אין Paper Engine נפרד | process/module ייעודי שאינו טוען סודות מסחר |
| אין גישה ל־Real Adapter | writes ב־adapter חסומים, אך אותו service מחזיק adapter לקריאות חשבון | dependency boundary שמונע import/call מ־Paper |
| Real flags כבויים | כן | test תצורה שמסרב startup אם Paper ו־Real מעורבבים |
| Kill Switch פעיל | כן | להגדיר מתג Paper נפרד |
| Paper Rule מסומן במפורש | לא | `execution_mode=PAPER` חובה ו־DB constraint |
| אין route שממיר Paper ל־Real | אין route כזה, אך גם אין mode מפורש | API נפרד ללא update mode ישיר |
| Paper אינו קורא CLOB write | אין Paper path כיום; adapter write hard-blocked | test שמחליף adapter ב־failing spy ומוכיח אפס calls |
| write endpoints חסומים | כן בקוד הנוכחי; create/cancel מחזירים `blocked` | להשאיר חסימה גם לאחר בניית Paper |
| Kill Switch + Risk gates | כן | לא להסתמך עליהם כגבול היחיד |
| בדיקות אוטומטיות | קיימות בדיקות ל־flags, Kill Switch ו־adapter blocked | בדיקות ייעודיות ל־Paper isolation, config matrix ו־negative CLOB calls |
| הפרדת DB | DEMO ו־LIVE נפרדים | constraints ו־labels בכל export/מסך |

מצב הסיכון כיום: לא נמצא מסלול פעיל ששולח order אמיתי. עם זאת, לבניית Paper Trading אסור להסתפק בכך שה־Real adapter “כרגע חסום”; נדרשת הפרדה מבנית שבה Paper Engine כלל אינו יכול להגיע אליו.

## 12. חוסרים לפי עדיפות

### קריטי

| חוסר | השפעה עסקית | מה נדרש | סוג עבודה | הוכחת השלמה |
|---|---|---|---|---|
| LIVE discovery רציף | מנויים נשארים על אירועים ישנים | Gamma worker ו־rollover | Backend/Runtime | 3 מעברי אירוע רצופים |
| Market WS רציף | אין מחיר אמיתי לחוקים | connect/reconnect/resubscribe/freshness | Data/Runtime | soak ו־השוואת מחיר |
| שמירת market history | אי אפשר לתחקר החלטה | snapshots/updates עם timestamps | DB | replay מלא של החלטה |
| execution mode מפורש | סכנת ערבוב Paper/Real | enum, constraints ו־UI labels | Architecture/DB | matrix tests |
| Paper Engine נפרד | אין יצירת Deal בטוחה | lifecycle ללא OrderManager Real | Backend/Safety | spy מוכיח אפס write calls |
| Rules worker LIVE | חוקים אינם רצים | evaluation loop על Market WS | Backend | חוק אחד פותח פעם אחת |
| SL/TP/resolution | Deal לא נסגר | exit worker ו־settlement | Backend | 3 תרחישי קבלה |
| idempotency/recovery | כפילויות לאחר reconnect/restart | keys, locks ו־DB state | Reliability | restart/reconnect test |

### חשוב

| חוסר | השפעה עסקית | מה נדרש | סוג עבודה | הוכחת השלמה |
|---|---|---|---|---|
| Rules UI מלא | בעלים אינו רואה מצב החלטה | CRUD, status, last reason | Product/UI | התאמה ל־DB |
| Paper Deals UI | אין שקיפות עסקה | open/closed/P&L/snapshots | Product/UI | התאמה ל־DB |
| Dashboard Paper KPI | תמונת מצב מטעה | KPI ו־health נפרדים | Product/UI | reconciliation בין מסכים |
| Export מלא | אי אפשר לנתח או לשחזר | evaluations, snapshots, lifecycle | Data/Product | שחזור Deal מ־XLSX |
| fees/slippage policy | P&L אינו אמין | policy, version ו־snapshot | Product/Backend | דוגמאות ידניות |
| Observability | worker עלול להיתקע בשקט | lag/stale/reconnect/duplicate alerts | Operations | fault injection |
| Coinbase LIVE אם נדרש | חוק Volume לא אפשרי | collector ושדות snapshot | Data | sample טרי ב־Rule decision |

### אפשר לדחות

| חוסר | השפעה עסקית | מה נדרש | סוג עבודה | הוכחת השלמה |
|---|---|---|---|---|
| Account/Positions UI מלא | אינו חוסם Paper | public/read-only account views | Product | read-only E2E |
| User WS order/trade E2E | אין פעילות אמיתית בשלב Paper | בדיקה עתידית ללא יצירת trade לצורך הבדיקה | Integration | אירוע טבעי/fixture מאושר |
| Real Trading Adapter | מחוץ ליעד | תכנון עתידי נפרד | Future | לא נדרש |
| allowances/approve/redemption | מחוץ ליעד | לא לבצע | Future | לא נדרש |
| alerts חיצוניים | UI יכול להספיק לפיילוט | ערוץ התראות | Operations | delivery test |

## 13. תוכנית עבודה בשלבים

| שלב | מטרה | פעולות | תנאי הצלחה | מה אסור | החלטה נדרשת מהבעלים |
|---|---|---|---|---|---|
| 1. אימות Market WebSocket | מקור מחיר אמיתי | discovery רציף, WS רציף, timestamps, history | 3 rollovers, reconnect ו־freshness תקינים | Rules או Deals | REST fallback ו־retention |
| 2. הגדרת `PAPER_TRADING` | גבול מוצר ובטיחות | execution mode, constraints, flags ומיתוג UI | אי אפשר ליצור Rule ללא mode; REAL נשאר חסום | private key או Real enable | התנהגות Kill Switch ב־Paper |
| 3. חיבור Rules לנתוני שוק | החלטות אוטומטיות | evaluation event-driven, entry policy ו־decision log | replay מייצר החלטה יחידה נכונה | call ל־OrderManager Real | equality/crossing וזמן כניסה |
| 4. יצירת Paper Deals | lifecycle מדומה | fill model, shares, snapshots, idempotency | כניסה אחת של `$1` ללא write חיצוני | CLOB order/cancel | liquidity, partial fill, fees/slippage |
| 5. יציאות | SL/TP/סיום אירוע | מעקב Bid, resolution ו־P&L | תרחישי 0.65, 0.97 וסיום עוברים | redemption | מחיר יציאה וסיום אירוע |
| 6. חיבור למסכים | מוצר מובן | Rules, Deals, Dashboard, Audit | משתמש יכול להסביר כל החלטה | ערבוב נתוני DEMO | KPI ושפה |
| 7. חוק אחד על אירוע אחד | פיילוט מבוקר | Rule Paper אחד, event אחד, `$1` מדומה | Deal אחד, lifecycle מלא, אפס writes | Real Rule או כמה חוקים | פרטי החוק והחלון |
| 8. בדיקה רציפה | אמינות | soak של כמה שעות ואז 24 שעות | ללא stale לא מזוהה, כפילויות או ערבוב אירועים | הרחבת scope תוך כדי | סף מוכנות |
| 9. עמלות, P&L ו־Export | תוצאה עסקית אמינה | אימות מודלים וייצוא | DB, מסכים ו־XLSX תואמים | שינוי היסטוריה ללא version | policy סופי |
| 10. החלטת מוכנות | go/no-go להרצה ממושכת | review תוצאות וחריגים | checklist חתום | מעבר ל־REAL | אישור בעלים |

סדר הביצוע חשוב: אין ליצור Rule פעיל ב־LIVE לפני ששלבים 1–5 הוכחו ב־fixtures/replay ובבדיקות בטיחות.

## 14. מה לא עושים כרגע

- לא מפעילים מסחר אמיתי.
- לא מכניסים private key לשרת לצורך Paper Trading.
- לא משנים allowances.
- לא מבצעים approve.
- לא שולחים orders.
- לא מבטלים orders.
- לא מבצעים redemption.
- לא מפעילים Real Rules.
- לא מערבבים בין מסד DEMO למסד LIVE.
- לא משתמשים ב־User WebSocket כמקור מחיר.
- לא מבטלים Kill Switch לצורך Paper Trading.
- לא יוצרים במסגרת התכנון Rule, Deal, Dry Run או fixture.

## 15. תשובה ישירה לבעלים

| שאלה | תשובה |
|---|---|
| מה המוצר יודע לעשות היום? | DEMO יודע לבצע Market Data אמיתי + Rules + Paper Deals. LIVE יודע להציג מרכז בקרה, לגלות שוק ב־startup, להחזיק User WS ולבצע reconciliation לקריאה בלבד. |
| האם User WebSocket מחובר? | כן. בזמן ה־Audit היה `CONNECTED` עם PONG טרי. בדיקת 600 השניות עברה ללא reconnect; לאורך חיי התהליך נרשמו 5 reconnects. |
| האם Market WebSocket מחובר? | לא. קיים smoke client בלבד, לא worker רציף. |
| האם מחירי שוק אמיתיים נשמרים ב־LIVE? | לא באופן שימושי ל־Paper. metadata נשמר מ־Gamma, אך הוא ישן, Best Bid/Ask ריקים ואין history. |
| האם Rules Engine של LIVE רץ? | לא. קיימים schema ו־API, אך אין worker. |
| האם ניתן כיום ליצור Paper Rule בטוח? | לא כמוצר Paper. אפשר לשמור רשומת `live_rule`, אך אין `execution_mode`, אין Paper worker ואין lifecycle; לכן אין לראות בכך Paper Rule מוכן. |
| האם Paper Deals יתעדכנו אוטומטית? | לא. `live_deals` ריקה ואין worker שפותח או סוגר Deals. |
| האם המסכים משקפים את הנתונים הנכונים? | חלקית. health, flags, User WS, reconciliation ו־Audit אמיתיים; Market Data ישן וללא מחיר; מסכי פעילות רבים ריקים; Deployment כולל טקסט סטטי מיושן. |
| האם קיים סיכון לשליחת פקודה אמיתית? | במצב ובקוד הנוכחיים המסלול חסום בכמה שכבות ולא נמצא write פעיל. עדיין יש לבנות Paper Engine ללא כל גישה ל־Real Adapter כדי שההפרדה לא תהיה תלויה רק ב־flags. |
| מהם שלושת הצעדים הקריטיים הבאים? | (1) discovery + Market WS רציפים ושמירת history; (2) `execution_mode=PAPER` ו־Paper Engine מבודד; (3) Rules + Deal lifecycle עם SL/TP, מסכים ו־Export. |
| מה אפשר לדחות? | Account/Positions מלאים, User WS trade E2E, alerts חיצוניים וכל רכיב Real/allowance/approve/redemption. Coinbase אפשר לדחות אם אינו תנאי בחוק הראשון. |
| כמה רחוקים אנחנו מהרצת חוק דמו אחד? | לא config אחד אלא שלושה רכיבי P0 והוכחת אינטגרציה: Market Data רציף, Paper isolation, ו־Rules/Deal lifecycle. בסיס DEMO קיים, אך LIVE עדיין אינו מוכן לחוק דמו אחד. |

## נספח א — Inventory וסיווג קובצי Markdown

הסיווג נעשה לפני ההעברה. רק `ARCHIVE` מועבר.

| קובץ | סיווג | נימוק |
|---|---|---|
| `README.md` | ACTIVE | README ראשי |
| `polymarket-collector/README.md` | ACTIVE | README של השירות הפעיל |
| `polymarket-btc-local/README.md` | ACTIVE | README של רכיב קיים |
| `polymarket-collector/LIVE_SYSTEM.md` | ACTIVE | מסמך מערכת/Runbook פעיל |
| `polymarket-collector/deploy/DEPLOYMENT_CHECKLIST.md` | ACTIVE | Deployment פעיל |
| `polymarket-collector/POLYMARKET_LIVE_OPERATOR_ACTIONS_REQUIRED_HE.md` | ACTIVE | מסמך פעולות בעלים; אינו tracked בזמן ה־Audit |
| `COINBASE_VOLUME_IMPLEMENTATION_REPORT.md` | ARCHIVE | דוח יישום קודם |
| `CURRENT_SYSTEM_REPORT.md` | ARCHIVE | דוח מצב קודם שהוחלף ב־Audit זה |
| `EXECUTIVE_DASHBOARD_ANALYSIS.md` | ARCHIVE | דוח ניתוח ותכנון קודם |
| `POLYMARKET_FEES_IMPLEMENTATION_REPORT.md` | ARCHIVE | דוח יישום קודם |
| `POLYMARKET_LIVE_ARCHITECTURE_ADAPTATION_REPORT.md` | ARCHIVE | דוח התאמה קודם |
| `POLYMARKET_LIVE_IMPLEMENTATION_REPORT.md` | ARCHIVE | דוח יישום קודם |
| `POLYMARKET_LIVE_PHASE_2_IMPLEMENTATION_REPORT.md` | ARCHIVE | דוח Phase קודם |
| `RULE_INACTIVE_SCHEDULE_BUG_REPORT.md` | ARCHIVE | דוח אבחון קודם |
| `RULE_INACTIVE_SCHEDULE_FINAL_CLOSURE_REPORT.md` | ARCHIVE | דוח סגירה קודם |
| `RULE_SCHEDULING_AND_DASHBOARD_FILTER_REPORT.md` | ARCHIVE | דוח יישום קודם |
| `DASHBOARD_DATA_EXPLANATION_HE.md` | UNCERTAIN | מסמך הסבר שעשוי להיות תיעוד משתמש פעיל |
| `POLYMARKET_LIVE_PRODUCT_QUESTIONS.md` | UNCERTAIN | רובו הוחלף, אך קוד המסך מפנה אליו בשם הקובץ |
| `polymarket-collector/POLYMARKET_USER_WEBSOCKET_CONNECTION_REPORT_HE.md` | UNCERTAIN | דוח היסטורי, אך מסמך פעולות הבעלים מפנה אליו |
| `POLYMARKET_LIVE_PRODUCT_PLAN_HE.md` | ACTIVE | המסמך המרכזי החדש; נשאר בשורש |

לא נמצאו secrets גלויים בסריקת Markdown. ערכי Environment ו־credentials לא הועתקו למסמך.

## נספח ב — היקף ובטיחות ה־Audit

- לא שונה קוד.
- לא שונו Environment, מסדי נתונים, WAL, גיבויים או secrets.
- לא בוצע restart.
- לא נוצר, הופעל, שונה או בוטל Rule.
- לא נוצר Deal, Dry Run, fixture או order.
- לא בוצעו approve, allowance, cancel, redemption או CLOB write.
- לא בוצעה פעולת מסחר.
- ה־audit כלל קריאות קבצים, סטטוס systemd/process, health GET ושאילתות SQLite ב־`mode=ro`.
- ספירות DB עשויות לגדול לאחר זמן ה־Audit משום ששירותי DEMO ו־LIVE ממשיכים לפעול כרגיל.
