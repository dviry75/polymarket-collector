# Polymarket BTC Collector

FastAPI collector for Polymarket BTC Up/Down 5m markets.

It stores:
- event/market metadata in SQLite
- order book snapshots every 2 seconds
- volume columns are kept for compatibility and written as zero until the new volume logic is added
- Coinbase BTC-USD 5-minute candle cumulative volume every 30 seconds in `btc_volume_log`
- demo trading rules in `rules`
- simulated rule-based deals in `deals`
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
- `GET /rules` list rules
- `POST /rules` create an immutable rule
- `POST /rules/{rule_id}/deactivate` mark a rule inactive without deleting it
- `GET /deals` list demo deals
- `POST /generate.xlsx` create a new Excel export
- `/download.xlsx` Excel export

## Demo Trading Rules and Deals

Rules are stored in `rules` with immutable trading parameters:

- `name`
- `entry_price`
- `stop_loss_price`
- `take_profit_price`
- `max_yes_entries_per_event`
- `max_no_entries_per_event`
- `status` (`active` or `inactive`)

Only `status` can change after creation. Deactivating a rule does not delete it and does not close existing open deals. Open deals continue to be evaluated until take profit, stop loss, or official event resolution.

An active rule created while an event is active stores that current event in `eligible_after_event_id`. The rule is ignored for that event and can start only on a later event, so this behavior survives process restarts and does not depend on in-memory state.

Entry logic is evaluated after each saved `orderbook_log` sample:

- YES entry uses `up_best_ask == entry_price`.
- NO entry uses `down_best_ask == entry_price`.
- Matching is exact after Decimal normalization.
- `entry_price = 0.5` is rejected.
- If both sides match in the same sample, no deal is opened and a log line is written.
- Each rule can have only one open deal at a time.
- YES and NO entry limits are counted separately per rule and event.

Exit logic for open deals uses only the best bid for the deal side:

- YES deals check `up_best_bid`.
- NO deals check `down_best_bid`.
- Stop loss closes as `loss` with `exit_price = stop_loss_price`.
- Take profit closes as `win` with `exit_price = take_profit_price`.
- If a sampled bid jumps beyond the target, the configured target price is stored, not the sampled bid.
- If both stop loss and take profit are considered hit in one processing pass, stop loss wins.

Event resolution closes remaining open deals only when the event is already stored as closed and `outcome_prices` clearly indicates `1/0` or `0/1`. The winning side closes at `1`, the losing side closes at `0`, and `exit_orderbook_log_id` remains `NULL` when no exact order book sample represents resolution.

Demo trading does not place real Polymarket orders and does not model fees, slippage, partial fills, or market depth beyond the relevant best ask and best bid.

Excel exports now contain five sheets:

- `events`
- `orderbook_log`
- `btc_volume_log`
- `rules`
- `deals`

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
