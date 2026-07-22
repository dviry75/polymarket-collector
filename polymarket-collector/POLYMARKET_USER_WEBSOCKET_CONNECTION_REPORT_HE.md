# דוח חיבור Polymarket User WebSocket

תאריך: 2026-07-22 UTC

## סיכום

המימוש הושלם, נבדק ונפרס ל-LIVE במצב read-only, אך הבדיקה החיה לא השלימה authentication: שרת Polymarket סגר פעמיים את החיבור לפני PONG או הודעת אישור, ללא close frame. המערכת עברה במכוון ל-AUTH_FAILED, הפסיקה reconnect אגרסיבי, וה-health הציבורי עבר ל-degraded. לכן אין תוצאת יציבות של 10 דקות ואין לטעון להצלחה מקצה לקצה.

## לפני העבודה

live/secrets.py קרא secrets מה-Environment אך כלל גם private key מיותר. POLYMARKET_USER_WS_URL כבר נטען. היה skeleton שבנה payload ושמר הודעה בלבד, וטבלת live_websocket_events בסיסית עם dedup לפי hash. לא היו connection lifecycle, heartbeat/PONG, stale, reconnect, resubscribe, normalization מלא או shutdown. גילוי BTC 5m היה בתהליך DEMO בלבד. Health פרטי הציג סטטוס בסיסי וה-public health הציג status בלבד.

## מה נוסף

- credentials ל-User WS בלבד מה-Environment; אין קריאה או דרישה ל-private key.
- client יחיד עם DISABLED, CONNECTING, AUTHENTICATING, CONNECTED, STALE, RECONNECTING, AUTH_FAILED, ERROR ו-STOPPED.
- auth/subscription לפי condition IDs ללא logging או persistence של auth.
- PING מיידי ואז כל 10 שניות, PONG timestamps ו-stale timeout.
- backoff+jitter, reconnect/resubscribe, dynamic subscribe/unsubscribe ו-shutdown נקי.
- שתי סגירות לפני PONG/message מסווגות auth failure שקט ומפסיקות retry אגרסיבי.
- גילוי ציבורי read-only ב-Gamma לחלון הנוכחי והבא ושמירה במסד LIVE בלבד.
- normalization ל-order/trade, YES/NO, sizes, side, price, maker/taker, transaction hash, timestamps ו-correlation.
- sanitization recursive לשדות auth, API key, secret, passphrase ו-private key.
- counters וסטטוס מפורט מאחורי Login, public health נשאר ok/degraded בלבד.
- banner: READ-ONLY — REAL TRADING DISABLED.

## קבצים

live/config.py, live/secrets.py, live/market_websocket.py, live/market_discovery.py (חדש), live/repository.py, live/router.py, live_app.py, tests/test_user_websocket.py (חדש), והדוח הזה.

## מסד ומיגרציה

ל-live_websocket_events נוספו message_type, message_status, outcome, side, price, original_size, matched_size, remaining_size, liquidity_role, transaction_hash, event_timestamp ו-correlation_json. ההיסטוריה לא נמחקה. מסד DEMO לא נקרא ולא נכתב על ידי ה-client.

## subscriptions חיים

- current: 0x70538aecf6ac5e8565dfef4b94fefd8ec5de4fb5b045f417a335b4a2a6ab4db4
- next: 0xaf90f6300ec417e1f027d0dd656e207d216594e42b4dc1b41eed166884f788d7

שניהם BTC Up or Down 5m ונשלחו כ-condition IDs, לא token IDs.

## בדיקות

python -m unittest discover -s tests: 56 tests, PASS. בדיקות User WS ייעודיות: 13, PASS. python -m compileall .: PASS. נבדקו auth ללא logging, credentials חסרים fail-closed, subscriptions, PONG/stale, shutdown, כל סטטוסי order/trade, duplicate, out-of-order history, sanitization, LIVE/DEMO separation והיעדר create/cancel. אין linter/type checker מוגדר.

## בדיקה חיה

PING אחרון: 2026-07-22T19:08:37.207955+00:00. PONG: לא התקבל. הודעה: לא התקבלה. מצב: AUTH_FAILED. שגיאה מסוננת: User WebSocket closed before authentication acknowledgement. reconnects במחזור הסופי: 1. order events: 0. trade events: 0. בדיקת 10 דקות לא בוצעה כי authentication/PONG לא הצליחו.

## Reconciliation

ה-worker הופעל אחרי ניסיונות connection וסיים ok ללא gaps. אך LIVE_ADAPTER=mock ולכן לא בוצעו GET authenticated אמיתיים ל-open orders/trades/balance/allowance. לא הוחלף adapter כדי לא להרחיב הרשאות או להסתכן ב-write. המגבלה מתועדת.

## שירותים ופריסה

בוצעו backups ל-LIVE DB ולתצורה. live.env הוא root:root mode 600 ו-EnvironmentFile מאומת. בוצע restart רק ל-polymarket-live.service. LIVE active; DEMO active ולא אותחל. הדומיין זמין; /live מחזיר 401 ללא Login. /health מחזיר degraded. הדגלים: LIVE_TRADING_ENABLED=false, LIVE_ORDER_SUBMISSION_ENABLED=false, LIVE_KILL_SWITCH=true, LIVE_ADAPTER=mock.

## חסר שנותר

יש לאמת או להנפיק מחדש את שלושת L2 API credentials ב-Environment בצד Polymarket. אין צורך ואין לבקש private key. לאחר תיקונם יש restart ל-LIVE בלבד ובדיקת PONG של 10 דקות. עד אז המערכת fail-closed.

## Git

Commit קוד: a699c66623a5fc5d271054c424388fb94173c771. Push target: origin/main (נכלל בפרסום הסופי). Secrets, Environment, DB, WAL וגיבויים אינם נכללים.

## הצהרת בטיחות

- האם נשלחה פקודת מסחר: No.
- האם בוטלה פקודה: No.
- האם נעשתה פעולה on-chain: No.
- האם נחשף secret: No.
- האם בוצעו POST/DELETE ל-CLOB: No.
