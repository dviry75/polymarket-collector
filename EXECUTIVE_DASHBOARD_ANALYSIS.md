# דוח ניתוח ותכנון Dashboard ניהולי למערכת Polymarket

תאריך בדיקה: 19/07/2026  
Scope: `polymarket-collector`, `polymarket-btc-local`, קובץ SQLite ראשי וקובצי SQLite מתיקיית `output` לצורך ראיות ריצה בלבד.  
הנחת עבודה מאושרת: הדוח נוצר כחריג היחיד לאיסור שינוי קבצים. לא בוצע שינוי בקוד, ב-UI, ב-DB, ב-migrations, ב-endpoints, ב-commit או ב-push.

## 1. Executive Summary

המערכת הנוכחית מתאימה כיום כ-MVP לאיסוף נתונים בסיסי ולתפעול ניסויי של כללי מסחר מדומים, אבל עדיין אינה מוכנה כ-dashboard ניהולי אמין לקבלת החלטות פיננסיות. יש בסיס טוב: FastAPI, SQLite, איסוף אירועים מ-Polymarket, דגימות orderbook, דגימות Coinbase BTC volume, טבלאות `rules` ו-`deals` בקוד, API בסיסי, dashboard HTML ו-export ל-Excel. עם זאת, ה-DB הראשי בפועל עדיין ריק ומכיל רק שלוש טבלאות, בעוד שהקוד הנוכחי כבר מגדיר חמש טבלאות.

ההמלצה המרכזית היא לבנות את הדאשבורד הניהולי בשלבים, בלי להתחיל מגרפים מורכבים: קודם Overview שמבוסס על `deals`, `rules`, `events` ובריאות איסוף; אחר כך ניתוח ביצועים לפי חוק; אחר כך סיכון ו-drawdown; ורק בהמשך חיבור עמוק ל-volume ותנאי שוק. מבחינת סביבת השרת שלך, התכנון צריך להישאר תואם לשרת FastAPI יחיד עם SQLite מקומי בשלב הראשון, בלי להניח PostgreSQL, workers מרובים, Redis או frontend חדש. יש לתכנן כך שה-dashboard לא יאט את collector.

המסקנה הפיננסית החשובה ביותר: החישוב הקיים ב-`deals.return_percent` מתאים לתשואה על מחיר הכניסה, אך אין כיום חישוב רווח/הפסד בדולרים לפי סכום השקעה. עבור ברירת מחדל של 1 דולר לעסקה, הרווח הכספי אינו רק הפרש נקודות המחיר. יש לחשב:

```text
shares = investment_usd / entry_price
pnl_usd = shares * (exit_price - entry_price)
roi_percent = pnl_usd / investment_usd * 100
```

## 2. Current System Understanding

### מבנה הפרויקט

ה-repository כולל שני מימושים רלוונטיים:

| אזור | תפקיד | סטטוס |
| --- | --- | --- |
| `polymarket-collector/app.py` | אפליקציית FastAPI בקובץ יחיד, SQLite, collectors, dashboard, Excel, rules/deals | המימוש המרכזי והנכון להמשך |
| `polymarket-btc-local/src/*` | collector מקומי מבוסס CSV/Excel | שימושי כהיסטוריה/POC, לא מומלץ כבסיס לדאשבורד ניהולי |

הקובץ המרכזי הוא `polymarket-collector/app.py`. הוא מגדיר:

- `DB_PATH = APP_DIR / "poly_data.sqlite3"` בשורה 27.
- `FastAPI(title="Polymarket BTC Collector")` בשורה 339.
- `init_db()` בשורה 410.
- collectors ברקע: `event_collector_loop()` בשורה 1868, `orderbook_collector_loop()` בשורה 1892, `coinbase_volume_collector_loop()` בשורה 1986.
- UI קיים ב-`dashboard()` בשורה 2568 ו-`render_dashboard_content()` בשורה 2523.
- API קיים עבור `rules`, `deals`, Excel ו-health בשורות 2711-2794.

### ארכיטקטורה בפועל

```text
Polymarket Gamma API
  -> discover_open_market()
  -> events
  -> active_market בזיכרון
  -> orderbook_collector_loop()
  -> Polymarket CLOB book
  -> orderbook_log
  -> process_demo_trading_for_orderbook()
  -> deals

Coinbase candles API
  -> coinbase_volume_collector_loop()
  -> btc_volume_log

FastAPI dashboard/API
  -> קורא SQLite
  -> מציג טבלאות גולמיות
  -> מייצר Excel
```

### התאמה לסביבת השרת

התכנון צריך להתאים לסביבת השרת הקיימת:

- Python + FastAPI + Uvicorn.
- SQLite מקומי בקובץ `polymarket-collector/poly_data.sqlite3`.
- תהליך collector אחד שמריץ background tasks בתוך אותה אפליקציה.
- אין כרגע Docker, systemd, nginx, auth, Redis, Celery, PostgreSQL או frontend נפרד בקוד.
- אין כרגע מערכת migrations מסודרת; `init_db()` מבצע `CREATE TABLE IF NOT EXISTS`, `ensure_column()` ויצירת אינדקסים.

לכן שלב ראשון של dashboard צריך להיות server-side פשוט בתוך FastAPI או endpoint JSON מינימלי, עם שאילתות SQLite יעילות, בלי להוסיף תשתית כבדה. מעבר ל-PostgreSQL צריך להישאר תכנון עתידי בלבד.

## 3. Available Data

### DB ראשי בפועל

נבדק read-only:

```text
polymarket-collector/poly_data.sqlite3
```

הטבלאות שקיימות בפועל כרגע:

| טבלה | רשומות בפועל | הערה |
| --- | ---: | --- |
| `events` | 0 | קיימת וריקה |
| `orderbook_log` | 0 | קיימת וריקה |
| `btc_volume_log` | 0 | קיימת וריקה |

חשוב: ב-DB הראשי עדיין אין `rules` ו-`deals`, אף שהקוד הנוכחי יוצר אותן ב-`init_db()`. המשמעות היא שבשרת שבו האפליקציה רצה אחרי הגרסה הנוכחית, הן צפויות להיווצר, אבל בבדיקה הנוכחית הן אינן קיימות בקובץ הראשי.

### טבלאות שמוגדרות בקוד

`init_db()` ב-`polymarket-collector/app.py` מגדיר חמש טבלאות:

| טבלה | מוגדרת בקוד | קיימת ב-DB הראשי | שימוש לדאשבורד |
| --- | --- | --- | --- |
| `events` | כן | כן | אירועים, סטטוס, תוצאה, זמנים |
| `orderbook_log` | כן | כן | מחירי bid/ask, spreads, נקודות כניסה/יציאה |
| `btc_volume_log` | כן | כן | volume חיצוני של BTC-USD |
| `rules` | כן | לא ב-DB הראשי הנוכחי | הגדרת אסטרטגיות/חוקים |
| `deals` | כן | לא ב-DB הראשי הנוכחי | עסקאות מדומות וביצועים |

### נתוני ריצה מתיקיית output

קיימים שני DBים של בדיקות:

```text
polymarket-collector/output/coinbase_volume_test_20260715_214323.sqlite3
polymarket-collector/output/coinbase_reliability_20260716/coinbase_volume_reliability.sqlite3
```

הם אינם ה-DB הראשי, אך שימושיים לראיות:

| DB בדיקה | `events` | `orderbook_log` | `btc_volume_log` |
| --- | ---: | ---: | ---: |
| `coinbase_volume_test_20260715_214323.sqlite3` | 6 | 59 | 20 |
| `coinbase_volume_reliability.sqlite3` | 6 | 59 | 20 |

בנתוני הבדיקה נמצאו `orderbook_log.status = success` לכל 59 הדגימות, ללא מחירי bid/ask חריגים וללא spreads שליליים. עם זאת, כל שורות ה-volume הפנימי ב-`orderbook_log` הן אפס, כי `empty_volume_metrics()` מחזירה אפסים וה-log מדפיס `volume_collection=disabled`.

## 4. Missing or Unreliable Data

### חסר או לא קיים בפועל

| תחום | מצב | השפעה על dashboard |
| --- | --- | --- |
| עסקאות production ב-`deals` | אין ב-DB הראשי, והטבלה אף לא קיימת שם כרגע | אי אפשר לחשב ביצועים אמיתיים מה-DB הראשי כרגע |
| `rules` ב-DB הראשי | לא קיימת בפועל כרגע | אי אפשר להציג ranking אמיתי עד הרצת `init_db()` ויצירת חוקים |
| רווח/הפסד בדולרים | לא נשמר | צריך לחשב בזמן dashboard לפי סכום השקעה |
| סכום השקעה לעסקה | לא נשמר ב-`deals` | בשלב ראשון להשתמש ב-filter ברירת מחדל `$1`; בעתיד לשמור עמודה |
| עמלות | לא נשמרות | להציג P&L ללא עמלות ולהכין מודל עתידי |
| raw trades | לא נשמרים | אין audit מלא של volume או עסקאות שבוצעו בפועל |
| orderbook depth/size | לא נשמר | אי אפשר לנתח liquidity אמיתית או slippage |
| מחיר BTC/Chainlink בזמן האירוע | לא נשמר כטבלה מסודרת | קשה לנתח תנאי שוק מעבר ל-Coinbase volume |
| תוצאת event אמינה לכל אירוע | תלויה ב-`outcome_prices` ובסטטוס | צריך ולידציה לפני שימוש לסגירת עסקאות |

### נתונים מבלבלים או לא אמינים חלקית

1. `events.status` מול `events.closed`: בקובצי ה-output נמצאו חוסר התאמות בין `status` לבין `closed`. זה לא בהכרח מוכיח bug ב-production, אבל עבור dashboard ניהולי זו נקודת אמינות קריטית.
2. `price_change_points`: מחושב כ-`abs(exit - entry) * 100`, ולכן אינו כולל סימן. יש להציגו כ-magnitude בלבד, לא כנטו.
3. `return_percent`: מחושב לפי `(exit - entry) / entry * 100`, ולכן יש בו סימן. הוא מתאים לתשואה על ההשקעה, אך לא נשמרה עמודת `pnl_usd`.
4. `volume_btc_cumulative`: מצטבר בתוך candle של Coinbase בלבד, לא נפח יומי ולא נפח בין שתי דגימות.
5. `volume_btc_delta`: מתאים יותר לניתוח פעילות בטווח קצר, אבל הוא תקף רק כאשר שתי דגימות רצופות שייכות לאותו candle, הפער קטן מהסף, וה-delta אינו שלילי.
6. `orderbook_log` אינו כולל unique constraint. דגימות כפולות אפשריות תאורטית.

## 5. Financial Calculation Model

### מודל Polymarket לעסקת Long על outcome

במודל הפשוט, קונים shares של outcome במחיר בין 0 ל-1 דולר. אם משקיעים `investment_usd` במחיר כניסה `entry_price`, מספר היחידות הוא:

```text
shares = investment_usd / entry_price
```

בעת יציאה במחיר `exit_price`:

```text
exit_value_usd = shares * exit_price
pnl_usd = exit_value_usd - investment_usd
```

שקול:

```text
pnl_usd = investment_usd * ((exit_price - entry_price) / entry_price)
roi_percent = ((exit_price - entry_price) / entry_price) * 100
```

### נוסחה מלאה לברירת מחדל של 1 דולר

```text
investment_usd = 1
shares = 1 / entry_price
pnl_usd = (1 / entry_price) * exit_price - 1
roi_percent = pnl_usd * 100
```

### דוגמאות מספריות

| תרחיש | כניסה | יציאה | השקעה | יחידות | P&L דולר | תשואה |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Take profit | 0.77 | 0.90 | $1 | 1.2987 | $0.1688 | 16.88% |
| Stop loss | 0.74 | 0.65 | $1 | 1.3514 | -$0.1216 | -12.16% |
| זכייה עד resolution | 0.77 | 1.00 | $1 | 1.2987 | $0.2987 | 29.87% |
| הפסד עד resolution | 0.77 | 0.00 | $1 | 1.2987 | -$1.0000 | -100.00% |

### השוואה לחישוב הקיים ב-`deals`

הקוד הקיים:

```text
calculate_deal_metrics(entry_price, exit_price)
points = abs(exit - entry) * 100
return_percent = ((exit - entry) / entry) * 100
```

מסקנה:

- `return_percent` תואם את `roi_percent` לעסקת Long על outcome, לפני עמלות וללא slippage.
- `price_change_points` אינו מתאים לרווח נטו כי הוא מוחלט ואינו כולל סימן.
- חסר `pnl_usd`, שאותו אפשר לחשב בדאשבורד לפי `investment_usd`.
- אם בעתיד יהיו עמלות, צריך:

```text
entry_fee_usd = fee_model(entry_value)
exit_fee_usd = fee_model(exit_value)
pnl_after_fees = exit_value_usd - investment_usd - entry_fee_usd - exit_fee_usd
```

### יחס ל-YES/NO

הקוד מתייחס ל-YES ו-NO כצדדים סימטריים:

- כניסת YES לפי `up_best_ask`.
- כניסת NO לפי `down_best_ask`.
- יציאת YES לפי `up_best_bid`.
- יציאת NO לפי `down_best_bid`.

לכן נוסחת ה-P&L זהה לשני הצדדים, כל עוד `entry_price` ו-`exit_price` הם מחיר ה-outcome שנקנה.

## 6. KPI Definitions

ברירת המחדל המומלצת לשיוך תקופתי:

- KPI פיננסיים סגורים: לפי `exit_at`, כי הרווח/ההפסד מתממש בסגירה.
- KPI פעילות/כניסות: לפי `entry_at`.
- KPI אירועים: לפי `events.start_time` או `events.end_time`, לפי מטרת הגרף.
- KPI בריאות איסוף: לפי `sampled_at`.

### KPIs מרכזיים

| KPI | משמעות | נוסחה | מקור | Timestamp | מקרי קצה |
| --- | --- | --- | --- | --- | --- |
| סך עסקאות | כמה עסקאות נפתחו | `COUNT(deals)` | `deals` | `entry_at` | אם אין `deals`, להציג 0/No data |
| עסקאות סגורות | עסקאות עם תוצאה | `COUNT WHERE result IN ('win','loss')` | `deals` | `exit_at` | עסקאות פתוחות לא נכללות |
| עסקאות פתוחות | חשיפה פעילה | `COUNT WHERE result='open'` | `deals` | מצב נוכחי | לא לשייך לרווח ממומש |
| הצלחות | עסקאות מנצחות | `COUNT WHERE result='win'` | `deals` | `exit_at` | תלוי סגירה נכונה |
| כישלונות | עסקאות מפסידות | `COUNT WHERE result='loss'` | `deals` | `exit_at` | כנ"ל |
| אחוז הצלחה | יחס הצלחות | `wins / closed_deals` | `deals` | `exit_at` | אם אין סגורות, NULL |
| רווח/הפסד נטו בדולרים | תוצאה כספית | `SUM(pnl_usd)` | מחושב מ-`deals` | `exit_at` | לפי investment filter |
| תשואה כוללת | תשואה על ההשקעה | `SUM(pnl_usd) / SUM(investment_usd)` | מחושב | `exit_at` | אם השקעה 0, NULL |
| תשואה ממוצעת לעסקה | איכות עסקה ממוצעת | `AVG(roi_percent)` או `AVG(pnl_usd)` | `deals` | `exit_at` | להפריד דולר/% |
| תשואה חציונית | פחות רגישה לקיצון | median של `roi_percent` | `deals` | `exit_at` | דורש חישוב בקוד או SQL מתקדם |
| Profit Factor | יחס רווחים להפסדים | `gross_profit / ABS(gross_loss)` | `deals` | `exit_at` | אם אין הפסדים: להציג `∞` או `N/A` |
| Expectancy | תוחלת לעסקה | `win_rate*avg_win - loss_rate*avg_loss` | `deals` | `exit_at` | מומלץ לחשב בדולר, נקודות ואחוז |
| Max Drawdown | ירידה משיא הון | max של `peak_equity - equity` | `deals` | `exit_at` | דורש סדר כרונולוגי |
| רצף הפסדים | סיכון התנהגותי | max consecutive `loss` | `deals` | `exit_at` | פתוחות לא נחשבות |
| רצף הצלחות | עקביות | max consecutive `win` | `deals` | `exit_at` | כנ"ל |
| חוק מוביל | החוק עם התוצאה הטובה ביותר | sort by `pnl_usd`, עם מינימום עסקאות | `rules`+`deals` | `exit_at` | להימנע ממסקנה על N קטן |
| חוק חלש | החוק עם התוצאה הגרועה | sort ascending by `pnl_usd` | `rules`+`deals` | `exit_at` | כנ"ל |
| שינוי מתקופה קודמת | מגמה | `current - previous` | כל KPI | לפי KPI | חייב תקופות זהות |

### Profit Factor

```text
gross_profit = SUM(pnl_usd WHERE pnl_usd > 0)
gross_loss_abs = ABS(SUM(pnl_usd WHERE pnl_usd < 0))
profit_factor = gross_profit / gross_loss_abs
```

אם אין הפסדים:

- אם יש רווחים: להציג `∞` עם tooltip "אין עסקאות מפסידות בתקופה".
- אם אין עסקאות: להציג `N/A`.
- אם אין רווחים ואין הפסדים: להציג `0` או `N/A`; המלצה: `N/A`.

### Expectancy

מומלץ לחשב בשלוש צורות:

```text
expectancy_usd = win_rate * avg_win_usd - loss_rate * avg_loss_usd_abs
expectancy_points = win_rate * avg_win_points - loss_rate * avg_loss_points_abs
expectancy_percent = win_rate * avg_win_roi - loss_rate * avg_loss_roi_abs
```

לדאשבורד הראשי להציג `expectancy_usd`, כי הוא הכי ניהולי. במסך rule detail להציג גם points ו-percent.

### Max Drawdown

סדר מומלץ:

```text
closed deals ORDER BY exit_at ASC, id ASC
equity_i = SUM(pnl_usd up to i)
peak_i = MAX(equity_0..equity_i)
drawdown_usd_i = peak_i - equity_i
drawdown_percent_i = drawdown_usd_i / (initial_capital_or_peak_capital)
max_drawdown = MAX(drawdown_usd_i)
```

בגלל שהמערכת כרגע משתמשת ב-$1 לעסקה ולא מנהלת capital account, מומלץ בשלב ראשון להציג:

- Drawdown בדולרים על equity curve מצטברת.
- Drawdown כאחוז מסך השקעות מצטבר או מ-peak equity רק אם מוגדר הון התחלתי.

זמן התאוששות:

```text
recovery_time = first_time_equity_returns_to_previous_peak - drawdown_start_time
```

## 7. Filters and Grouping

### פילטרים גלובליים מומלצים בראש המסך

| פילטר | ברירת מחדל | סיבה |
| --- | --- | --- |
| טווח זמן | 7 ימים אחרונים או כל התקופה אם אין מספיק נתונים | נוח לניהול ולטסטים |
| חוק | כל החוקים | KPI ניהולי כולל |
| צד | YES+NO | לא להסתיר חצי פעילות |
| סטטוס עסקה | סגורות בלבד עבור פיננסי | למנוע P&L לא ממומש |
| סכום השקעה | `$1` | לפי דרישת המשימה |

### Advanced Filters

- חוק פעיל/לא פעיל.
- תוצאה: win/loss/open.
- סיבת יציאה: `take_profit`, `stop_loss`, `event_resolution`.
- טווח מחיר כניסה.
- טווח volume.
- שעה ביום.
- יום בשבוע.
- YES בלבד / NO בלבד.
- טווח `return_percent`.
- לכלול/לא לכלול עמלות בעתיד.

### Grouping

- לפי יום/שבוע/חודש.
- לפי חוק.
- לפי צד.
- לפי סיבת יציאה.
- לפי bucket של מחיר כניסה.
- לפי bucket של `volume_btc_delta`.
- לפי שעה ביום ויום בשבוע.

## 8. Dashboard Information Architecture

המלצה: לא להעמיס את ה-dashboard הקיים בטבלאות גולמיות. ליצור חלוקה פשוטה:

1. `Overview`
2. `Rules Performance`
3. `Risk`
4. `Market Conditions`
5. `System Health`
6. `Raw Data`

### Overview

המסך הראשי צריך לענות במהירות:

- האם המערכת מרוויחה או מפסידה?
- כמה עסקאות נסגרו?
- מה אחוז ההצלחה?
- איזה חוק מוביל?
- האם יש בעיית איסוף?

KPI cards מומלצים, 6-8 בלבד:

1. Net P&L
2. Closed Deals
3. Win Rate
4. Avg ROI / Deal
5. Profit Factor
6. Max Drawdown
7. Best Rule
8. Data Health

### Rules Performance

טבלת דירוג חוקים:

| עמודה | הערה |
| --- | --- |
| Rule | `rules.name` |
| Status | active/inactive |
| Deals | count |
| Win Rate | רק סגורות |
| Net P&L | מחושב |
| Profit Factor | מחושב |
| Expectancy | מחושב |
| Max Drawdown | לפי עסקאות החוק |

### Risk

להציג:

- Equity curve.
- Drawdown curve.
- רצף הפסדים.
- Worst day/week.
- פיזור תשואות.

### Market Conditions

להציג:

- ביצועים לפי `volume_btc_delta`.
- ביצועים לפי מחיר כניסה.
- YES מול NO.
- שעה ביום/יום בשבוע.
- spread ממוצע בכניסה אם יש `entry_orderbook_log_id`.

### System Health

אזור קטן, לא מרכזי:

- `GET /health` fields.
- זמן דגימת Coinbase אחרון.
- זמן orderbook אחרון.
- מספר שגיאות בתקופה.
- פערים בדגימה.
- DB size דרך `render_storage_status()`.

### Raw Data

להשאיר טבלאות קיימות, אבל לא כ-first screen:

- Events
- Orderbook Log
- BTC Volume Log
- Rules
- Deals

## 9. Proposed Charts and Tables

### חובה למסך הראשי

| רכיב | סוג | מטרה |
| --- | --- | --- |
| KPI Cards | cards | תמונת מצב מיידית |
| Equity Curve | line | האם הביצועים משתפרים |
| Daily/Weekly P&L | bar | קצב רווח/הפסד |
| Rules Ranking | table | איזה חוק עובד |
| Win/Loss Split | stacked bar או donut קטן | יחס תוצאות |
| System Health Strip | compact status | האם הנתונים אמינים |

### למסכי משנה

| רכיב | מסך | מטרה |
| --- | --- | --- |
| Drawdown לאורך זמן | Risk | הבנת סיכון |
| Return distribution | Risk | זנבות וקיצון |
| Heatmap שעה/יום | Market Conditions | דפוסי זמן |
| Performance by volume bucket | Market Conditions | השפעת volume |
| Performance by entry price | Market Conditions | מחירי כניסה טובים/חלשים |
| YES vs NO | Market Conditions | הטיית צד |
| Exit reason breakdown | Rules/Risk | למה עסקאות נסגרות |

## 10. Performance and Scalability

### מצב נוכחי

SQLite מספיק לשלב הנוכחי, במיוחד כי ה-DB הראשי ריק. אבל אם orderbook נכתב כל 2 שניות, הצמיחה הצפויה היא:

```text
30 samples/minute
1,800 samples/hour
43,200 samples/day
~1.3M samples/month
```

`btc_volume_log` כל 30 שניות:

```text
2 samples/minute
120 samples/hour
2,880 samples/day
~86K samples/month
```

### שאילתות שעלולות להיות כבדות

- Aggregation על `deals` לפי תקופות וחוקים.
- Equity curve ו-drawdown על כל העסקאות.
- Join של `deals.entry_orderbook_log_id` אל `orderbook_log.id`.
- בדיקות gaps על `orderbook_log`.
- Grouping לפי יום/שעה על timestamps כ-TEXT.

### אינדקסים קיימים בקוד

ב-`init_db()` קיימים:

- `idx_btc_volume_log_unique_bucket`
- `idx_btc_volume_log_sampled_at`
- `idx_btc_volume_log_candle_start_at`
- `idx_btc_volume_log_event_slug`
- `idx_rules_status`
- `idx_deals_rule_id`
- `idx_deals_event_id`
- `idx_deals_result`
- `idx_deals_one_open_per_rule`
- `idx_deals_unique_entry_sample`
- `idx_deals_rule_event_side`

חסר בקוד:

- אינדקס על `deals.exit_at`.
- אינדקס על `deals.entry_at`.
- אינדקס על `deals.result, exit_at`.
- אינדקס על `orderbook_log.sampled_at`.
- אינדקס על `orderbook_log.event_slug`.
- אינדקס על `orderbook_log.status`.

אין לבצע אותם עכשיו, אבל אלו שינויים עתידיים מומלצים.

### גישה מדורגת

שלב פשוט:

- לחשב KPI בזמן טעינת המסך על `deals`, כל עוד כמות העסקאות קטנה.
- להגביל טווח ברירת מחדל.
- לא לבצע aggregation על כל `orderbook_log` במסך הראשי.

שלב ביניים:

- Cache בזיכרון ל-Overview ל-10-30 שניות.
- endpoints נפרדים ל-dashboard summary.
- שאילתות עם אינדקסים על timestamps.

שלב עתידי:

- טבלאות aggregate יומיות/שבועיות/חודשיות.
- הפרדה בין collector לבין dashboard process.
- PostgreSQL אם SQLite הופך לצוואר בקבוק.

### מניעת האטת collector

- לא להריץ שאילתות ארוכות על `orderbook_log` בכל refresh.
- להימנע מ-`SELECT *` למסכים ניהוליים.
- להשתמש ב-LIMIT ובטווח זמן.
- לחשב היסטוריה כבדה מראש.
- להפריד read-only dashboard connection מכתיבת collector.

## 11. Data Quality Findings

### בעיות ודאיות

| ממצא | ראיה | השפעה |
| --- | --- | --- |
| DB ראשי ריק | `events=0`, `orderbook_log=0`, `btc_volume_log=0` | אין נתוני dashboard אמיתיים כרגע |
| DB ראשי חסר `rules/deals` בפועל | PRAGMA tables ב-`poly_data.sqlite3` | פער בין קוד לסכמה בפועל |
| `orderbook_log` volume פנימי כבוי | `empty_volume_metrics()` בשורה 1581; בכל output rows volume=0 | אי אפשר לנתח volume Polymarket פנימי |
| `price_change_points` ללא סימן | `abs(exit-entry)*100` בשורה 796 | אסור להציג כ-P&L נטו |
| אין `pnl_usd` | `deals` schema בשורות 519-540 | dashboard צריך לחשב |

### חשדות לבדיקה

| ממצא | ראיה | בדיקה עתידית |
| --- | --- | --- |
| חוסר עקביות `status` מול `closed` | בקובצי output נמצאו 1-2 mismatches | לבדוק אחרי ריצת production |
| קישורי FK תלויים ב-PRAGMA | יש FOREIGN KEY ב-`deals`, אבל SQLite לא אוכף בלי `PRAGMA foreign_keys=ON` | להפעיל ולבדוק |
| אין unique ל-orderbook snapshots | אין index על `orderbook_log` | לבדוק כפילויות לפי event+sample time |
| תוצאות event תלויות `outcome_prices` | `resolve_market_result()` בודק `["1","0"]` או `["0","1"]` | לבדוק פורמטים נוספים |

### התנהגות תקינה אך מבלבלת

- `volume_btc_cumulative` הוא מצטבר בתוך candle בלבד.
- `volume_btc_delta` יכול להיות `NULL` וזה תקין ב-baseline, candle חדש, פער גדול או ירידה cumulative.
- חוק פעיל חדש לא פועל על האירוע שכבר פעיל; `eligible_after_event_id` מונע כניסה באותו event.
- חוק שהופך inactive לא סוגר עסקאות פתוחות; הן ממשיכות להיסגר לפי stop/take/resolution.

### החלטות מוצר פתוחות

- האם dashboard מציג רק closed deals או גם open unrealized P&L.
- האם להציג drawdown ביחס להון התחלתי או ביחס להשקעה מצטברת.
- האם ברירת מחדל של `$1` היא רק UI filter או עמודה שתישמר בעתיד.
- האם volume dashboard מבוסס Coinbase בלבד או שגם Polymarket trades/depth יתווספו.

## 12. Required Future Changes

### Database

- לוודא ש-`rules` ו-`deals` קיימות ב-DB production אחרי startup.
- להוסיף בעתיד `investment_usd`, `shares`, `pnl_usd`, ואולי `fees_usd`.
- להוסיף אינדקסים על `deals.entry_at`, `deals.exit_at`, `orderbook_log.sampled_at`, `orderbook_log.event_slug`.
- לשקול טבלאות aggregate: daily, weekly, monthly, per-rule.
- לשקול שמירת raw trades או trade checkpoints.

### Backend

- להוסיף שכבת calculation service או פונקציות aggregation מופרדות מ-UI.
- להימנע מחישובים כבדים בתוך `dashboard()` ישירות.
- להוסיף cache קצר ל-summary.
- להוסיף health מורחב: orderbook lag, volume lag, error rate.

### API

- להוסיף בעתיד endpoints קריאה בלבד:
  - `/api/dashboard/overview`
  - `/api/dashboard/rules`
  - `/api/dashboard/risk`
  - `/api/dashboard/health`
- לתמוך בפילטרים: date range, rule, side, result, investment.

### Frontend

- להפריד בין Overview לבין Raw Data.
- להוסיף גרפים רק אחרי שיש endpoints/queries יציבים.
- לשמור על UI צפוף, ניהולי ופשוט, לא landing page.

### Calculations

- לחשב P&L לפי shares.
- להגדיר edge cases של no losses, no trades, open trades.
- לבנות equity curve לפי `exit_at`.
- להחליט כיצד להציג unrealized open trades.

### Data Quality

- להוסיף בדיקות עקביות תקופתיות.
- לבדוק mismatch בין `status`, `closed`, `accepting_orders`.
- לבדוק עסקאות בלי event/rule.
- לבדוק התאמת entry/exit למחירי orderbook.

### Tests

- להוסיף tests ל-P&L דולר.
- להוסיף tests ל-KPIs.
- להוסיף tests ל-filters.
- להוסיף tests ל-drawdown.
- להוסיף tests ל-data-quality queries.

## 13. Suggested Implementation Phases

### שלב 1: Overview בסיסי

מטרה: dashboard ניהולי ראשון, ללא שינוי DB גדול.

- לוודא `rules/deals` קיימות ב-DB בשרת.
- לחשב KPIs על `deals`.
- להשתמש ב-$1 כפרמטר חישוב.
- להציג cards ו-table קצר.
- לא לגעת ב-orderbook raw מעבר לבריאות איסוף.

### שלב 2: Rules Performance

- דירוג חוקים.
- פילטר לפי חוק.
- KPI לכל חוק.
- זיהוי חוק מוביל וחוק חלש.

### שלב 3: Risk

- Equity curve.
- Max drawdown.
- רצפי הפסדים.
- התפלגות תשואות.

### שלב 4: Market Conditions

- Volume buckets לפי `btc_volume_log.volume_btc_delta`.
- מחיר כניסה.
- שעה ביום/יום בשבוע.
- YES מול NO.

### שלב 5: Aggregations ו-Cache

- אינדקסים.
- cache קצר.
- summary tables אם כמות הנתונים גדלה.
- הכנה ל-PostgreSQL אם צריך.

## 14. Open Questions

1. האם הדאשבורד צריך להציג P&L רק לעסקאות סגורות, או גם הערכת P&L לעסקאות פתוחות לפי bid נוכחי?
2. האם `$1` לעסקה הוא רק ברירת מחדל לתצוגה, או שהמערכת אמורה בעתיד לשמור סכום השקעה שונה לכל עסקה?
3. האם Max Drawdown צריך להיות מחושב על equity curve של P&L בלבד או מול הון התחלתי מוגדר?
4. האם `volume_btc_delta` של Coinbase מספיק לתנאי שוק, או שנדרש גם volume/price מקורב מ-Polymarket trades?
5. האם dashboard יהיה פנימי בלבד בשרת מקומי, או שייחשף לרשת ודורש authentication?
6. האם המימוש canonical הוא סופית `polymarket-collector` ולא `polymarket-btc-local`?
7. האם יש דרישה לשמור היסטוריה ארוכה ב-SQLite, או שצפוי מעבר ל-PostgreSQL בשלב ידוע?

## 15. Final Recommendation

ההמלצה היא להתקדם עם `polymarket-collector` בלבד כבסיס, ולהשאיר את `polymarket-btc-local` כ-reference היסטורי. אין להתחיל מפיתוח UI גדול. קודם צריך לוודא שה-DB בשרת עבר את `init_db()` וכולל `rules` ו-`deals`, ואז לבנות Overview ניהולי שמחשב KPI פיננסיים מ-`deals` לפי מודל `$1` לעסקה.

השלב הראשון לפיתוח צריך להיות:

1. endpoint או פונקציית summary פנימית שמחזירה KPI בסיסיים.
2. חישוב `pnl_usd` בזמן הקריאה לפי `investment_usd`.
3. Overview עם 6-8 KPIs בלבד.
4. אזור health קטן.
5. השארת טבלאות raw בטאב נפרד.

### סיכום קצר

מה כבר מוכן:

- FastAPI server.
- SQLite storage.
- collectors ל-events, orderbook ו-Coinbase volume.
- schema בקוד עבור `rules` ו-`deals`.
- API בסיסי לחוקים ועסקאות.
- dashboard HTML בסיסי.
- Excel export.
- tests קיימים ל-rules/deals ול-Coinbase volume.

מה חסר:

- נתוני עסקאות ב-DB הראשי.
- טבלאות `rules/deals` בפועל ב-DB הראשי הנוכחי.
- P&L בדולרים.
- KPIs aggregated.
- אינדקסים ל-dashboard analytics.
- data quality checks מובנים.
- auth/production deployment.

מה מסוכן:

- להסתמך על `price_change_points` כרווח.
- להציג מסקנות ניהוליות כאשר ה-DB הראשי ריק.
- להריץ dashboard כבד על `orderbook_log` בזמן שה-collector כותב.
- לחשוף את ה-dashboard ללא auth אם השרת נגיש מהרשת.

ההחלטות החשובות:

- האם להציג open P&L.
- האם `$1` נשאר display-only או נשמר ב-DB בעתיד.
- האם SQLite מספיק לטווח המתוכנן.
- האם נדרש auth.
- האם Coinbase volume מספיק לתנאי שוק.

