# Public integration contracts

Checked 2026-10-29 using unauthenticated read-only requests. No account API or
production state was accessed. Tests remain completely offline.

| Integration | Public verification | Limits |
| --- | --- | --- |
| Gamma discovery | PARTIAL: 20/21 candidate series; BTC current+next verified through implemented discovery | SOL hourly missing; not all 21 current instances verified |
| Native Market WS | VERIFIED: subscribed to BTC UP/DOWN and received two book snapshots | Not a sustained-load/soak test |
| Fee metadata | PARTIAL: current Gamma schedule and official formula/precision verified | Fee-enabled entries blocked: debit units and tie rounding unverified |
| Binance research | PARTIAL: BTC symbol, exact open candle and fresh current trade verified | Other pairs verified only when fetched; availability not assumed |
| Official resolution | VERIFIED: older BTC market closed, UMA resolved, exact labeled 1/0 winner | Recent closed market correctly remained pending; no inference |

## Discovery and official settlement

[Gamma discovery](https://docs.polymarket.com/market-data/discover-markets) supplies
verified recurring series, explicit labels/token IDs and `eventStartTime`/`endDate`.
Creation `startDate` is not the trading start. Candidate family names require exact
returned series slug/recurrence, duration and identity. Duplicate series/overlapping
current instances fail closed; no last-row or first-token guessing. Verified series
identity/provenance is stored with the definition/instance. Time buckets/slugs are
not rollover authority. Discovery evidence expires after 30 seconds.

Resolution reads the exact market ID via `/markets/{id}` (market/event slugs can
differ), matches stored condition/token identities, and requires `closed=true`,
`umaResolutionStatus=resolved`, and one exact 1/0 winner. The final sample ended
2026-10-29 15:10 UTC. Pending/ambiguous evidence does nothing. Settlement is atomic,
idempotent net-shares × 1/0; no fabricated bid, sell fee, redemption or account API.
A public CLOB market GET returned HTTP 403; no bypass/authenticated fallback used.

## Ordered public market data

[Public protocol](https://docs.polymarket.com/market-data/realtime-data): native
`wss://ws-subscriptions-clob.polymarket.com/ws/market`, public token subscriptions,
dynamic updates and PING/PONG. Demand updates conservatively request a complete
resync/new generation, retaining old held tokens. Connection/backoff/heartbeat and
bounded buffering are DEMO-owned; there is no User WS or authenticated PolyBolt.

Both exchange timestamp and local receipt must be fresh (default 5s), checked again
at decision delivery and after acquiring the DB write transaction. Structural stale
snapshots cannot authorize actions. Intermediate top transitions are emitted in
wire order before lossy telemetry, with local contiguous ordinals per generation.
JSON numeric prices parse as Decimal. Duplicated wire messages are ignored; loss,
reorder, overflow or stale delivery invalidates decisions and requires fresh data.
Two continuous healthy seconds are required after recovery. No historical replay.

## Applicable fees

Authority: [official fees](https://docs.polymarket.com/trading/fees) and Gamma's exact
market `feesEnabled`/`feeSchedule`. Documented fee is shares × rate × p × (1-p),
with five-decimal precision. The observed BTC rate is metadata, never a fallback.
There is no runtime category/default rate. Evidence expires after 300 seconds.

Current public docs do not sufficiently establish debit units and tie rounding
for this gross-to-net model. All fee-enabled public schedules, including numeric
zero, remain UNSUPPORTED. Missing schedule is UNKNOWN. Only explicit
`feesEnabled=false` supplies VALID zero. An evidence-backed code change is required
before enabling fee-bearing public entries; no configuration bypass is provided.

Explicit fake contracts verify share-deducted/cash buy fees, net-share sells,
HALF_UP/DOWN rounding and ledger reconciliation. A share buy fee is recorded at
its cash equivalent but is not charged to cash twice. Missing current exit fees
also block a fabricated book exit; official fee-free settlement is independent.

## Reference sources and research proxy

Inspected short descriptions reference Chainlink USD TWAP-60s; inspected hourly
BTC/ETH/XRP/BNB rules reference Binance ASSET/USDT one-hour candles. Mapping is
verified per instance description; unknown mapping fails closed. Exact Chainlink
TWAP is not approximated as an authoritative oracle. The migrated PolyBolt stream
requires trading API credentials and is outside this work package.

[Binance public endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
verify base/quote/status using exchangeInfo, exact market-open klines, and fresh
exchange-timestamped recent trades. Reference is explicitly `SPOT_USDT_RESEARCH`,
including for hourly families, and always labelled a research proxy. It is not a
Chainlink USD TWAP. Open/current source, pair and reference type must agree; the
baseline timestamp must equal the verified market start. Current data expires at
5s. Missing pair/candle, blocked HTTP or stale data cannot satisfy a movement rule.

Per-instance research workers retry independently. Observations share a symbol
lock and a two-second public trade-fetch cache across timeframes, preserving
exchange timestamps without multiplying requests. Available open/entry/exit values,
source IDs, timestamps and movement calculations are persisted with the
position. Official resolution, not the proxy, determines the winner. No provider
limitation is replaced by guessed identifiers, historical rates or fake prices.
