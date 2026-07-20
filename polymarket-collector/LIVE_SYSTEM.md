# Polymarket LIVE System

This project now includes an isolated `/live` area next to the existing DEMO collector. The LIVE module is built fail-closed and uses separate tables prefixed with `live_`.

## Safe Defaults

The default configuration is DEMO-only:

```text
TRADING_MODE=DEMO
LIVE_MODULE_ENABLED=false
LIVE_TRADING_ENABLED=false
LIVE_ORDER_SUBMISSION_ENABLED=false
LIVE_ADAPTER=mock
LIVE_KILL_SWITCH=true
```

Real Polymarket order submission is not enabled in this build. `RealPolymarketTradingAdapter` returns `blocked` for write operations.

## Running In Mock Mode

Set only non-secret flags:

```text
LIVE_MODULE_ENABLED=true
LIVE_ADAPTER=mock
LIVE_OPERATOR_TOKEN=<operator token stored outside Git>
```

Start the existing FastAPI app as usual:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000/live
```

Operator POST actions require the `X-Live-Operator-Token` header. If `LIVE_OPERATOR_TOKEN` is not configured, write actions remain blocked.

## Database

`init_db()` now also runs an idempotent LIVE migration. It creates only additive tables:

- `live_markets`
- `live_rules`
- `live_deals`
- `live_orders`
- `live_order_fills`
- `live_websocket_events`
- `live_positions`
- `live_account_snapshots`
- `live_reconciliation_runs`
- `live_audit_log`
- `live_system_state`

No DEMO tables are dropped or repurposed.

Before any future deployment, back up the production SQLite DB:

```bash
sqlite3 poly_data.sqlite3 ".backup 'poly_data.backup.sqlite3'"
```

## nginx/systemd

The `/live` path is served by the same FastAPI app and router. If nginx already proxies the domain to this service, no separate `location /live` is required beyond the existing proxy rule. Configure environment variables in a restricted systemd environment file; do not place secrets in the repository.

## Rollback

1. Set `LIVE_MODULE_ENABLED=false`.
2. Set `LIVE_KILL_SWITCH=true`.
3. Restart the service.
4. Keep the additive `live_` tables for audit unless an explicit archival plan is approved.

## Future Account Connection

Required before read-only account connection or trading:

- public wallet/profile/proxy address
- account type
- `signature_type`
- `funder` address
- server-side secret storage decision
- whether automated signing on the server is allowed
- external alerting channel

Never paste private keys, API secrets, passphrases, or signing material into chat, Markdown, Git, logs, or the SQLite DB.

