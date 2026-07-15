# Polymarket BTC Collector

FastAPI collector for Polymarket BTC Up/Down 5m markets.

It stores:
- event/market metadata in SQLite
- order book snapshots every 2 seconds
- volume columns are kept for compatibility and written as zero until the new volume logic is added
- Coinbase BTC-USD 5-minute candle cumulative volume every 30 seconds in `btc_volume_log`
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

## Coinbase BTC Volume

The Coinbase collector uses the public Coinbase Exchange candles endpoint:

```text
https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300
```

It runs as an independent background task and writes to a separate SQLite table:

```text
btc_volume_log
```

Default environment variables:

```text
COINBASE_CANDLES_URL=https://api.exchange.coinbase.com/products/{product_id}/candles
COINBASE_PRODUCT_ID=BTC-USD
COINBASE_CANDLE_GRANULARITY_SECONDS=300
COINBASE_VOLUME_POLL_INTERVAL_SECONDS=30
COINBASE_REQUEST_TIMEOUT_SECONDS=10
COINBASE_MAX_DELTA_GAP_SECONDS=90
COINBASE_MISSING_CANDLE_RETRY_COUNT=2
COINBASE_MISSING_CANDLE_RETRY_DELAY_SECONDS=2
```

The candles request uses an explicit current-candle range:

```text
start=<current UTC 5-minute candle start>
end=<current UTC time>
```

If the current candle is not returned, the collector performs up to 2 short retries. It still refuses to use the previous candle as a substitute.

Automated tests:

```bash
python -m unittest discover -s polymarket-collector/tests
```

Bounded integration test:

```bash
python polymarket-collector/scripts/run_coinbase_volume_integration_test.py --duration-seconds 600
```

## Data

The app writes `poly_data.sqlite3` next to `app.py`.
