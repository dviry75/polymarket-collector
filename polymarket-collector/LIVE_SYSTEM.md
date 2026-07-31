# Polymarket LIVE System

The project now has a standalone LIVE FastAPI entrypoint next to the existing DEMO collector. LIVE is fail-closed by default and uses a separate SQLite database containing only `live_*` tables.

No deployment, allowance, redemption, Live Rule creation, or real order submission is part of this build.

## Service Separation

DEMO:

```text
entrypoint: app:app
service: polymarket.service
port: 8000
database: /opt/polymarket-btc/poly_data.sqlite3
```

LIVE:

```text
entrypoint: live_app:app
service: polymarket-live.service
port: 8001
database: /opt/polymarket-btc-live/poly_live.sqlite3
domain: https://live-poly.dvirtechnologies.com
```

The DEMO `init_db()` does not run LIVE migrations by default. The LIVE entrypoint initializes only the LIVE DB schema. DEMO can still be run and tested without importing or configuring the LIVE runtime.

## Safe Defaults

```text
TRADING_MODE=DEMO
LIVE_MODULE_ENABLED=false
LIVE_TRADING_ENABLED=false
LIVE_ORDER_SUBMISSION_ENABLED=false
LIVE_ADAPTER=mock
LIVE_KILL_SWITCH=true
```

Real Polymarket order submission remains blocked. `RealPolymarketTradingAdapter` returns `blocked` for write operations.

## Login

Single initial admin:

```text
LIVE_LOGIN_USERNAME=Admin@system.com
```

Required server-side secrets:

```text
LIVE_LOGIN_PASSWORD_HASH=<argon2id hash from Google Secret Manager>
LIVE_SESSION_SECRET=<64-byte random value from Google Secret Manager>
LIVE_OPERATOR_TOKEN=<server-only action token>
```

Every `/live` route requires login except `/live/login`. Public root health is separate and redacted. Sessions are persistent until manual logout, password/session-secret rotation, or `revoke all sessions`.

Write actions require:

- Login session
- CSRF token
- `X-Live-Operator-Token`

Critical actions also require password re-authentication, including Kill Switch deactivation and session revocation.

## Running Locally In Mock Mode

```bash
uvicorn live_app:app --host 127.0.0.1 --port 8001
```

Then open:

```text
http://127.0.0.1:8001/live
```

The future production URL is:

```text
https://live-poly.dvirtechnologies.com/live
```

## Database

LIVE DB:

```text
/opt/polymarket-btc-live/poly_live.sqlite3
```

LIVE tables:

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
- `live_dry_runs`
- `live_daily_limits`
- `live_backups`

There are no foreign keys or joins between DEMO and LIVE database files.

## Public Health

The standalone LIVE app exposes:

```text
GET /health
```

It returns only:

```json
{"status":"ok"}
```

or:

```json
{"status":"degraded"}
```

Detailed health is private at `/live/health`.

## Market WebSocket Paper Trading

LIVE now contains a separate, fail-closed Paper Trading path driven only by
Polymarket's public Market WebSocket. It does not import or receive a trading
adapter, `OrderManager`, private key, or authenticated CLOB credentials.

Enable it explicitly:

```dotenv
LIVE_MODULE_ENABLED=true
LIVE_EXECUTION_MODE=PAPER_TRADING
LIVE_PAPER_TRADING_ENABLED=true
POLYMARKET_MARKET_WS_ENABLED=true
LIVE_TRADING_ENABLED=false
LIVE_ORDER_SUBMISSION_ENABLED=false
LIVE_PAPER_TAKER_FEE_RATE=0.07
```

`configure()` refuses mixed Paper/real write settings. `REAL_TRADING` is not an
accepted execution mode in this build. The existing real-trading kill switch is
independent from `LIVE_PAPER_TRADING_ENABLED`.

The startup lifecycle continuously refreshes the current and next BTC five-minute
markets, subscribes to all four YES/NO token IDs, keeps the connection alive with
PING/PONG, dynamically subscribes and unsubscribes at rollover, and reconnects
with bounded exponential backoff.

Paper rules preserve the existing DEMO entry behavior: exact Best Ask match,
no entry when both sides match, one open deal per rule and event, and a new rule
waits for the next event. Exits use the purchased side's Best Bid; stop loss wins
before take profit, and official `market_resolved` messages close at payout 0/1.
The simulated financial model uses a `$1` default amount and the conservative
DEMO taker-fee formula.

Separate protected sections are available in the LIVE navigation:

- `Paper Overview`
- `Paper Rules`
- `Paper Deals`

Protected read APIs:

- `GET /live/paper/health`
- `GET /live/paper/rules`
- `GET /live/paper/deals`
- `GET /live/paper/evaluations`

Operator APIs:

- `POST /live/paper/rules`
- `POST /live/paper/rules/{rule_id}/status`

Paper history is stored in `live_market_snapshots` and
`live_rule_evaluations`; Paper rows in `live_rules` and `live_deals` are labeled
with `execution_mode=PAPER_TRADING`. These tables are also included in the LIVE
XLSX export.

## Maintenance

The Maintenance tab implements a safe `DRAINING` state machine:

1. Move to `DRAINING`.
2. Block new entries/rules.
3. Continue managing existing orders and positions.
4. Wait for the current BTC 5-minute event and open exposure to clear.
5. Run final reconciliation.
6. Report `stop_ready=true` only when exposure is `0` and there are no non-final orders or open deals.

The app never stops the process directly. Future systemd integration should use a constrained helper or `ExecStop` flow, not broad sudo.

## Backups

Backups use the SQLite backup API, temporary file plus atomic rename, gzip compression, SHA-256 checksum, audit rows, and cleanup by:

- retention: 7 days
- max total storage: 1GB
- warning threshold: 80%

Backups are stored under:

```text
/opt/polymarket-btc-live/backups
```

No real server backup is created unless an authenticated admin explicitly runs the protected backup action.

## Deployment Artifacts

Prepared but not applied:

- `deploy/polymarket-live.service`
- `deploy/nginx-live-poly.conf`
- `deploy/live.env.example`
- `deploy/DEPLOYMENT_CHECKLIST.md`

nginx should route `https://live-poly.dvirtechnologies.com` to `127.0.0.1:8001` with WebSocket upgrade headers and HTTPS redirect.

## Secrets

Do not store secrets in Git, Markdown, logs, exports, or SQLite payloads.

Google Secret Manager should hold:

- Polymarket private key
- Polymarket API key/secret/passphrase
- `LIVE_LOGIN_PASSWORD_HASH`
- `LIVE_SESSION_SECRET`
- `LIVE_OPERATOR_TOKEN`

## Rollback

1. Request Maintenance drain and wait for `stop_ready=true`.
2. Set `LIVE_MODULE_ENABLED=false`.
3. Set `LIVE_KILL_SWITCH=true`.
4. Stop or restart only `polymarket-live.service`.
5. Keep `poly_live.sqlite3` and backups for audit unless an explicit archival plan is approved.
