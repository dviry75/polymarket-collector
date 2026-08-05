# תוכנית מימוש — Polymarket LIVE מלא ובטוח

תאריך: 2026-08-05 UTC  
Branch: `codex/live-full-implementation-20260805`  
בסיס: `origin/main` ב־`775fa6d95a1b4933b621290a3b0743baf6685865`

## נקודת מוצא

- השירות הפיני פעיל מ־`/opt/polymarket-btc-live/repo/polymarket-collector` עם מסד LIVE נפרד.
- `Pause Entries`/Kill Switch נשאר פעיל. אין אישור ל־Order, Cancel של Order אמיתי, Approval או Redemption.
- ה־SDK הקודם הוא `py_clob_client_v2==1.1.0`; התיעוד הרשמי העדכני דורש migration ל־`polymarket-client`.
- מסד LIVE גדל בקצב לא תקין בגלל שמירת כל payload ועדכוני snapshot חוזרים; יש לתקן לפני soak נוסף.

## שלבים וקבצים צפויים

1. בטיחות ותלויות
   - נעילת כל dependencies ו־lock/freeze מלא.
   - הרחבת `live/config.py`, `live/secrets.py` ו־deployment examples.
   - startup fail-closed, אימות signer/wallet וסריקת masking.
2. נתוני שוק
   - תיקון `live/market_websocket.py` כך ש־snapshot מחליף ספר ו־delta משנה levels נקודתיים בלבד.
   - readiness נפרד לכל token, resync אחרי reconnect/gap/out-of-order, raw payloads כבויים, snapshot רק בשינוי ועד 2Hz.
3. State ו־DB
   - migrations additive בלבד ב־`live/repository.py` לטבלאות event locks, intents, positions, reconciliations, redemption, audit timeline, alerts ו־archive jobs.
   - ערכי כסף/מחיר/כמות חדשים יישמרו כ־TEXT קנוני ויעובדו ב־`Decimal`.
4. Real Adapter ומנוע החוק
   - החלפת `live/adapters/polymarket.py` ב־adapter ל־SDK המאוחד, עם create+post דו־שלבי כדי לא להפעיל allowance recovery אוטומטי.
   - BUY/SELL FAK מוגני max/min price, SELL GTC, ביטולים ממוקדים, reads, heartbeat ו־redemption intent.
   - state machine חדש לחוק 0.74/0.96/0.66/0.60, `$5 All-In`, נעילה durable אחת ל־Event, TP/Stop ללא oversell ו־recovery idempotent.
5. Reconciliation ו־recovery
   - reconciliation ב־startup/reconnect/אחרי פעולה ובמחזוריות משתנה; remote הוא מקור האמת הכספי.
   - כל סתירה חוסמת כניסות בלבד וממשיכה ניהול פוזיציות.
6. UI ותפעול
   - Overview מורחב, Pause Entries, Emergency Close preview/confirm, Logs מקושרים, Alerts persistent, filters/export עם masking.
   - Admin + session + CSRF + re-auth בפעולות קריטיות.
7. Retention וארכיון
   - retention ל־30 יום, archive gzip אטומי ל־GCS עם manifest/checksum ו־365 יום; מחיקה רק לאחר read-back.
   - systemd timer/job ומדדי DB growth.
8. בדיקות ופריסה
   - unit/contract/recovery/UI tests, compile, secret scan, fixtures deterministic ו־Paper soak בטוח.
   - גיבוי DB וקונפיגורציה; migration על copy; restore sample; פריסה עם Pause Entries פעיל ו־Canary disarmed.

## סיכונים ונקודות עצירה

- שינוי API/SDK: ייבדק מול התיעוד והגרסה המותקנת; חוסר התאמה חוסם LIVE writes.
- allowance recovery אוטומטי של ה־SDK: לא ייעשה שימוש ב־`place_*`; רק preflight ואז `create_*` + `post_order`.
- race בין TP לביטול/Stop: mutex durable, intent יחיד ו־reconciliation לפני SELL נוסף.
- DB פעיל: כל migration נבדק קודם על עותק; אין cleanup לפני archive read-back.
- Secret Manager: ה־VM הנוכחי מדווח `ACCESS_TOKEN_SCOPE_INSUFFICIENT`; לאחר השלמת הקוד והפריסה הבטוחה תבוצע עצירה A עם הוראות מדויקות לתיקון scope/IAM וליצירת הסודות, ללא ערכים בצ׳אט.
- Canary: לא יבוצע ללא אישור מפורש חדש. השירות יישאר Paused ו־disarmed.

