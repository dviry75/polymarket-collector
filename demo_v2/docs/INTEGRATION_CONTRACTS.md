# Public integration contracts — Phase 9

Verification date: 2026-10-30 (Asia/Jerusalem). Public snapshot started at
2026-10-30T09:09:14.146105+00:00 UTC. Status is evidence at observation time, not a
promise that the same instance remains current. No authenticated Polymarket call,
proxy, User-Agent spoof, order/cancel/redeem, production resource or deploy was used.

Evidence: `../tests/fixtures/phase9_public_evidence.json`, containing exact external
market/condition/UP/DOWN identities, aware start/end/discovery timestamps, duration,
current/next mapping, final historical resolution, fee metadata, reference context,
HTTP outcomes, public WS decisions and local API smoke results. The fixture is
replayed offline by `test_phase9_release.py`; tests make no network calls.

## All 21 families

[Gamma discovery](https://docs.polymarket.com/market-data/discover-markets) is the
instance authority. Every row has one unique current instance, one next instance
with the same verified duration, contiguous end/start, explicit UP/DOWN labels,
distinct tokens/conditions, and a separately checked historical final 1/0 winner.
All current markets were accepting orders, public protocol `v1` (CTF asset IDs),
fee-enabled and unresolved. Full token IDs/timestamps are retained in the fixture.

| Family | Status | Series ID | Current market ID | Next market ID | Current UTC window | Older resolution |
| --- | --- | --- | --- | --- | --- | --- |
| BTC-5m | VERIFIED | 10684 | 6382438 | 6382465 | 09:05–09:10 | verified 1/0 |
| BTC-15m | VERIFIED | 10192 | 6382435 | 6382477 | 09:00–09:15 | verified 1/0 |
| BTC-1h | VERIFIED | 10114 | 6337334 | 6337936 | 09:00–10:00 | verified 1/0 |
| ETH-5m | VERIFIED | 10683 | 6382445 | 6382461 | 09:05–09:10 | verified 1/0 |
| ETH-15m | VERIFIED | 10191 | 6382423 | 6382478 | 09:00–09:15 | verified 1/0 |
| ETH-1h | VERIFIED | 10117 | 6337335 | 6337937 | 09:00–10:00 | verified 1/0 |
| SOL-5m | VERIFIED | 10686 | 6382443 | 6382462 | 09:05–09:10 | verified 1/0 |
| SOL-15m | VERIFIED | 10423 | 6382431 | 6382481 | 09:00–09:15 | verified 1/0 |
| SOL-1h | VERIFIED | 10122 | 6337336 | 6337938 | 09:00–10:00 | verified 1/0 |
| XRP-5m | VERIFIED | 10685 | 6382444 | 6382466 | 09:05–09:10 | verified 1/0 |
| XRP-15m | VERIFIED | 10422 | 6382427 | 6382475 | 09:00–09:15 | verified 1/0 |
| XRP-1h | VERIFIED | 10123 | 6337344 | 6337939 | 09:00–10:00 | verified 1/0 |
| DOGE-5m | VERIFIED | 11325 | 6382442 | 6382464 | 09:05–09:10 | verified 1/0 |
| DOGE-15m | VERIFIED | 11328 | 6382430 | 6382473 | 09:00–09:15 | verified 1/0 |
| DOGE-1h | VERIFIED | 11287 | 6337355 | 6337940 | 09:00–10:00 | verified 1/0 |
| HYPE-5m | VERIFIED | 11327 | 6382439 | 6382460 | 09:05–09:10 | verified 1/0 |
| HYPE-15m | VERIFIED | 11329 | 6382416 | 6382476 | 09:00–09:15 | verified 1/0 |
| HYPE-1h | VERIFIED | 11289 | 6337365 | 6337941 | 09:00–10:00 | verified 1/0 |
| BNB-5m | VERIFIED | 11326 | 6382440 | 6382459 | 09:05–09:10 | verified 1/0 |
| BNB-15m | VERIFIED | 11330 | 6382420 | 6382472 | 09:00–09:15 | verified 1/0 |
| BNB-1h | VERIFIED | 11291 | 6337374 | 6337942 | 09:00–10:00 | verified 1/0 |

The SOL hourly convention is **solana-up-or-down-hourly**, series 10122. Its
former `sol-up-or-down-hourly` candidate was wrong; corrected with a regression.
Candidates still require exact returned series slug/recurrence and market identity.
No first-token, overlapping-instance or epoch-slug guessing is accepted.
Unsupported explicit market protocols fail closed; presence of legacy IDs does
not justify selecting them on a Protocol V2/unknown-version market. Supporting
Position IDs is not added in Phase 9. See [market version selection](https://docs.polymarket.com/market-data/market-details).

## Fee contract — PARTIAL, fee-bearing entries still blocked

Authority: [current official fees](https://docs.polymarket.com/trading/fees), Gamma
per-market schedule and the [official CTF Exchange V2 source pinned at ccc0596](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol).
All 21 captured markets: feesEnabled=true, exponent=1, rate=0.07, takerOnly=true,
rebateRate=0.2. These are captured values, never runtime defaults. Fee metadata
expires after 300s. Missing, stale or unsupported evidence never becomes VALID zero.

Formula: C shares × per-market rate × [p(1-p)]^exponent. **BUY fee is additional
collateral cash; gross shares are not reduced by this CTF Exchange V2 fee. SELL
fee is collateral deducted from proceeds.** `_settleTakerOrder`, the complementary
and BUY paths confirm these units. The [current official Python SDK budget math](https://github.com/Polymarket/py-sdk/blob/d26958d68a054edb01787825ff63c8c0d175d127/src/polymarket/_internal/actions/orders/market.py#L409)
corroborates notional+fee cash cost. Historical share-deduction formulas belong to
other contract generations and are not current-market authority. Generic share-fee
arithmetic remains tested for explicit fake contracts; public CTF evidence records CASH.

Official docs specify five decimal places but not an exact tie rule or whether
rounding is applied per individual maker match before aggregation. The operator
supplies fee amounts to the contract; the contract validates limits, not this
calculation policy. SDK budget estimates do not establish charged-fee rounding.
Counterexample with current rate: 5 shares at 0.01 gives raw 0.003465; DOWN gives
0.00346, HALF_UP gives 0.00347. Two 2.5-share matches with HALF_UP total 0.00346.
Both missing rules affect real accepted quantities, so neither can be guessed.

Fee-enabled evidence stays **UNSUPPORTED**, with exact blocker
CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED. No enabling configuration
bypass and no fallback rate. Official non-boundary examples (100 shares at 0.10,
0.20, 0.30, 0.50, 0.90) are tested; the cash example at 0.50 gives 1.75 fee,
51.75 buy debit for 100 net shares, and 48.25 sell cash. Only explicit
feesEnabled=false can provide VALID zero. Verified settlement pays net shares ×
1/0, independently of book sale fees, with no invented SELL or redemption action.

## Underlying/reference feeds — 7/7 research sources verified

All seven Binance spot pairs returned verified base/quote/TRADING metadata, exact
open candles for all three market windows, and exchange-timestamped recent trades
fresh within 5s when checked. [Binance public endpoint documentation](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
defines the candle-open and trade-time fields. Baseline/current source, pair and
reference type agree. Candle timestamps must equal the verified market start.

| Asset | Research provider/pair | Open/live availability | Hourly market authority from rules | Status |
| --- | --- | --- | --- | --- |
| BTC | BTCUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |
| ETH | ETHUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |
| SOL | SOLUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |
| XRP | XRPUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |
| DOGE | DOGEUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |
| HYPE | HYPEUSDT spot research | 3/3 open/current contexts; fresh | Binance USD-M futures candle | VERIFIED |
| BNB | BNBUSDT spot research | 3/3 open/current contexts; fresh | Binance spot candle | VERIFIED |

Every 5m/15m market specifies a Chainlink USD TWAP-60s stream. Hourly rules name
Binance USDT candles; **HYPE hourly specifically uses the Binance futures URL**,
while the other six use spot URLs. HYPE is labelled BINANCE_USDM_1H_CANDLE as the
settlement reference. All supplied prices, including HYPE, remain explicitly
SPOT_USDT_RESEARCH/is_research_proxy=true. Neither spot data nor a candle-open
proxy is presented as a Chainlink TWAP or authoritative HYPE futures observation.
Official Gamma resolution determines the actual winner. No exact oracle stream
or authenticated PolyBolt integration is added.

Runtime retries per-instance independently at one-second worker intervals; symbol
locks and a two-second fetch cache avoid multiplying trade requests by timeframe.
Exchange timestamps are preserved through cache/retries. HTTP timeout is 10s;
stale/missing/unmatched references cannot satisfy a movement rule. Offline tests
prove transient retry and that a blocked asset cannot starve healthy instances.
No sustained rate-limit/soak result is claimed from this bounded verification.

## CLOB 403 investigation

Original path: `https://clob.polymarket.com/markets/{condition_id}`. The exact old
condition 0x5b24c35e819c2541a231a3365b5adcfb396dd21f71fda1f03d607b44e221e56d
still returned 403 with native urllib, original default User-Agent/TLS and explicit
no-proxy configuration. Default HTTPX requests for the current market's `/markets`,
`/book`, `/fee-rate` and `/clob-markets` returned 200 JSON with Cloudflare headers.
The samples differ in client and market path; prior response headers are missing.
The precise cause (client signature, URL-specific policy or transient edge state)
is **undetermined**. There is no evidence to assert a geography or rate-limit cause.
No further client/identity change was attempted to evade the 403.

Current SDK [public fee metadata method](https://github.com/Polymarket/py-sdk/blob/d26958d68a054edb01787825ff63c8c0d175d127/src/polymarket/_internal/actions/orders/market_data.py#L92)
reads `/clob-markets/{condition}`. Its fd.r=0.07/e=1/to=true matched Gamma. Legacy
`/fee-rate` returned base_fee=1000; this is **not** substituted for the current
curve rate. The DEMO runtime constructs Gamma/Binance HTTP clients and native
Market WS only; it does not require any CLOB HTTP path. A guarded runtime test
forbids constructing a CLOB HTTP client. The public E2E also ran without that HTTP
service. Thus this 403 is not a critical DEMO dependency or an auth requirement.

## Public E2E and local API RC

Public Discovery -> native WS subscription -> fresh MarketObservation -> FIFO ->
Rule evaluation -> temporary DEMO persistence **PASS**. A test-named rule with
trigger 0.5 persisted decision/rearm state. Fee evidence remained UNSUPPORTED,
so **zero positions** was expected: no public trade is claimed. Explicit reconnect
advanced the durable generation, new observations were used, unsubscribe was sent,
and the data component moved NOT_READY -> READY -> NOT_READY -> READY. Stale
captured observations are rejected offline; rollover with old held tokens is
replayed against all 21 captured current/next identities. There was no long wait
for an actual market rollover and no missed-event backfill.

A real Uvicorn listener on 127.0.0.1 with an ephemeral port, disposable DEMO DB,
generated Argon2 test credentials and no external provider calls passed login,
current/logout, rule CRUD/version/actions, fake-fixture trades/detail, aligned KPI,
markets, health, both confirmed resets and OpenAPI. Response models and Decimal
strings were validated. Archive preserved an open fake position until explicit
reset. No route permits order/cancel/redeem. The listener and all workers stopped;
no installed/running LIVE service or production configuration was touched.

Reproduction helpers are explicit test-only programs, never collected by pytest:
`tests/verify_public_integrations.py --mode matrix|e2e --output /tmp/...` (authorized
public GET/WS only) and `tests/verify_api_rc.py --output /tmp/...` (loopback only).
Use PYTHONDONTWRITEBYTECODE=1 and PYTHONPATH=.:polymarket-collector from repo root.
