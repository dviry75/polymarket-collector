# Polymarket DEMO V2 — Source of Truth

## Purpose
Simulated trading/research using live Polymarket data. No real order submission.

## Markets
21 recurring Up/Down markets: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB x 5m/15m/1h. Exclude hourly “above X” threshold markets.

## Rule fields
Name; selected markets; exact entry trigger; trade size in shares (min 5); entry frequency per market instance (default 1); optional TP; optional SL; weekly schedule; optional multiple entry windows inside each market; optional underlying-asset movement filters; enabled/disabled.

## Entry
Entry only when observed best ask equals configured trigger exactly using normalized decimal semantics. If trigger=0.74 and feed goes 0.75→0.73, no trade. If first fresh snapshot after subscribing/recovering is exactly 0.74 and rule is eligible, entry is allowed. Either UP or DOWN may trigger.

## Frequency and re-arm
Frequency is shared across both sides for a rule+market-instance. Multiple positions may remain open concurrently. Counter resets next market instance. Repeated messages at unchanged trigger price do not create repeated entries; price must leave and return.

## Schedule
All schedules Asia/Jerusalem. Multiple ranges per weekday; ranges may cross midnight; no schedule=24/7. Schedule affects new entries only.

## In-market windows
Zero or more windows measured from market start, e.g. 5m 03:00–05:00; 1h 00:00–10:00, 25:00–35:00, 50:00–60:00. No windows means whole market. Entry requires both general schedule and in-market eligibility.

## TP/SL
Optional. Evaluate purchased side best bid. If feed jumps beyond threshold, exit at actually observed best bid, not configured threshold. Example SL 0.66, 0.68→0.63 => exit 0.63. TP 0.96, 0.94→0.97 => exit 0.97. Each position independent.

## Resolution
Open position at official resolution: winner 1.00, loser 0.00. Exit reason MARKET_RESOLUTION. No fake order-book sell.

## Rule lifecycle
Create/edit/enable/disable/duplicate/delete/archive. Edits affect future entries only. Every position preserves entry-time rule snapshot/version. Disable/delete stops new entries only; existing positions continue. Preserve historical attribution; prefer soft-delete/archive.

## Fees/accounting
Trade size is configured in gross shares. Model current/applicable Polymarket market fee behavior, not a historical average when accurate fee data exists. BUY deducts cash and fee may reduce net shares. SELL uses net shares, observed exit bid, sell fee, net proceeds. Persist gross shares, entry notional, buy fee, net shares, gross exit proceeds, sell fee, total fees, net proceeds, net P&L. If required fee data is unavailable/unreliable, new entries fail closed until healthy.

## Wallets
Global wallet starts at $0 with no lower bound. Entry deducts cash; exit/resolution returns cash. Also expose a per-rule wallet. Filtered KPI wallet may represent scoped net cash flow for the filtered set.

## Classification
Closed trade: positive net P&L=positive/win; negative=negative/loss; zero=break-even. Break-even neither positive nor negative and excluded from success-rate denominator. Open trades excluded.

## Underlying asset price context
Persist where available: underlying/reference at market open, at entry, at exit, timestamps, source/reference type. Derived values must be reproducible: open→entry absolute/%; open→exit; movement during trade. Do not assume all market/timeframe families use identical source.

## Underlying movement filters
Optional rule conditions supporting percentage or absolute price movement; greater-than/less-than/between; positive/negative/either-absolute. Persist observed values used for decision, not only pass/fail.

## Listening policy
Listen to a market only when at least one enabled rule is currently eligible to enter there OR an open position needs monitoring. Otherwise unsubscribe is allowed. On resume, use only fresh data from that point.

## No trade backfill
Never reconstruct entry/TP/SL events that happened during a data gap. If reconnect snapshot itself satisfies a condition, act on that fresh snapshot. Official verified resolution may still close already-open positions.

## Health/self-healing
Expose READY/DEGRADED/NOT_READY. Monitor market WS connectivity and actual data freshness, underlying feed freshness, discovery/resolution, fee-data readiness, DB and rule engine/worker health. Critical faults fail closed for new entries. Automatically reconnect/re-subscribe/re-resolve/retry. Resume only after required dependencies become healthy/stable. Do not fabricate missed events.

## Persistence
Do not auto-prune research history. Persist rules/versions, market instances, trades/positions, wallet ledger, fees, underlying observations, trigger/exit provenance and relevant health incidents. High-volume raw telemetry may be stored efficiently; preserve research-relevant history.

## Reset
Reset Trading Data clears simulated trades/wallet/statistics but keeps rules. Factory Reset Demo also resets rules. Authenticated explicit confirmation required. Never affect LIVE.

## Auth
Use same credential source/values as LIVE where practical; separate DEMO login/session. Never expose credential values.

## Frontend boundary
Frontend is owned by a separate UI agent. Codex/backend work must not implement DEMO UI. Provide stable documented APIs for KPI, filters, trades, rules, live prices and health.

## Deployment/isolation
DEMO V2 lives in this repo but must have separate app entrypoint, service, DB, config/env, logs/runtime state and reverse-proxy route. Planned route: https://demo-poly.dvirtechnologies.com/demo/. No real-trading adapters/order submission in DEMO execution.
