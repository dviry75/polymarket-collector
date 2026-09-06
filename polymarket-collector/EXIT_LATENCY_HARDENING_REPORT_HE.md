# קשיחות זמן־יציאה: "איחור ביצוע" בסטופלוס

ענף: `exit-latency-hotpath` (מסתעף מ־`fd44ca5`, הפריסה החיה של exit-forensics).

## מה נמצא

בטבלת "היסטוריית עסקאות" בדאשבורד, הסטטוס **"איחור ביצוע"** =
`live_strategy_exit_audit.execution_latency_ms` — הזמן מרגע נעילת ה־STOP/EMERGENCY
(`LATCH_MARKET_EXIT`) ועד שליחת פקודת ה־SELL ל־CLOB.

הנתונים בטבלה מסומנים `RECONSTRUCTED_PARTIAL` (שוחזרו מ־`backfill_stop_exit_audit.py`),
והקלסיפייר הוא v1 לא־מאומת — **אבל האיחור אמיתי**. מעקב ישיר ב־`live_audit_timeline`
(אותו שעון) על `btc-updown-5m-1788699900`:

| שלב | חותמת זמן |
|---|---|
| `LATCH_MARKET_EXIT` (מילוי כניסה 0.45 מול signal 0.74) | 13:09:11.481 |
| `SELL_MARKET_FAK … EMERGENCY_INVALID_ENTRY_OPTIMISTIC_SUBMIT` | 13:09:20.546 |
| **פער** | **9.06 שניות** |

טווח נצפה: 7–17 שניות, על 111 יציאות (75 ACCEPTABLE + 36 BAD_EXIT). ב־BAD_EXIT
המחיר כבר קרס (0.03 / 0.09 / 0.28) עד שה־SELL יצא.

## שורש הסיבה

1. **חסימת head-of-line בתור הפריימים.** `_frame_worker` יחיד מושך פריימים קריטיים
   מ־`_critical_frames` (deque לא־חסום, FIFO קשיח, ללא coalescing) ומעבד כל אחד
   תחת `self._event_locks[event_id]`. `_exit_supervisor_loop` — שאמור לשלוח את
   ה־SELL תוך 250ms — תופס את **אותו lock**. מהלוגים בחלון האירוע: 31 פריימים
   נכנסו לתור מול 12 שעובדו ב־30 שניות; פערים של 6.4s ו־9.8s בין
   `CRITICAL_FRAME_PROCESSING` עוקבים; `ENTRY_074 outcome=BLOCKED reason=EVENT_LOCKED`
   באמצע. שוק 5 דקות מייצר פריימים קריטיים משני הטוקנים (YES+NO) במקביל, וספר
   מתנודד סביב 0.66/0.74 מייצר קצה חדש בכל חצייה.

2. **סריקה לא־מאונדקסת תחת נעילת כתיבה.** `record_authoritative_auto_repair`
   הריץ `SELECT COUNT(*) FROM live_audit_log WHERE action=? AND occurred_at>=?`
   (~1 שנייה על 7.3M שורות) בתוך `BEGIN IMMEDIATE`. כל auto-repair של רקונסיליאציה
   עצר את כתיבות ה־`timeline()` של ה־frame worker — ולכן גם SELL נעול — לשנייה.

3. **מגבר: תפיחות DB.** `poly_live.sqlite3` = 5GB. `live_audit_log` (יומן שינויי
   מצב key/value) = 7.3M שורות וגדל במיליונים ליום (`set_market_ws_last_message_at`
   לבד = 2.6M — שורת audit לכל heartbeat של ה־WS). כל stall של נעילה מתארך פי כמה.

## מה שונה בענף הזה

### B — coalescing של תור הפריימים הקריטיים (`live/strategy_runtime.py`, `live/config.py`)

פריים קריטי שממתין בתור וחולק `(condition_id, trigger_types, readiness)` עם פריים
חדש נכנס — מוחלף במקומו בפריים החדש (שומר על מקום ה־FIFO). ההצדקה: חובת ה־STOP
דורסת (עמידה ב־`stop_stage` ב־DB, בבעלות הסופרווייזר), וקצה ENTRY חסום ע"י בדיקת
signal-TTL — כך שרק הפריים העדכני ביותר לכל קצה הוא בר־פעולה. `readiness` בתוך
המפתח כדי שפריים `NOT_READY` (עדות fail-closed) לעולם לא יוחלף ע"י `READY` מאוחר.

תקרת עומק בטוחת־STOP (`_enforce_critical_queue_cap` — מפנה רק פריימים ללא STOP),
ומטריקה `critical_frames_coalesced`.

דגלים (ברירת מחדל: פעיל):
- `LIVE_CRITICAL_FRAME_COALESCE_ENABLED` (bool, `true`)
- `LIVE_CRITICAL_FRAME_MAX_DEPTH` (int, `24`)

### C — הקלה על נתיב ה־DB החם (`live/repository.py`, `live/strategy_repository.py`)

- אינדקס חדש `idx_live_audit_log_action_time` על `live_audit_log(action, occurred_at)`
  (מיגרציה אדיטיבית, בטוחת rollback). ה־COUNT הופך ל־COVERING INDEX search תת־מילישנייה.
- ב־`record_authoritative_auto_repair`: ה־COUNT יוצא מחוץ ל־`BEGIN IMMEDIATE` —
  INSERT+commit, ואז ספירה עם הנעילה משוחררת, ואז txn קצר לעדכון ה־telemetry.

### D — גיזום היומנים (`scripts/prune_audit_log.py`, לא רץ)

כלי אופרטור, off-hot-path, dry-run כברירת מחדל. מעביר שורות ישנות מ־`live_audit_log`
ו־`live_audit_timeline` ל־DB ארכיון צמוד, מוחק ב־batches של 20K עם `busy_timeout`
קצר (כתיבות ה־trader לא נרעבות), מאמת ספירה לפני כל DELETE. שומר תמיד 3 ימים
אחרונים ואת שורות ה־`live_audit_log` שאינן `set_*` (auto-repair, quarantine,
פעולות אופרטור).

```bash
# תצוגה בלבד
python scripts/prune_audit_log.py --db /opt/polymarket-btc-live/poly_live.sqlite3
# ביצוע (ה־trader יכול להישאר רץ)
python scripts/prune_audit_log.py --db /opt/polymarket-btc-live/poly_live.sqlite3 \
    --older-than-days 7 --apply
```

DELETE משחרר דפים לשימוש חוזר (הגידול נעצר) אך לא מכווץ את הקובץ — `VACUUM` מלא
דורש חלון תחזוקה עם ה־trader מושבת.

## פריסה

הענף לא נפרס. הוא מסתעף מ־`fd44ca5` (מה שרץ חי), אז פריסה מעליו מוסיפה רק:
מיגרציה אדיטיבית אחת (`CREATE INDEX IF NOT EXISTS` — ריצה חד־פעמית על 7.3M שורות,
~10–30s תחת נעילת כתיבה במהלך `migrate` בהפעלה, לפני שה־trader חי) + שינוי לוגיקה
ב־hot path של הפריימים.

לפני restart: לוודא `kill_switch` / מצב פוזיציות (המערכת כרגע עצורה על kill_switch
של אופרטור). אחרי restart, לאמת מול לכידות `ExitEvidenceCollector` **חיות** (כרגע
0 שורות אמיתיות; כל 324 משוחזרות): לחפש `live_strategy_exit_audit` עם
`evidence_quality != 'RECONSTRUCTED_PARTIAL'` ו־`execution_latency_ms` — היעד הוא
מתחת ל־`LIVE_EXIT_SUPERVISOR_STOP_TO_SUBMIT_SLA_SECONDS` (2s).
מטריקות ריצה: `critical_frames_coalesced`, `critical_queue_depth`,
`max_critical_queue_depth`, `exit_supervisor_runs`.

## המשך (לא בענף הזה)

- **A — ניתוק ה־SELL הנעול מ־`_event_locks` המשותף** (מסלול מהיר ל־`stop_stage>=1`).
  האימפקט היחיד הגדול ביותר; נוגע ב־hot path של כסף אמיתי — דורש תכנון ואישור נפרד.
- **צמצום firehose של `live_audit_log`**: לדלג על שורת audit למפתחות timestamp
  טהורים (`*_last_message_at`, `*_last_successful_heartbeat_at`) ב־`set_states_on_connection`.
  משנה סמנטיקת audit גלובלית — עדיף מאחורי דגל, בשינוי נפרד.
- **הפרדת `CRITICAL_TRIGGER_LIFECYCLE`** ל־DEBUG / דגימה: כרגע עשרות שורות `warning`
  לפריים.
