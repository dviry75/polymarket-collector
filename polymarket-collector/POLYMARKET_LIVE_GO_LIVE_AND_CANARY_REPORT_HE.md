# דוח מעבר ל־LIVE ועסקת Canary

תאריך: 2026-08-06 UTC

## תוצאה

המערכת עברה זמנית ל־`REAL_TRADING` לאחר שכל בדיקות החובה עברו. עסקת Canary אמיתית נשלחה בעקבות הטריגר החוקי בלבד. במהלך reconciliation התגלה שה־SDK פירש את `amount=3.8` כסכום pUSD וקנה 16.521738 טוקנים במחיר נמוך, למרות intent של 5 טוקנים. זוהי חריגה ממגבלת ה־Canary.

בעקבות החריגה הופעל Kill Switch, ה־Canary נוטרל, השירות נעצר, הקוד תוקן, והמערכת הופעלה מחדש במצב `READ_ONLY` עם כל שליחת orders כבויה. לא בוצע ניסיון כניסה נוסף.

## החוק שהופעל

- שם: `BTC 5m Exact 0.74 Canary`
- מזהה: `BTC5M_EXACT_074_CANARY_V1`
- active from next event אחרי: `btc-updown-5m-1786002900`
- אירוע ראשון זכאי: `btc-updown-5m-1786003200`
- אירוע שבו התרחש trigger: `btc-updown-5m-1786005000`
- side: `YES / Up`
- trigger: Best Ask מדויק `0.74` בתוך 120 השניות האחרונות.

לא נמצאה אסטרטגיה חדשה יותר שסתרה את 0.74/0.76/0.96/0.66/0.60/0.55. מנוע האסטרטגיה הקשיח הוא החוק היחיד; `live_rules` הכיל 0 חוקים פעילים.

## בדיקות החובה לפני החימוש

- EnvironmentFile של השירות: `/etc/polymarket-live/live.env`.
- `POLYMARKET_SIGNATURE_TYPE=3`.
- funder / Deposit Wallet: `0xcE075637152167517e1492FcF5ff2D131686ee38`.
- signer: `0x75D4148E7220b02545f822816901836679B0F7D7`.
- private key נטען מ־Secret Manager בזיכרון בלבד; לא הודפס ולא נשמר.
- identity: `VERIFIED`; account mode: `FULL_TRADING`.
- balance לפני העסקה: `$50.95`.
- collateral allowance: תקין וגבוה מ־$5.
- conditional-token allowances: חיוביים עבור ארבעת הטוקנים של current/next events.
- Market WebSocket: `CONNECTED`.
- User WebSocket: `CONNECTED`.
- geography: `ALLOWED`, country `FI`.
- DB integrity: `ok`; migrations וכתיבת intents/fills/positions/audit עברו.
- לפני העסקה: 0 orders, 0 positions, 0 unresolved intents, 0 open deals.
- config validation: ללא שגיאות; max tokens 5, max spend $5, max open deals 1, max active rules 1, daily loss $10.
- נוסף לפני החימוש gate חסר של הפסד יומי למסלול האסטרטגיה הישיר.

## עסקת ה־Canary

- זמן intent: `2026-08-06T08:33:00.239370Z`.
- intent: `80575ed4-74fd-5d07-a527-f6f17d2aa036`.
- requested shares ב־DB: `5`.
- requested amount: `3.8 pUSD`.
- limit / worst price: `0.76`.
- order type: `FAK`.
- order ID מקוצר: `0xd72e…1455`.
- order ID מלא: `0xd72ece845894faa53717600a2dc205316cbd8d62c528298461599b3598431455`.
- fill מאומת: `16.521738` טוקנים במחיר ממוצע `0.23`.
- notional מקומי: `3.79999974`.
- remote trade: `a0617c54…64d3`.
- User WebSocket קלט ועיבד את אותו trade בשלושה lifecycle updates.

ה־Canary נצרך אטומית לפני השליחה:

```text
canary_consumed=true
canary_armed=false
pause_entries=true
```

## Take Profit וסיום האירוע

- TP הוצב אוטומטית ב־`0.96 GTC`.
- TP quantity: `16.52`, בהתאם לכמות שנקנתה בפועל אך מעל מגבלת ה־Canary המקורית.
- TP order מקוצר: `0xa2d3…265b`.
- ה־TP לא קיבל fill ובוטל עם סגירת השוק.
- outcome `Up` הפסיד; remote position מסומן `redeemable=true`, `current_value=0`.
- local position: `RESOLVED_LOSER`.
- realized loss שנרשם: `-$3.79999974`.
- daily loss ledger: `-$3.79999974`, losing deals: 1.
- balance read-only לאחר העסקה: `$46.945191`.
- remote open orders לאחר reconciliation: 0.
- local active positions: 0; unresolved intents: 0; open deals: 0.
- לא בוצע redemption או transaction נוסף.

## התקלה ושורש הבעיה

`AsyncSecureClient.create_market_order` עבור BUY מפרש `amount` כסכום spend. הגנת `max_price=0.76` היא worst price ואינה קובעת גודל של 5 טוקנים. כאשר fill בוצע ב־0.23, סכום של 3.8 pUSD הפך ל־16.521738 טוקנים.

לפני השליחה הקוד בדק `amount <= max_tokens * max_price`, אך בדיקה זו אינה מגבילה את מספר הטוקנים כאשר מחיר הביצוע נמוך מ־max price.

## תיקונים שבוצעו

- BUY FAK שונה ל־BUY limit signed order בגודל 5 ובמחיר 0.76; order type נשלח כ־FAK.
- נוסף invariant לפני POST על `maker_amount <= $5` ועל `taker_amount <= 5 tokens`.
- max open deals, max active rules ו־daily realized loss $10 הוגדרו immutable ב־config validation.
- נוסף daily-loss gate למסלול האסטרטגיה הישיר.
- reconciliation סוגר TP שבוטל מרחוק.
- reconciliation מזהה position resolved/redeemable ומעדכן local position/deal.
- daily realized loss מתעדכן פעם אחת בלבד, באופן idempotent.
- תוקנה יצירת strategy deal כך שאינה תלויה ב־consume_canary.

## בדיקות לאחר התיקון

- focused strategy/system: `40 passed`.
- full suite: `112 passed, 7 warnings, 9 subtests passed`.
- `compileall`: עבר.
- `pip check`: אין dependencies שבורות.
- `git diff --check`: עבר.

לא נשלח order נוסף לצורך בדיקת התיקון.

## מצב סופי

```text
LIVE_EXECUTION_MODE=READ_ONLY
LIVE_PAPER_TRADING_ENABLED=false
LIVE_TRADING_ENABLED=false
LIVE_ORDER_SUBMISSION_ENABLED=false
LIVE_KILL_SWITCH=true
LIVE_PAUSE_ENTRIES=true
LIVE_CANARY_ARMED=false
```

DB:

```text
kill_switch=true
pause_entries=true
canary_armed=false
canary_consumed=true
active_positions=0
unresolved_intents=0
open_deals=0
```

השירות `active`, health מקומי וציבורי תקינים, instance יחיד ו־0 restarts. Market/User WebSockets מחוברים ו־authenticated reconciliation במצב `READY`.

## Git

- branch: `codex/live-full-implementation-20260805`
- commit קוד: `d4b98e1a5eebc9e497966144e49fc01173117b4c`
- message: `Fix live canary token cap and reconciliation`
- push של commit הקוד לענף המרוחק: הצליח.
- commit הדוח ו־push שלו: מבוצעים לאחר חתימת דוח זה.

## החלטה

- האם נשלחה עסקת Canary אמיתית: **כן**.
- האם עמדה במגבלת 5 הטוקנים: **לא — התמלאו 16.521738**.
- האם נשלח ניסיון נוסף: **לא**.
- האם Kill Switch פעיל: **כן**.
- האם המערכת כעת במסחר אמיתי: **לא — READ_ONLY נעול**.
- האם מותר לחמש מחדש ללא בדיקה ואישור חדשים: **לא**.
