# בדיקות ארוכות לביצוע לאחר ה־rollout

הבדיקות הבאות לא הורצו במהלך הפריסה כדי לא לעכב או לסכן את LIVE. אין להריץ fault injection מול `/opt/polymarket-btc-live/poly_live.sqlite3`.

## 1. LIVE soak בקריאה בלבד — 24 שעות

מטרה: להוכיח שהמנגנון יציב לאורך זמן ללא restart storm, audit storm או pause/resume flapping.

יש לדגום פעם בדקה דרך IPC/DB read-only:

- `polymarket-trader.service` ו־`polymarket-dashboard.service` במצב `active/running`.
- `NRestarts=0` או ללא עלייה בלתי מוסברת.
- `recovery_engine_status=HEALTHY`.
- אין `AUTO_RECOVERY_DEGRADED`.
- כל reconciliation מסתיים `ok`, או שתקלה זמנית מתאוששת ללא מפעיל.
- אין יותר מאירוע audit אחד לכל שינוי blocker אמיתי.
- אין עלייה חריגה בקצב גידול DB, CPU או זיכרון.
- אין כניסה כאשר `pause_entries=true`.

תנאי הצלחה: 24 שעות ללא release לא בטוח, ללא התערבות ידנית בתקלה recoverable וללא storm.

## 2. Fault-injection ממושך — Mock/DB משוכפל בלבד

להריץ 100 מחזורים לכל תרחיש:

- Market WS disconnect/reconnect.
- User WS stale/reconnect + reconciliation.
- book not ready/fresh.
- heartbeat failure/success חדש.
- reconciliation rate-limit/network timeout.
- repairable gap ולאחריו clean run ללא repair.
- freshness/heartbeat flapping בזמן stability.

בכל מחזור יש לאמת:

```text
pause acquired
→ generation נשמר/גדל כראוי
→ כל blockers מוצגים
→ stability מתאפס בעת flap
→ auto-resume מתרחש רק לאחר כל הראיות
→ זמן stability אינו עולה על 5 שניות
```

תנאי הצלחה: 0 שחרורים מוקדמים, 0 שחרורי generation ישן, 0 audit/request storms.

## 3. Restart endurance — Mock/DB משוכפל בלבד

לבצע 20 restarts בכל מצב:

- `TRADING`
- `PAUSED_RECOVERING`
- `PAUSED_WAITING_STABILITY`
- `PAUSED_MANUAL_ONLY`
- לאחר clean reconciliation ולפני release

תנאי הצלחה: generation, owner, policy, acquired evidence ו־financial proof נשמרים; manual-only לעולם אינו משתחרר אוטומטית.

## 4. Security diff scan מלא

להריץ Codex Security diff scan מלא על ה־working-tree/commit הסופי כאשר מחבר Codex Security Access זמין. לכסות במיוחד:

- הרשאות resume דרך API/IPC.
- CAS ועסקאות DB.
- אפשרות ל־operator/kill-switch bypass.
- מידע רגיש ב־status/dashboard/audit.
- הזרקת reason/details לתצוגה או ללוגים.

תנאי הצלחה: אין finding ברמת reportable; כל finding מאומת מתוקן ונבדק לפני commit סופי.
