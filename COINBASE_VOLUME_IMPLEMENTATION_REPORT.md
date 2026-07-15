# Coinbase BTC Volume Implementation Report

תאריך: 16/07/2026  
Scope: `polymarket-collector` בלבד

## 1. Summary

נוסף collector עצמאי ל-Coinbase BTC-USD volume בתוך אפליקציית FastAPI/SQLite הקיימת.

קבצים שהשתנו או נוספו:

- `polymarket-collector/app.py`
- `polymarket-collector/README.md`
- `polymarket-collector/tests/test_coinbase_volume.py`
- `polymarket-collector/scripts/run_coinbase_volume_integration_test.py`
- `COINBASE_VOLUME_IMPLEMENTATION_REPORT.md`

הטבלה נפרדת כי נתוני Coinbase הם מדד חיצוני של BTC-USD, ולא חלק מ-orderbook של Polymarket. לכן הם נשמרים ב-`btc_volume_log` ולא ב-`orderbook_log`.

## 2. API

מקור הנתונים:

```text
GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300
```

פורמט candle:

```text
[time, low, high, open, close, volume]
```

בחירת הנר:

- הקוד מחשב את תחילת bucket ה-5 דקות הנוכחי לפי UTC.
- הקוד עובר על כל candles שחזרו.
- נבחר רק candle שה-`time` שלו שווה בדיוק ל-bucket הנוכחי.
- אם candle נוכחי לא נמצא, נשמרת שורת `error` ולא נשמר volume שגוי.

## 3. Database

נוספה טבלה:

```text
btc_volume_log
```

שדות:

```text
id
sampled_at
sample_bucket_at
candle_start_at
product_id
granularity_seconds
volume_btc_cumulative
volume_btc_delta
seconds_since_previous_sample
event_slug
condition_id
source
status
error
```

Indexes:

- `idx_btc_volume_log_unique_bucket`
- `idx_btc_volume_log_sampled_at`
- `idx_btc_volume_log_candle_start_at`
- `idx_btc_volume_log_event_slug`

Unique dedupe:

```text
product_id + candle_start_at + sample_bucket_at
```

Backward compatibility:

- לא נמחקו טבלאות קיימות.
- לא שונו שמות עמודות קיימות.
- `events` ו-`orderbook_log` נשארו קיימות.

## 4. Delta Logic

Baseline:

- אם אין דגימה תקינה קודמת, `volume_btc_delta = NULL`.
- אם candle השתנה, `volume_btc_delta = NULL`.

Same candle:

- אם הדגימה הקודמת התקינה היא מאותו `candle_start_at`, הקוד מחשב:

```text
current cumulative volume - previous cumulative volume
```

Negative delta:

- אם cumulative volume יורד בתוך אותו candle, נשמר baseline עם error.

Long gap:

- אם עברו יותר מ-90 שניות מהדגימה התקינה הקודמת, נשמר baseline ולא delta.

Restart:

- הקוד קורא את הדגימה התקינה האחרונה מ-SQLite.
- הוא לא מסתמך רק על זיכרון.

## 5. Dashboard

נוסף section חדש:

```text
Coinbase BTC Volume
```

כולל summary:

- latest cumulative volume
- latest delta
- last successful sample
- collector status

וטבלה של 50 הרשומות האחרונות עם:

```text
Sampled At
Candle Start
Product
Cumulative Volume BTC
Volume Delta BTC
Seconds Since Previous Sample
Event Slug
Status
Error
```

## 6. Excel

הורחב `/download.xlsx`.

נוסף sheet:

```text
btc_volume_log
```

ה-sheets הקיימים נשארו:

- `events`
- `orderbook_log`
- `btc_volume_log`

## 7. Automated Tests

```text
command:
python -m unittest discover -s polymarket-collector\tests

result:
OK

passed:
6

failed:
0
```

```text
command:
python -c "import ast, pathlib; ..."

result:
AST OK

passed:
3 Python files checked

failed:
0
```

```text
command:
python -c "import app; app.init_db(); ..."

result:
init import OK

failed:
0
```

Dashboard/health smoke check:

```text
result:
dashboard OK
health returned Coinbase fields
```

## 8. 10-Minute Test

Command:

```text
python polymarket-collector\scripts\run_coinbase_volume_integration_test.py --duration-seconds 600
```

Start time:

```text
2026-07-15T21:43:23.500669+00:00
```

End time:

```text
2026-07-15T21:53:23.512970+00:00
```

Actual duration:

```text
600.012301 seconds
```

DB path:

```text
C:\Users\ASUS\OneDrive\מסמכים\פולימרקט DB\polymarket-collector\output\coinbase_volume_test_20260715_214323.sqlite3
```

Results:

| Metric | Value |
| --- | ---: |
| Coinbase sample count | 20 |
| Polymarket sample count | 59 |
| Unique candle count | 3 |
| Baseline count | 2 |
| Valid delta count | 2 |
| Error count | 16 |
| Duplicate bucket count | 0 |
| Min sample interval | 30.00507 |
| Avg interval between valid comparison samples | 110.125267 |
| Max interval between valid comparison samples | 270.302899 |

Data quality:

- timestamps עולים.
- אין duplicate buckets.
- `candle_start_at` מיושר ל-5 דקות.
- sample ראשון בכל candle תקין נשמר כ-baseline.
- אין delta בין candles.
- אין delta שלילי תקין.
- `event_slug` נשמר כאשר היה active event.
- Polymarket המשיך לפעול במקביל.
- Coinbase errors לא עצרו את Polymarket.

## 9. Excel File

Full path:

```text
C:\Users\ASUS\OneDrive\מסמכים\פולימרקט DB\polymarket-collector\output\polymarket_coinbase_10min_test_20260715_215323.xlsx
```

File size:

```text
23283 bytes
```

Sheet names:

```text
events
orderbook_log
btc_volume_log
```

Row count per sheet:

| Sheet | Rows |
| --- | ---: |
| events | 6 |
| orderbook_log | 59 |
| btc_volume_log | 20 |

Reopen:

```text
openpyxl reopen success: true
```

## 10. Open Issues

1. Coinbase did not always return the current 5-minute candle during the candle lifetime.
   - Result: 16 rows were saved as `error`.
   - Error: `Current Coinbase candle not found`.
   - The code intentionally did not use the previous candle as a substitute.

2. Valid deltas were available only when Coinbase returned the same current candle in consecutive successful samples.
   - In the 10-minute test there were 2 valid delta rows.

3. The test proved safety behavior, but Coinbase current-candle availability may need further investigation.
   - Possible next check: whether Coinbase requires explicit `start`/`end` params for more reliable current-candle retrieval.
   - This was not added because the instruction specified the public candles endpoint with `granularity=300`.

4. No production deployment configuration was changed.

## 11. Recommendations

1. Keep the strict current-candle rule. It prevented writing incorrect volume.
2. Investigate Coinbase candle availability with explicit `start`/`end` parameters before relying on 30-second deltas.
3. Keep `btc_volume_log` separate from `orderbook_log`.
4. Monitor `error_count / sample_count` in production; high error rate means Coinbase current-candle data is not available often enough for the desired 30-second signal.
5. If 30-second delta is critical, consider a trade/ticker endpoint from Coinbase in a later phase, but only after validating requirements.

