# Public integration contracts

Verified read-only on 2026-10-29. No account/trading authentication was used.

## Discovery and resolution

[Gamma discovery](https://docs.polymarket.com/market-data/discover-markets) is the
authority for series membership, explicit outcome labels/token IDs and instance
`eventStartTime`/`endDate`. Creation `startDate` is not the trading-window start.
Candidate family names are accepted only when returned series metadata confirms
the exact slug/recurrence, duration, unique condition/market ID and UP/DOWN labels.
Threshold/hourly-above markets and ambiguous matches fail closed.

A public `/series` lookup returned 20 of the 21 requested candidate families;
`SOL-1h` was not returned and remains unavailable until verified, not fabricated.
Search also returned historical events despite an active search filter, so local
metadata/status/time validation is mandatory. Hourly event slugs differ from
5m/15m slugs; no generic epoch-slug construction is used in discovery.

Official settlement requires exact stored market/condition/token matching,
`closed=true`, `umaResolutionStatus=resolved`, and one 1/0 winner. WS resolution
messages are not turned into bids or simulated sells. Public CLOB `/markets/...`
returned 403 in this environment; no bypass or authenticated fallback is attempted.

## Public Market WS

[Native public protocol](https://docs.polymarket.com/market-data/realtime-data):
market subscription, subscribe/unsubscribe updates and PING/PONG. DEMO uses only
the public market endpoint. The connection has bounded buffering; decision
overflow invalidates pending observations and reconnects. Demand changes issue
dynamic updates followed by a conservative complete resync/new generation.
Old held-instance tokens remain in the wanted set through rollover.

Exchange timestamps and local receipt times must both be fresh (default 5s).
Initial stale snapshots may load structural books but never authorize actions.
Ordered intermediate top transitions are sorted by raw wire change index and
delivered before telemetry sampling. Missing/misaligned books fail closed.

## Fees and underlying — verification in progress

Current [fee documentation](https://docs.polymarket.com/trading/fees) specifies a
price-dependent formula and five-decimal fee precision. Current Gamma BTC metadata
exposes `feesEnabled`, `feeType=crypto_fees_v2`, and `feeSchedule` rate/exponent/
takerOnly parameters. No historical constant is a fallback. Buy deduction units
and exact rounding still require independent verification before fee-enabled
entries can be marked VALID.

Current 5m/15m descriptions reference Chainlink **TWAP** streams; observed hourly
BTC/ETH/XRP/BNB descriptions reference Binance USDT 1-hour candles. These are not
interchangeable. The [current price stream](https://docs.polymarket.com/market-data/realtime-data)
uses authenticated PolyBolt; it is outside the allowed public-only boundary.
A research proxy must be labeled as a proxy, never an authoritative TWAP oracle.
## Fee and reference contracts (Phase 4)

Official authority: https://docs.polymarket.com/trading/fees and Gamma per-market
`feesEnabled` / `feeSchedule`. Formula: shares × applicable rate × p × (1-p),
documented to five decimals. No category rate is a runtime fallback. Current
documentation does not establish debit units and tie rounding sufficiently for
this gross-to-net simulation. Fee-enabled public evidence is therefore
UNSUPPORTED, including numeric zero schedules; explicit `feesEnabled=false` is
VALID zero. Evidence expires after 300 seconds. Explicit fake contracts test share
and cash debit units, sell net shares, HALF_UP/DOWN and exact Decimal arithmetic.
Enabling fee-bearing public entries requires an evidence-backed code change.

Market rules inspected on 2026-10-29 distinguish short Chainlink BTC/USD TWAP-60s
families from hourly Binance ASSET/USDT one-hour candles. Source mapping verifies
each instance description; unknown descriptions fail closed. Binance public
exchangeInfo verifies base/quote/status, klines verifies exact market-open time,
and recent trades provides exchange timestamps for entry/exit research context.
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
is the endpoint authority. The Binance source is always labelled SPOT_USDT_RESEARCH,
not the Chainlink USD TWAP oracle, and current data must be <=5 seconds old.
The migrated PolyBolt reference stream requires trading API credentials and is
excluded. Missing pair, blocked HTTP or unmatched candle returns no usable context.
