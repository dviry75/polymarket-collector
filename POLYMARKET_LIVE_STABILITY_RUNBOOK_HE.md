# Runbook יציבות — Polymarket LIVE

עודכן: 2026-08-25. סביבת היעד היא `LIVE` והשירות הראשי הוא `polymarket-trader.service`.

## מצב תקין

מצב תקין מחייב את כל התנאים הבאים יחד:

- השירות `active`, ללא restart לא מתוכנן.
- `trading_status=ENABLED`,‏ `pause_entries=false`,‏ `pause_state=TRADING`.
- `operator_action_required=false`,‏ `global_entry_halt_required=false`,‏ `incident_scope=NONE`.
- Strategy ו־reconciliation במצב `READY`; גיל reconciliation מוצלח קטן מחמש דקות.
- Market WS ו־User WS מחוברים, ספרי החובה `READY`, והתורים חסומים בגודל ואינם צוברים backlog.
- אין reconciliation במצב `running` מעל חמש דקות.
- אין quarantine פתוח, intent לא־סופי או פקודה פתוחה שאינם מוסברים.

את המצב קוראים דרך פקודת `STATUS` של ה־IPC הרשמי בלבד. אין להסיק מצב מסחר רק מ־systemd או רק משורה ב־DB.

## סוגי חסימה

### חסימה גלובלית חולפת

מיועדת לניתוק WS, נתון שוק לא־טרי, rate limit, תשובת API זמנית או reconciliation שטרם הושלם. המצב נשאר fail-closed, מבצע reconciliation וממתין לחלון יציבות. כאשר כל השערים נקיים הוא משתחרר אוטומטית. אם הוא אינו משתחרר בתוך חמש דקות לאחר שהשערים נקיים, יש לחקור `AUTO_RECOVERY_STUCK`.

### Quarantine ממוקד

תקלה מוכחת ב־position/token/event מבודדת את האובייקט בלבד. האובייקט נשאר ב־DB, בחשבונאות, ב־risk, ב־dashboard וב־reconciliation. אין למחוק אותו ואין להפוך quarantine מקומי לעצירה גלובלית בלי ראיה account-wide.

### עצירה ידנית גלובלית

`PAUSED_MANUAL_ONLY` או `operator_action_required=true` מחייבים החלטת מפעיל. אין לבצע resume רק משום שריצה אחת הסתיימה בהצלחה. נדרשת הוכחה סמכותית שאין פקודות/חשיפות סותרות, reconciliation נקי, WS תקינים, heartbeat תקין והיעדר blockers.

### סיבה לא ידועה

סיבה לא ידועה מתחילה בחסימת entries זמנית ובניסיונות classification/reconciliation. אם הוכח scope מקומי — quarantine. אם היא נשארת account/global/unknown מעבר לחלון התצפית — הסלמה למפעיל. אין להמשיך “כי לא יודעים”, ואין להפוך מחרוזת חדשה מיד ל־MANUAL_ONLY נצחי.

## בדיקה תפעולית

1. בדוק `systemctl status polymarket-trader.service` ואת `MainPID`,‏ `NRestarts` וזמן העלייה.
2. קרא `STATUS` דרך `live.ipc.TraderIPCClient('/run/polymarket/trader.sock')` עם הפקודה `STATUS` בלבד.
3. ודא WS, queues, readiness, pause, exposure, positions ו־reconciliation coordinator.
4. לקריאת DB השתמש ב־SQLite read-only (`mode=ro` ו־`query_only=ON`). אין לבצע `UPDATE`,‏ `DELETE`,‏ `INSERT` או שינוי pragma.
5. בדוק את שירות ה־soak ואת קובצי `samples.jsonl`,‏ `events.jsonl`,‏ `resources.jsonl` ו־`metadata.json`.

## Resume ידני

Resume ידני מבוצע רק דרך ה־Dashboard או פקודת `RESUME_ENTRIES` ב־IPC הרשמי. הפעולה עצמה בודקת את כל שערי ה־release ומסרבת אם ראיה חסרה. אין לעקוף את הסירוב ואין לשנות `pause_entries` ישירות במסד.

אין לבצע resume כאשר קיימים contradiction לא פתור, חשיפה לא מוסברת, פקודה remote ללא linkage, quarantine שמחייב פעולה אנושית, heartbeat כושל, geography שאינו `ALLOWED`, WS stale, reconciliation לא־טרי או kill switch פעיל.

## מדיניות repair

- Remote presence הוא ראיה חזקה; remote absence הוא ראיה חלשה עד propagation grace ותצפית סמכותית שנייה.
- תיקון אוטומטי מותר רק כאשר linkage והאמת הפיננסית מוכחים.
- כל repair נרשם עם before/after, reason, evidence ו־timestamp.
- כאשר repair אינו בטוח — quarantine או fail-closed; אין עריכת DB גולמית.

## גיבוי, restart ו־rollback

לפני restart או שינוי מסד מפעילים את `polymarket-live-backup.service`, ממתינים ל־`Result=success`, ומוודאים ברשומת `live_backups` שהסטטוס `ok`, הגודל חיובי ו־SHA-256 קיים. לאחר restart מאמתים PID חדש, `NRestarts=0`, reconciliation נקי ושחרור pause דרך המנגנון הרגיל.

שחזור backup הוא אירוע maintenance נפרד: עוצרים את השירות, מאמתים checksum, משתמשים בהליך שחזור מאושר, ומעלים fail-closed. אין להחליף קובץ SQLite בזמן שהתהליך חי. Rollback קוד נעשה ב־commit חדש/`git revert` מבוקר, בלי למחוק שינויי משתמש ובלי `reset --hard`.

## בריאות reconciliation

- הצלחה אחרונה מעל חמש דקות פותחת `RECONCILIATION_STALE_OVER_5M` עם dedup.
- הצלחה חדשה סוגרת את האירוע אוטומטית.
- ריצה מתה במצב `running` מעל חמש דקות מסומנת `failed/ORPHANED_PREVIOUS_PROCESS` בעלייה הבאה, עם audit.
- contradiction פיננסי אמיתי נשאר fail-closed; תשובת API זמנית יכולה לעבור למסלול transient רק לאחר reconciliation נקי מאוחר יותר.

## פירוש alerts

- `[CRITICAL ACTION]`: פעולה אנושית נדרשת; אין לאשר/לסגור לפני בדיקת הראיות.
- `[QUARANTINE]`: בידוד ממוקד; אינו בהכרח עצירה גלובלית.
- `[AUTO-REPAIR]`: תיקון סמכותי שבוצע ללא צורך בהחלטת מפעיל.
- Alert פתוח חוזר מעדכן occurrence/last_seen ללא flood. לאחר resolution, הישנות חדשה נפתחת מחדש עם recurrence גלוי.
- מיילים חיצוניים נשלחים רק לאחר הרשאה מפורשת ליעד ולתוכן. עד אז ה־outbox נשמר ומוצג, ללא שליחה אוטומטית.

## בדיקת תוצאות ה־soak

שירות `polymarket-soak-24h.service` הוא read-only ורץ כ־24 שעות. בסיום בדוק שאין stuck reconciliation,‏ WS storm, backlog, pause שלא השתחרר אחרי gates נקיים, duplicate order, גידול WAL/זיכרון לא חסום או contradiction חוזר מאותו אירוע ממוקד.
