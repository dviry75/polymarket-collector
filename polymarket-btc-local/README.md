# polymarket-btc-local

MVP מקומי בפייתון לאיסוף נתוני Polymarket BTC Up/Down 5m.

הפרויקט שומר:
- metadata של events/markets בקובץ `data/events.csv`
- snapshots מאוחדים של Orderbook + Trades בקובץ `data/event_logs.csv`
- קובץ Excel בסוף כל ריצה תחת `output/polymarket_btc_run_<timestamp>.xlsx`

אין Google Sheets, אין WebSocket, אין Database, אין Docker, ואין מסחר או auth מול Polymarket.

## התקנה

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

אפשר להריץ גם בלי `.env`; במקרה כזה ייעשה שימוש בברירות המחדל.

## הרצה

```powershell
python -m src.main
```

ברירת המחדל:
- דגימה כל 2 שניות
- משך ריצה 120 שניות

ל-run קצר:

```powershell
$env:POLL_INTERVAL_SECONDS="2"
$env:RUN_DURATION_SECONDS="30"
python -m src.main
```

## קבצי פלט

```text
data/events.csv
data/event_logs.csv
output/polymarket_btc_run_<timestamp>.xlsx
```

כל שורה ב-`event_logs.csv` היא snapshot אחד של אותו רגע.

## משתני סביבה

```env
APP_ENV=local
POLYMARKET_GAMMA_EVENTS_URL=https://gamma-api.polymarket.com/events
EVENTS_FETCH_LIMIT=100
DISCOVERY_INTERVAL_SECONDS=60
CSV_EVENTS_PATH=data/events.csv
CSV_EVENT_LOGS_PATH=data/event_logs.csv
POLL_INTERVAL_SECONDS=2
RUN_DURATION_SECONDS=120
```

## מקורות נתונים

Orderbook לפי token id:

```text
https://clob.polymarket.com/book?token_id=<UP_TOKEN>
https://clob.polymarket.com/book?token_id=<DOWN_TOKEN>
```

Trades לפי condition id:

```text
https://data-api.polymarket.com/trades?market=<CONDITION_ID>&limit=100
```

Volume חלוני מחושב רק מ-Trades API, לא מ-Gamma volume המצטבר.
