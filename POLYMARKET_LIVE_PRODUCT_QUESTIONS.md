# Polymarket LIVE Product Questions

Date: 2026-07-21

This document collects the decisions and information still needed from the owner before taking the LIVE module from local/mock readiness to a hosted product and, later, to account-connected read-only mode or real trading.

Do not put secrets in this file.

## 1. Product And Deployment Scope

1. Should LIVE run inside the existing FastAPI service, or as a separate Linux service/process?
2. Preferred URL:
   - `/live` under the existing domain
   - a separate subdomain such as `live-poly.dvirtechnologies.com`
3. Should the LIVE service use the same SQLite DB file with `live_*` tables, or a separate SQLite DB file?
4. Who is allowed to access `/live` initially?
5. Do you want read-only viewer users in addition to operator/admin users?
6. What is the acceptable maintenance window for restarts?
7. Where should backups be stored on the Linux server?

## 2. Login And Roles

1. What username should be used for the first operator?
2. How do you want to generate/store `LIVE_LOGIN_PASSWORD_HASH`?
3. Where should `LIVE_SESSION_SECRET` be stored?
4. Should there be separate roles?
   - viewer
   - operator
   - admin
5. Should operator actions require password re-entry or a second confirmation?
6. Should session TTL stay at 1 hour or be changed?
7. Should `/live/health` be private only, or should there be a separate redacted public health endpoint?

## 3. Server And Environment

1. Linux server hostname/IP.
2. Deployment user.
3. Application path on server.
4. Python version.
5. systemd service name.
6. nginx/domain routing details.
7. Where should `.env` or systemd environment files live?
8. Log directory and retention policy.
9. SQLite DB path.
10. Backup command/path.

## 4. Google Secret Manager

1. GCP project ID.
2. Service account name/email.
3. Exact secret names or prefix.
4. Which secrets will be stored there:
   - `POLYMARKET_PRIVATE_KEY`
   - `POLYMARKET_API_KEY`
   - `POLYMARKET_API_SECRET`
   - `POLYMARKET_API_PASSPHRASE`
   - `LIVE_LOGIN_PASSWORD_HASH`
   - `LIVE_SESSION_SECRET`
   - `LIVE_OPERATOR_TOKEN`
5. Should the server authenticate to GCP by service account key file or workload identity?
6. Who can rotate secrets?
7. What is the rotation procedure?

## 5. Polymarket Public Account Identity

Known public profile address:

```text
0xcE075637152167517e1492FcF5ff2D131686ee38
```

Questions:

1. Confirm this is the intended Polymarket profile address.
2. Confirm account login type: email/Magic?
3. Do you already know the proxy wallet?
4. Do you already know the funder/deposit wallet?
5. Should the UI show positions from this public profile before private credentials are configured?
6. Should account activity/history be retained in DB snapshots?
7. How often should public account identity refresh run?

## 6. Read-only Account Connection

1. Do you approve read-only private account connection after Secret Manager is configured?
2. Which read-only data should be enabled first?
   - balances
   - allowances
   - open orders
   - trades/fills
   - positions
3. Should read-only account failures activate Kill Switch?
4. Should missing allowance block Dry Run or only show a warning before real mode?
5. Should positions found remotely but not locally be shown as reconciliation gaps?

## 7. Market Data And WebSocket

1. Should active BTC 5m market discovery rely on Gamma slug generation, Gamma search, CLOB endpoint, or a hybrid?
2. How long should a public Market WS smoke test run?
3. Should Market WS stale always block entries?
4. Is REST fallback allowed to unblock entries, or visibility only?
5. Should `market_resolved` be required from WS, or is REST resolution fallback acceptable?
6. Should all raw WS payloads be retained, or only redacted/normalized event rows?

## 8. Reconciliation

Current target:

```text
LIVE_RECONCILIATION_INTERVAL_SECONDS=15
LIVE_RECONCILIATION_MAX_AGE_SECONDS=30
```

Questions:

1. Should reconciliation run whenever LIVE module is enabled, even in mock mode?
2. Should unresolved gaps block all orders or only new entries?
3. Should manual gap resolution be possible from UI?
4. Who is allowed to clear a reconciliation gap?
5. Should reconciliation runs be exported automatically?
6. How many reconciliation records should be retained?

## 9. Risk Policy

Current configured policy:

```text
LIVE_DEFAULT_TRADE_AMOUNT_USD=1
LIVE_MAX_TRADE_AMOUNT_USD=1
LIVE_MAX_TOTAL_EXPOSURE_USD=3
LIVE_MAX_OPEN_DEALS=3
LIVE_MAX_OPEN_ORDERS=3
LIVE_MAX_ACTIVE_RULES=1
LIVE_MAX_DAILY_REALIZED_LOSS_USD=10
LIVE_MAX_CONSECUTIVE_FAILED_ORDERS=3
LIVE_MAX_CONSECUTIVE_LOSING_DEALS=5
```

Questions:

1. Confirm the `$1` default and hard cap.
2. Confirm the `$3` exposure cap.
3. Confirm the `$10` daily loss limit.
4. Should daily PnL reset by `Asia/Jerusalem` day?
5. What counts as a failed order?
6. Should FOK-unfilled due to liquidity count as a system failure?
7. What counts as a losing deal?
8. Should Kill Switch require manual unlock after daily loss?
9. Should open positions continue exit management after Kill Switch?

## 10. Orders And Automation

Current policy:

- Entry: `FOK`
- Stop Loss: `FAK`
- Take Profit: internal trigger
- Stop Loss max attempts: `3`
- Stop Loss retry delay: `500ms`
- Stop Loss max slippage: `0.05`

Questions:

1. Confirm entry remains `FOK`.
2. Confirm Stop Loss remains `FAK`.
3. Confirm partial Stop Loss exit is acceptable.
4. Should Stop Loss retry all remaining size or progressively reduce size?
5. Should Take Profit remain internal, or later use resting limit sell?
6. Should manual exit be allowed before real automatic rule trading?
7. Should cancel operations stay manual only?
8. Should there be a per-market cooldown after any order failure?

## 11. Live Rules

1. Who can create a Live Rule?
2. Should creating a Live Rule always create it as `inactive`?
3. Should there be an approval screen before activation?
4. Should there be only one active Live Rule globally?
5. Should Live Rules ever be copied from DEMO rules?
6. What exact first Live Rule do you want later?
7. Should Live Rules have notes/owner fields?

## 12. UI And Product Screens

New UI screens added:

- Overview
- Operations
- Risk
- Logs
- Market Data
- Account
- Dry Run
- Reconciliation
- Orders
- Deployment

Questions:

1. Which screen should be the default first page?
2. Which KPIs are most important to see above the fold?
3. Should the UI be Hebrew, English, or mixed?
4. Should dangerous states use full-width warning banners?
5. Should audit/log rows be filterable by date/severity/action?
6. Should raw payloads be hidden behind a “show raw” button?
7. Should exports be XLSX only, CSV only, or both?

## 13. Alerts

1. Which alert channel should be used first?
   - UI only
   - email
   - Telegram
   - Slack
   - WhatsApp
2. Which alerts are critical?
3. Who receives alerts?
4. Should alerts require acknowledgement?
5. Should alert acknowledgement be audited?
6. Should repeated alerts be grouped?

## 14. Security Before Production

1. Should cookies be `Secure=true` only behind HTTPS?
2. Should CSRF tokens be required for every POST?
3. Should password hashing move from SHA-256 to bcrypt/argon2?
4. Should IP allowlisting be added for `/live`?
5. Should there be a separate admin-only audit export?
6. Should every deployment run a repository secret scan?
7. Should database backups be encrypted?
8. Should server logs redact raw Polymarket payloads?

## 15. First Manual Real Trade

This is not approved yet.

Before any future first trade, confirm:

1. Account identity is `VERIFIED`.
2. Market WS is healthy.
3. User WS is healthy.
4. Reconciliation is fresh and gap-free.
5. `$1` passes market minimum size.
6. Tick size is valid.
7. Balance is sufficient.
8. Allowance was manually configured and verified.
9. Kill Switch was tested.
10. One inactive Live Rule was reviewed.
11. Real submission was explicitly approved for a single manual trade.

