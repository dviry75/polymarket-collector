# דוח פריסה ואימות — LIVE Paper Trading

תאריך ביצוע: 31 ביולי 2026 (UTC)

## 1. סיכום מנהלים

מערכת Paper Trading מתוך `b405fad` נפרסה בהצלחה לתוך מערכת ה־LIVE בכתובת `https://live-poly.dvirtechnologies.com`. המערכת מקבלת Order Book אמיתי מערוץ Market WebSocket הציבורי של Polymarket, מפעילה מנוע חוקים מבודד שאינו מחזיק תלות בפקודות מסחר, ושומרת rules, evaluations, snapshots ו־deals מסוג `PAPER_TRADING` במסד ה־LIVE הנפרד.

תוקן מחיר הסגירה המדומה: סף SL/TP נשמר כ־trigger/requested exit, בעוד שמחיר הביצוע, PnL, ROI והעמלה מחושבים לפי VWAP של רמות Bid זמינות כאשר יש עומק מספיק, או לפי Best Bid אמיתי ועדכני כאשר אין עומק מספיק. ללא Bid תקין לא נוצר מילוי.

תנאי הכניסה נשאר מחייב: רק Best Ask ששווה בדיוק למחיר החוק באמצעות `Decimal`. לא הוכנסה לוגיקת crossing, טווח, `>=` או `<=` לכניסה.

בוצע ניסוי חי במשך שישה אירועי BTC Up or Down של 5 דקות. בכל ששת האירועים התקבלו 0 עדכונים במחיר 0.74 בדיוק, ולכן לא נפתחה עסקה. לא שונה מחיר החוק ולא זויפה עסקה. החוק הזמני הושבת בסיום.

## 2. המצב לפני העבודה

- ההנחה `/opt/polymarket-btc/polymarket-collector` הייתה תיקיית הקוד, אך Git root בפועל הוא `/opt/polymarket-btc`.
- השירות בפועל: `polymarket-live.service`.
- `WorkingDirectory`: `/opt/polymarket-btc-live`.
- פקודת הפעלה: `/opt/polymarket-btc-live/.venv/bin/uvicorn live_app:app --host 127.0.0.1 --port 8001`.
- קובץ סביבה: `/etc/polymarket-live/live.env`.
- מסד LIVE: `/opt/polymarket-btc-live/poly_live.sqlite3`.
- מסד DEMO המקורי: `/opt/polymarket-btc/polymarket-collector/poly_data.sqlite3`.
- nginx והשירות היו active; health מקומי וציבורי החזירו `{"status":"ok"}`.
- HEAD היה `41594afe4b149af7b92defd23f7edcc04d98e6cb`, branch `main`, מאחור ביחס ל־remote.
- היו שינויים מקומיים קיימים ב־`app.py` וב־`tests/test_rules_deals.py`, וכן קבצים לא מנוהלים. הם נשמרו, לא בוצע reset/stash ולא הוכנסו ל־commits של משימה זו.
- התאמות read-only בנתיב ה־LIVE, שלא היו עדיין ב־main, נשמרו בגיבוי ומוזגו בהמשך במקום להידרס.

## 3. קומיטים שנפרסו

- בסיס Paper Trading: `b405fad25a843ab19bc54500f4939397be6ce6f9` — `Add LIVE WebSocket paper trading`.
- תיקון מחיר יציאה: `af18585efa1022925469eafa3df0911bf02a5c1d`.
- שימור תנאי חוק DEMO ב־Paper: `d1da329adad6ca77fe7cbe9105d543d627ca5b07`.
- yield ל־event loop בזמן קליטת WS: `1adbf8530183b1b0cb2075f28d9d88986e291bdd`.
- reconnect בעת רוטציית asset IDs: `b355696f86adea17415aeadecb7540f693bd15a7`.
- שימור התנהגות read-only הקודמת: `657efb1052a353955ee1f821c22a6081561117ce`.

## 4. תיקון מחיר הסגירה

לפני התיקון, Stop Loss ו־Take Profit הופעלו לפי Best Bid, אך `close_paper_deal` קיבל את סף החוק עצמו כמחיר ביצוע. לאחר התיקון:

1. הטריגר העסקי נשאר לפי Best Bid כפי שהיה.
2. `requested_exit_price` שומר את סף ה־SL/TP שהופעל.
3. `average_exit_fill_price` שומר את מחיר הביצוע המדומה בפועל.
4. כאשר `bids_json` מכיל עומק מספיק לכמות העסקה, מחושב VWAP בסדר מחיר יורד.
5. כאשר העומק חסר או אינו מספיק, נעשה fallback ל־Best Bid תקין ועדכני.
6. ללא Best Bid תקין העסקה נשארת פתוחה ונכתב audit מסוג `paper_exit_not_filled` עם `NO_VALID_FRESH_BID`.
7. gross PnL, net PnL, ROI ועמלת היציאה מחושבים כולם ממחיר הביצוע.
8. audit הסגירה כולל trigger price, Best Bid, execution price ו־fill method.

בדיקות מוכיחות TP 0.95 מול Best Bid 0.94, SL 0.65 מול Best Bid 0.63, VWAP רב־רמתי, PnL לפי execution, ומניעת מילוי ללא Bid.

## 5. אישור מפורש לגבי מחיר הכניסה

מחיר הכניסה נשאר equality מדויק:

```python
entry = Decimal(str(rule.get("entry_price")))
ask = Decimal(str(snapshot.get("best_ask")))
if entry is None or ask != entry:
    return "SKIP", "ENTRY_PRICE_NOT_MATCHED"
```

- מעבר 0.73 → 0.75 אינו פותח עסקה.
- 0.74 פותח עסקה בבדיקת unit כאשר שאר התנאים מתקיימים.
- עדכוני 0.74 חוזרים אינם יוצרים עסקאות כפולות.
- בניסוי החי נצפה crossing מ־0.12 ל־0.87; לא נפתחה עסקה.
- לא נעשה שימוש בהשוואת float ישירה בהחלטת הכניסה.

## 6. קבצים ששונו

- `live/paper_trading.py`
- `live/repository.py`
- `live/router.py`
- `live/market_websocket.py`
- `live/config.py`
- `live/adapters/polymarket.py`
- `live/reconciliation.py`
- `tests/test_paper_trading.py`
- `LIVE_PAPER_DEPLOYMENT_AND_VERIFICATION_REPORT_HE.md`

קובצי `.env`, מסדי נתונים, backups, logs והבדיקה המקומית הלא־מנוהלת `tests/test_polymarket_adapter.py` לא הוכנסו ל־Git.

## 7. Migrations

הופעל `LiveRepository.migrate()` באופן אידמפוטנטי על DB ה־LIVE לאחר גיבוי ובזמן שהשירות עצור.

נוצרו/אומתו:

- `live_market_snapshots` ואינדקסים רלוונטיים.
- `live_rule_evaluations` ואינדקסים רלוונטיים.
- עמודות בידוד ו־audit ל־Paper ב־`live_rules` וב־`live_deals`.
- עמודות חוק DEMO: מכסות YES/NO, חלון כניסה, timezone, inactive windows, מקור rule ו־snapshot מקור.
- unique index שמונע Paper deal פתוח כפול לאותו rule/event.

`PRAGMA integrity_check` לאחר הפריסה: `ok`.

## 8. משתני סביבה

נוספו או שונו השמות הבאים בלבד:

- `LIVE_EXECUTION_MODE`
- `LIVE_PAPER_TRADING_ENABLED`
- `POLYMARKET_MARKET_WS_ENABLED`
- `LIVE_MARKET_DISCOVERY_INTERVAL_SECONDS`
- `LIVE_MARKET_DATA_STALE_AFTER_SECONDS`
- `LIVE_PAPER_TAKER_FEE_RATE`

ערכים סודיים אינם מופיעים בדוח. הגדרות הבטיחות נשארו: `TRADING_MODE=DEMO`, `LIVE_TRADING_ENABLED=false`, `LIVE_ORDER_SUBMISSION_ENABLED=false`, kill switch פעיל, ו־`real_submission_armed=false`.

## 9. גיבויים

ספריית גיבוי: `/opt/polymarket-btc-live/backups/live-paper-20260731T085250Z`.

- `poly_live.sqlite3` — גיבוי עקבי דרך SQLite backup API, 61,067,264 bytes, integrity `ok`.
- `live.env` — mode 0600, root-owned.
- `polymarket-live.service` — mode 0600, root-owned.
- `POLYMARKET_LIVE_PRODUCT_PLAN_HE.local-untracked.md` ועותק original של הקובץ המקומי שהתנגש ב־fast-forward.
- `code-before/live/` ו־`code-before/live_app.py` — קוד ה־LIVE לפני הפריסה.

## 10. שירות, nginx ו־health

- `polymarket-live.service`: `active`.
- nginx: `active`; `nginx -t` successful.
- health מקומי: HTTP 200, `{"status":"ok"}`.
- health ציבורי: HTTP 200, `{"status":"ok"}`.
- `/live/login`: HTTP 200.
- דפי Paper/API מוגנים ודורשים session; בקשה לא מאומתת ל־Paper overview החזירה 401 כמצופה.
- קיימת אזהרת nginx לא חוסמת על תחביר `listen ... http2` מיושן. לא נדרש שינוי nginx לצורך הפריסה.

בפריסה הראשונה נמצא event-loop starvation בגלל backlog WS רציף. נוסף yield מפורש. התהליך הישן נדרש ל־SIGKILL לאחר timeout פעם אחת; לאחר התיקון restarts הושלמו וה־health חזר.

## 11. WebSocket ונתוני שוק

- Market WebSocket: `CONNECTED`.
- המקור ב־snapshots: `POLYMARKET_MARKET_WS`.
- בזמן האימות הסופי היו 28,967 snapshots; הרשומה האחרונה הייתה עם timestamp עדכני.
- smoke test ציבורי נפרד קיבל 4 הודעות `book` אמיתיות ל־4 asset IDs.
- dynamic subscription המקורי לא הזרים באופן אמין את השוק המתחלף אף שה־payload תאם לתיעוד Polymarket. הוחלף ב־reconnect ציבורי מבוקר בעת שינוי רשימת assets; לאחר מכן כל אירוע חדש קיבל initial book ללא restart ידני.
- אין שימוש ב־wallet signing או בכתיבת מסחר במסלול Paper/Market WS.

## 12. הפרדה בין Paper למסחר אמיתי

- `live_rules` ו־`live_deals` משתמשים ב־`execution_mode='PAPER_TRADING'`.
- endpoints נפרדים: `/live/paper/rules`, `/live/paper/deals`, `/live/paper/evaluations`, `/live/paper/health`.
- views נפרדים: Paper Overview, Paper Rules, Paper Deals.
- `PaperTradingEngine.health().write_dependencies == []`.
- המנוע אינו מייבא OrderManager, TradingAdapter או submit/cancel functions.
- `live_orders=0`, `live_order_fills=0`, `live_deals=0` בסיום הניסוי.
- ה־adapter המשומר מאפשר GET read-only בלבד; submit/cancel נשארים חסומים ומחזירים `REAL_POLYMARKET_ORDER_SUBMISSION_DISABLED_IN_THIS_BUILD`.
- מערכת DEMO המקורית ומסד `poly_data.sqlite3` לא שונו; החוק המקורי נשאר פעיל.

## 13. חוק הבדיקה

מקור: מסד DEMO, `rules.id=7`, שם מדויק `רובוט פולימרקט`.

חוק LIVE Paper:

- ID: 1.
- שם: `רובוט פולימרקט - LIVE PAPER TEST - 20260731T090956Z`.
- entry: 0.74.
- Stop Loss: 0.66.
- Take Profit: 0.95.
- סכום: 1 USD, בהתאם לסכום הקבוע של מנגנון DEMO.
- max YES entries/event: 1.
- max NO entries/event: 1.
- חלון כניסה: מ־120 שניות לפני סוף האירוע עד 0.
- timezone: `Asia/Jerusalem`.
- שבעה inactive windows הועתקו במלואם, כולל status לכל יום.
- source demo rule ID: 7; snapshot מקור מלא נשמר.
- `eligible_after_event_id`: `btc-updown-5m-1785488700`, כלומר פעיל מהאירוע הבא.
- מצב סופי: `inactive`.

לא היו שדות Volume בחוק המקור או בטבלת `rules`; לכן לא היה מסנן Volume להעתיק.

## 14. תוצאת עסקת Paper חיה

לא נפתחה עסקה, ולכן אין פרטי entry/exit/PnL חיים לדווח. זהו terminal condition תקין לפי ההנחיה: הושלמו שישה אירועי 5 דקות ללא מחיר כניסה מדויק.

| Event | Evaluations | Ask min | Ask max | עדכוני 0.74 מדויקים |
|---|---:|---:|---:|---:|
| `btc-updown-5m-1785489000` | 44 | 0.46 | 0.55 | 0 |
| `btc-updown-5m-1785489300` | 1,178 | 0.06 | 0.95 | 0 |
| `btc-updown-5m-1785489600` | 3,004 | 0.41 | 0.60 | 0 |
| `btc-updown-5m-1785489900` | 2,636 | 0.50 | 0.51 | 0 |
| `btc-updown-5m-1785490200` | 2,694 | 0.34 | 0.67 | 0 |
| `btc-updown-5m-1785490500` | 1,890 | 0.11 | 0.90 | 0 |

בכלל הניסוי נשמרו 16,179 evaluations לחוק. הסיבות כללו `BEFORE_ENTRY_WINDOW`, `ENTRY_PRICE_NOT_MATCHED` ו־`EVENT_ENDED`. crossing חי מ־0.12 ל־0.87 באירוע השישי לא פתח עסקה.

## 15. הוכחה שלא נשלחה פקודת מסחר אמיתית

- `live_orders`: 0.
- `live_order_fills`: 0.
- `live_deals`: 0.
- `real_submission_armed=false`.
- `LIVE_TRADING_ENABLED=false`.
- `LIVE_ORDER_SUBMISSION_ENABLED=false`.
- Paper engine מצהיר ומוכח בבדיקה ללא write dependencies.
- בדיקות adapter מאשרות ש־submit/cancel חסומים.

## 16. תוצאות בדיקות

- full project suite על הקוד הסופי ב־checkout הראשי: 81 passed, 0 failed, 9 subtests passed.
- בדיקות Paper על הקוד המותקן בפועל ב־`/opt/polymarket-btc-live`: 13 passed, 0 failed.
- בדיקות Paper + User WS ממוקדות לאחר תיקוני WS: 26 passed, 0 failed.
- compileall: עבר.
- `git diff --check`: עבר.
- DB integrity: עבר.
- nginx config test: עבר.
- local/public health: עבר.
- Market WS smoke: עבר, 4/4 messages, 4 snapshots מסוג book.
- אזהרות בלבד: FastAPI `on_event` deprecation ו־Starlette/httpx deprecation; אינן כשל בדיקה.

במהלך האימות הסופי הראשוני נכשלו 4 בדיקות מקומיות לא־מנוהלות בגלל דריסת התאמות read-only קודמות. ההתאמות שוחזרו מהגיבוי, מוזגו, וכל 81 הבדיקות עברו בריצה הסופית. הכשל לא הוסתר.

## 17. דברים שלא עבדו / נושאים להמשך

1. לא נפתחה עסקת Paper חיה כי Best Ask לא היה 0.74 בדיוק בששת האירועים. זה אינו כשל טכני; המנוע בדק את החוק 16,179 פעמים.
2. לכן תיקון execution price הוכח ב־unit/integration tests אך לא הופעל בעסקת LIVE Paper בניסוי הזה.
3. dynamic WS subscription לא הזרים באופן אמין assets חדשים; הוחלף ב־reconnect מבוקר והוכח בפועל לאורך האירועים הבאים.
4. קיימת אזהרת nginx על תחביר http2 מיושן.
5. קיימות אזהרות deprecation ב־FastAPI/Starlette שכדאי לטפל בהן בנפרד.
6. דפי Paper מוגנים; ללא credentials גלויים לא בוצעה כניסה ידנית ל־UI. מבנה הדפים וה־API אומת בבדיקת TestClient מלאה, ו־login/health אומתו ב־LIVE.

## 18. מצב סופי

- הפריסה הושלמה: כן.
- המערכת באוויר: כן.
- WebSocket מחובר: כן.
- Paper Engine פעיל: כן.
- נפתחה עסקת Paper אמיתית: לא; לא התקבל מחיר 0.74 מדויק בשישה אירועים.
- החוק הזמני הושבת: כן, `inactive`.
- קיימת עסקה פתוחה: לא.
- המערכת מוכנה לשימוש ידני בחוקי DEMO בתוך LIVE: כן.
- מסחר אמיתי חסום: כן.

## 19. Commit hash סופי

Commit הקוד הסופי שנבדק לפני הוספת דוח זה: `657efb1052a353955ee1f821c22a6081561117ce`.

Commit פרסום הדוח הוא הקומיט שמכיל קובץ זה ב־`main`; ה־hash המלא שלו מדווח בסיכום הצ'אט וב־GitHub. לא ניתן להטמיע בתוך תוכן commit את ה־hash של אותו commit עצמו בלי לשנות את ה־hash.

## 20. GitHub ו־branch

- Repository: `https://github.com/dviry75/polymarket-collector`
- Branch: `main`
- דוח: `https://github.com/dviry75/polymarket-collector/blob/main/polymarket-collector/LIVE_PAPER_DEPLOYMENT_AND_VERIFICATION_REPORT_HE.md`
