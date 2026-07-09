# Polymarket BTC Collector

FastAPI collector for Polymarket BTC Up/Down 5m markets.

It stores:
- event/market metadata in SQLite
- order book snapshots every 10 seconds
- executed trade volume per 10-second window from Polymarket Trades API
- downloadable Excel export at `/download.xlsx`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

## Endpoints

- `/` dashboard
- `/health` health check
- `/download.xlsx` Excel export

## Data

The app writes `poly_data.sqlite3` next to `app.py`.
