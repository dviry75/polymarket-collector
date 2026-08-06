# דוח מוכנות שלב 1 — LIVE Canary יחיד

תאריך בדיקה ופריסה: 2026-08-05 (UTC)<br>
שרת: `polymarket-live-fi`<br>
כתובת ציבורית: `https://live-poly.dvirtechnologies.com`

## 1. תקציר מנהלים

המערכת הוקשחה והוכנה טכנית לניסיון Canary אמיתי יחיד, אך לא חומשה ולא נשלחה ממנה שום פקודת כתיבה ל־Polymarket. הקוד הפרוס מגביל כניסה ל־5 טוקנים ול־$5, צורך ומנטרל את ה־Canary באותה טרנזקציית SQLite שבה נשמר intent הכניסה, אינו מבצע retry עיוור, משחזר fills ופוזיציות לאחר restart, ומונע SELL מקביל כאשר ביטול אינו ודאי.

השירות פעיל ובריא מקומית ובכתובת הציבורית. Market WebSocket ו־User WebSocket במצב `CONNECTED`. כל דגלי המסחר נשארו נעולים, ואין orders, positions, intents או deals פתוחים.

## 2. Git, commit וגרסה שנבדקו

- Git: `2.43.0`
- branch: `codex/live-full-implementation-20260805`
- commit בסיס לפני העבודה: `9b19ff1`
- commit קוד שנבדק ונפרס: `7f1fb733a740ab366b659c8e4d66c71d9c5b9535`
- message: `Harden single-trade live canary readiness`
- push: נכשל; `origin` הוא HTTPS, אין credentials זמינים בשרת, ו־`gh` אינו מותקן. הקומיט המקומי נשמר ולא נמחק.
- קובץ המשתמש הלא־קשור `PRIVATE_KEY_SECURE_SETUP_GUIDE_HE.md` לא שונה ולא נכלל בקומיט.

## 3. מצב המערכת לפני השינויים

- hostname: `polymarket-live-fi`; timezone: `Etc/UTC`; NTP synchronized.
- בפועל מותקנת `Ubuntu 24.04.4 LTS`, בניגוד לפרטי הסביבה שסיפקו `Ubuntu 26.04 LTS`. אי־ההתאמה תועדה ואינה משנה את לוגיקת המסחר.
- השירות היה `active`; health מקומי וציבורי החזיר `{"status":"ok"}`.
- env: `PAPER_TRADING`, מסחר ושליחת orders כבויים, kill switch ו־pause מופעלים, Canary כבוי.
- DB: `kill_switch=true`, `pause_entries=true`, `canary_armed=false`.
- חשיפה: `open_orders=0`, `active_positions=0`, `unresolved_intents=0`, `open_deals=0`.
- גרסת secret מספר 1 הייתה נגישה דרך המשאב המדויק. ל־service account אין `secretmanager.versions.get`, ולכן מצב `ENABLED` לא נקרא כ־metadata; גישה מוצלחת לגרסה המדויקת ו־CRC תקין אימתו שהיא זמינה תפעולית.

## 4. הארכיטקטורה שנמצאה

- `LiveConfig.from_env()` טוען הגדרות ומבצע validation fail-closed.
- `RealPolymarketTradingAdapter` מרכז CLOB authenticated reads, יצירת order, ביטול ממוקד, status, trades/fills, balance/allowance, positions, heartbeat ו־redemption מוגן.
- לקוח public משמש market metadata/order book; לקוח secure נוצר רק כאשר נדרש signing או readiness פרטי מפורש.
- `LiveRepository` ו־`StrategyRepository` שומרים state, intents, fills, positions, deals, audit ו־reconciliation ב־SQLite.
- `MarketWebSocketWorker` מנהל snapshots/deltas, freshness, sequence ו־readiness; User WebSocket מקשר orders/fills לחשבון.
- `LiveStrategyRuntime` מממש entry, TP, שני שלבי stop, resolution ו־heartbeat.
- kill switch נשמר גם ב־env וגם ב־DB ונבדק לפני החלטת כניסה. `pause_entries` ו־`canary_armed` נשמרים ב־DB. unique indexes ו־event lock מונעים כניסה או exit מקבילים.

## 5. שינויים שבוצעו בקוד

- Secret Manager עם version pin קשיח, CRC32C, UTF-8 validation ו־fail-closed.
- ביטול טעינת private key אוטומטית בזמן PAPER.
- תיקון `signature_type` ל־3 עבור Deposit Wallet.
- מגבלת 5 טוקנים בנוסף ל־$5; BUY SDK amount הוא `3.8` ב־worst price `0.76`, כך שכמות החתימה אינה יכולה לעלות על 5, ו־`max_spend=5`.
- consumption אטומי ודורבילי של Canary יחד עם intent, `pause_entries=true` ו־`canary_armed=false`.
- Stop ראשון: ביטול TP ממוקד ומאומת, ואז SELL LIMIT GTC ב־0.55.
- Stop שני: ביטול GTC ממוקד, reconciliation, ואז SELL FAK עם worst-price 0.55.
- aggregation דטרמיניסטי של fills, החלת delta בלבד, שחזור entry/position לאחר restart, ויצירת TP לאחר fill מאומת.
- כל כשל reconciliation מפעיל kill switch, pause ומנטרל Canary.
- בדיקות fake-secret בלבד וסוויטת regression מורחבת.

## 6. חיבור ל־Secret Manager

המשאב המדויק הוא:

`projects/lyrical-carver-490321-t6/secrets/polymarket-live-POLYMARKET_PRIVATE_KEY/versions/1`

אין שימוש ב־`latest`. ה־payload נקרא ישירות לתהליך, נבדק מול CRC32C, מפוענח כ־UTF-8 ונשאר בזיכרון בלבד. הוא אינו מיוצא ל־environment, אינו נשמר בדיסק/DB ואינו נכתב ללוג. ה־private key לא נלקח כ־fallback מ־env. במצב PAPER הרגיל הוא כלל אינו נטען; readiness פרטי דורש `LIVE_PRIVATE_SIGNING_READINESS_ENABLED=true` מפורש. ראו [Google Secret Manager: access a secret version](https://docs.cloud.google.com/secret-manager/docs/access-secret-version) ו־[OAuth scopes for Secret Manager](https://docs.cloud.google.com/secret-manager/docs/accessing-the-api).

## 7. signer ציבורי

`0x75D4148E7220b02545f822816901836679B0F7D7`

הכתובת נגזרה בזיכרון מהמפתח, ללא הצגת המפתח, וה־preflight החזיר `VERIFIED`.

## 8. funder / proxy ציבורי

`0xcE075637152167517e1492FcF5ff2D131686ee38`

זוהי כתובת ה־Deposit Wallet שאליה משויכים balance, allowances ו־positions. signer ו־wallet שונים באופן הצפוי למבנה זה.

## 9. סוג חתימה ו־chain ID

- `signature_type=3`
- wallet type: `DEPOSIT_WALLET` / `POLY_1271`
- chain ID: `137` (Polygon)

הערך הקודם 1 לא התאים לסיווג הדטרמיניסטי של SDK. לפי [Polymarket Authentication](https://docs.polymarket.com/api-reference/authentication), type 3 הוא POLY_1271/deposit wallet, בעוד type 1 הוא POLY_PROXY.

## 10. authentication

authenticated read-only preflight עבר:

- identity: `VERIFIED`
- account mode: `FULL_TRADING`
- closed-only: `false`
- open orders: 0
- trades: 0
- positions: 0

נעשה שימוש ב־API credentials הקיימים; לא נוצרו, לא הוחלפו ולא שונו credentials. לא הופעלה פעולת deploy-wallet, order, cancel או blockchain transaction.

## 11. balances ו־allowances

- USDC balance: `$50.95`
- allowance status: `ok`
- שלושה exchange contracts החזירו allowance גדול בהרבה מ־$5.

לא בוצעו approve, deposit, transfer, withdrawal או redemption.

## 12. min order size, tick size ושעון

בדגימה לאחר הפריסה:

- market: `btc-updown-5m-1785973200`
- condition: `0x2d2dc4415e0c046ab06c1fd7bd2dcd61872c59a42bf04f26803a9162e04752ed`
- `min_order_size=5`
- `tick_size=0.01`
- accepting orders: true
- YES bid/ask: 0.43/0.44; NO bid/ask: 0.56/0.57

מינימום 5 מתאים בדיוק לתקרת 5 הטוקנים; החישוב בודק גם עלות+fee מול $5. אם min order גדול מ־5 או העלות השמרנית גבוהה מ־$5, הכניסה נחסמת. CLOB time היה `1785974086` בתוך bracket מקומי זהה של שנייה, כלומר לא נמצא clock skew מהותי.

## 13. מנגנוני Canary קשיחים

- scope קשיח ל־slug `btc-updown-5m-*` ול־token mapping מאומת.
- trigger מדויק בלבד: best ask שווה 0.74; שני צדדים בו־זמנית גורמים skip.
- attempt יחיד נשמר כ־event lock ו־unique entry intent.
- BEGIN IMMEDIATE בודק arm/consume/kill/pause ובאותה טרנזקציה שומר intent, מסמן consumed, מפעיל pause ומנטרל arm.
- rejection, zero fill, partial, timeout או תשובה לא חד־משמעית אינם מאפשרים entry נוסף.
- restart אינו מאפס `canary_consumed`.
- adapter דורש גם flags חמושים, kill switch כבוי ו־durable intent לפני POST.

## 14. full, partial ו־zero fill

- full: position נקבעת לפי fills מאומתים, נשמרים average price, fee ו־all-in cost, ואז TP רק על הכמות בפועל.
- partial: נשמרת הכמות בפועל בלבד; reconciliation מסכם fills deduplicated ומחיל רק delta חדש. יתרה מתחת למינימום מסומנת dust.
- zero: intent נסגר כ־`ZERO_FILL`, אין position ואין TP.
- TP GTC נשאר פעיל לאחר partial. Stop GTC נשאר פעיל לאחר partial עד לביטול מאומת או fill נוסף.

## 15. מניעת duplicate orders

ההגנה משולבת: unique DB indexes, stable UUIDs, event lock, durable intent לפני network, `idempotency_key`, active-exit uniqueness ו־trade-id deduplication. timeout מסומן uncertain ודורש reconciliation; אין retry אוטומטי של create order.

## 16. restart ו־reconciliation

ה־worker קורא identity, account mode, balance, allowances, open orders, trades ו־positions. remote truth גובר על state מקומי. fills מקושרים ל־intent לפי remote order ID; entry משוחזר לפוזיציה, וביציאות מוחל רק ההפרש מול הכמות שכבר יושמה. mismatch משאיר readiness ב־`NOT_READY` ומפעיל pause; exception מפעיל גם kill switch ומנטרל Canary. לאחר restart, TP חסר נוצר רק אחרי reconciliation נקי.

ב־PAPER הפרוס `reconciliation_readiness=NOT_READY` בכוונה, משום ש־private signing readiness כבוי. בדיקת readiness מפורשת ונפרדת עברה. בשלב 2 חובה להריץ reconciliation authenticated נקי ולהגיע ל־`READY` לפני חימוש.

## 17. תוצאות הבדיקות

- full suite: `111 passed, 7 warnings, 9 subtests passed`.
- focused secret/strategy suite: `30 passed`.
- fixture soak: 12 events, 9 entry intents, 3 simultaneous skips, 22 frames, 0 duplicate-entry groups, 0 parallel-exit groups, 0 active positions, integrity `ok`.
- `compileall`: עבר.
- `pip check`: `No broken requirements found`.
- migrations ו־SQLite integrity: עברו.
- `git diff --check`: עבר לאחר תיקון EOF.
- secret scan: לא נמצא private key בקבצים ששונו; שני ערכי 64-hex במסמך קיים הם message hashes, לא secrets.
- לא קיימים בפרויקט כלים מוגדרים ל־lint/type; לכן לא נטען ש־lint/mypy הורצו.
- 7 warnings הם deprecation קיימים של FastAPI lifespan ו־Starlette TestClient.

כיסוי 20 תרחישי Dry Run:

1. full entry — עבר.
2. partial entry — עבר.
3. zero fill — עבר.
4. rejection — עבר.
5. timeout + reconciliation/no blind retry — עבר.
6. restart אחרי intent ולפני response — עבר.
7. restart אחרי partial fill — עבר.
8. entry שני — נחסם.
9. TP מלא — עבר.
10. TP חלקי — עבר.
11. stop ב־0.66 — עבר כ־GTC 0.55.
12. מעבר ב־0.60 — cancel/reconcile ואז FAK מוגן 0.55.
13. cancel failure — fail-closed וללא SELL מקביל.
14. User WS disconnect — pause/recovery נבדקו.
15. Market WS stale — readiness חסום.
16. resolution עם position — local settlement נבדק; redemption אמיתי לא בוצע.
17. Secret Manager unavailable — kill/pause/disarm.
18. payload/CRC/UTF-8 לא תקינים — exception ללא leak.
19. signer mismatch — נעצר לפני יצירת client/network.
20. min order מעל תקרת 5 — נחסם.

## 18. שינויים בפריסה

לפני שינוי נשמרו:

- `/opt/polymarket-btc-live/backups/live.env.pre_stage1_20260805T232233Z`
- `/opt/polymarket-btc-live/backups/polymarket-live.service.pre_stage1_20260805T232233Z`
- `/opt/polymarket-btc-live/backups/poly_live.pre_stage1_20260805T232500Z.sqlite3` — SQLite backup תקין, mode 600, integrity `ok`.

`/etc/polymarket-live/live.env` עודכן רק בדגלים לא־סודיים: signature type 3, secret version 1, max tokens 5, signing readiness false, stop order GTC ומחיר הגנה 0.55. private key לא הוכנס לקובץ. הקוד נפרס דרך working tree והשירות עבר restart מבוקר.

## 19. מצב השירות לאחר הפריסה

- `polymarket-live.service=active`
- local health: `{"status":"ok"}`
- public health: `{"status":"ok"}`
- Market WS: `CONNECTED`
- User WS: `CONNECTED`
- journal: startup/shutdown תקינים; לא נמצא secret.
- SQLite `PRAGMA integrity_check=ok`.

## 20. דגלי בטיחות לאחר הפריסה

```text
LIVE_EXECUTION_MODE=PAPER_TRADING
LIVE_PAPER_TRADING_ENABLED=true
LIVE_TRADING_ENABLED=false
LIVE_ORDER_SUBMISSION_ENABLED=false
LIVE_KILL_SWITCH=true
LIVE_PAUSE_ENTRIES=true
LIVE_CANARY_ARMED=false
LIVE_PRIVATE_SIGNING_READINESS_ENABLED=false
```

DB:

```text
kill_switch=true
pause_entries=true
canary_armed=false
canary_consumed=false
open_orders=0
active_positions=0
unresolved_intents=0
open_deals=0
```

## 21. סיכונים ופערים שנותרו

- ה־push לא הושלם עקב היעדר GitHub credentials ו־`gh` בשרת. הקומיט המקומי קיים.
- מצב גרסת secret לא ניתן לקריאה ישירה כ־metadata בגלל הרשאת `versions.get` חסרה; exact version access ו־CRC עברו. אין להרחיב הרשאות אוטומטית.
- API key/secret/passphrase הקיימים עדיין נמצאים ב־EnvironmentFile מוגן; private key בלבד עבר ל־Secret Manager. מיגרציה עתידית של credentials אלה היא hardening רצוי, לא חסם Canary.
- reconciliation של השירות נשאר `NOT_READY` ב־PAPER בכוונה; שלב 2 חייב להפעיל readiness פרטי מפורש ולדרוש pass נקי לפני arm.
- redemption אוטומטי אמיתי נשאר חסום; אחרי resolution נדרשים authorization מפורש, gas/funder validation ו־intent דורבילי.
- לא ניתן ולא הותר לבדוק מסלול כתיבה אמיתי; כל semantics של order נבדקו מול SDK, docs ו־mocks.
- גרסת Ubuntu בפועל היא 24.04.4 ולא 26.04 כפי שסופק.

## 22. מה יקרה לאחר אישור שלב 2

לאחר פרומפט נפרד ומפורש בלבד: יבוצעו preflight ו־reconciliation authenticated; ייבדקו מחדש market scope, end time, token IDs, min size, tick, book freshness, balance ו־allowances. רק אם הכול נקי, flags ו־DB יחומשו באופן מבוקר לניסיון יחיד. trigger החוק יקבע YES/NO. בעת reserve ראשון ה־DB יצור intent ויצרוך/ינטרל Canary אטומית; BUY FAK עם worst price 0.76 יוגבל ל־3.8 amount, 5 tokens ו־$5 max. fill יאומת מול Polymarket; אם יש fill יוצב TP GTC 0.96. לאחר כל attempt המערכת תישאר paused/disarmed.

## 23. rollback מדויק

יש להריץ מה־checkout, עם השירות עצור:

```bash
sudo systemctl stop polymarket-live.service
git -C /opt/polymarket-btc-live/repo revert --no-edit 7f1fb733a740ab366b659c8e4d66c71d9c5b9535
sudo install -o root -g root -m 600 /opt/polymarket-btc-live/backups/live.env.pre_stage1_20260805T232233Z /etc/polymarket-live/live.env
sudo install -o root -g root -m 644 /opt/polymarket-btc-live/backups/polymarket-live.service.pre_stage1_20260805T232233Z /etc/systemd/system/polymarket-live.service
sudo sqlite3 /opt/polymarket-btc-live/poly_live.sqlite3 ".restore '/opt/polymarket-btc-live/backups/poly_live.pre_stage1_20260805T232500Z.sqlite3'"
sudo systemctl daemon-reload
sudo systemctl start polymarket-live.service
curl -fsS http://127.0.0.1:8001/health
```

ה־rollback משחזר snapshot של תחילת שלב 1 ולכן מוחק state חדש שנוצר אחריו; יש להשתמש בו רק כשהשירות עצור ולאחר אימות שאין חשיפה אמיתית חדשה.

## 24. החלטת מוכנות

> **האם המערכת מוכנה טכנית ל־Canary אחד: כן**<br>
> **האם נשלחה פקודה אמיתית: לא**<br>
> **האם המסחר נשאר נעול: כן**

## מקורות רשמיים

- [Polymarket — Create Order](https://docs.polymarket.com/trading/orders/create): FAK, GTC ו־worst-price semantics.
- [Polymarket — Authentication](https://docs.polymarket.com/api-reference/authentication): signature types ו־funder.
- [Google Cloud — Access a secret version](https://docs.cloud.google.com/secret-manager/docs/access-secret-version): exact version access ו־CRC.
- [Google Cloud — Secret Manager quickstart](https://docs.cloud.google.com/secret-manager/docs/create-secret-quickstart): המלצה להימנע מ־latest בייצור.
