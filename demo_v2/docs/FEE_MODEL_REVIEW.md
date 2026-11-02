# DEMO simulation fee model

## Current product decision — empirical flat simulation fees (2026-11-03)

DEMO V2 intentionally does not replicate exact Polymarket fee rounding.
Simulation uses a fixed empirical fee of $0.068 per BUY/SELL execution,
derived from $111.81 across 1,644 historical operations. This is a deliberate
research model assumption, **not an authoritative Polymarket fee formula**.
The operator supplied 832 entries/$70.83 and 812 exits/$40.98;
111.81 / 1644 = 0.0680109489… rounded by the product decision to 0.068.
This change does not independently re-audit those historical rows.

`DEMO_FLAT_FEE_PER_ACTION` is optional and defaults to `Decimal("0.068")`.
Invalid, nonfinite, negative or binary-float configuration fails before startup;
zero and nonnegative Decimal overrides are allowed. No environment override is
needed to use the default. Configuration belongs to DEMO only.

- BUY: shares × observed ask + fee is debited as cash. Shares are unchanged.
- SELL (TP/SL): shares × observed bid − fee is the signed cash credit. Even at
  bid=0, the fee is charged; it is not capped at gross proceeds.
- Official resolution: shares × 1/0, with zero additional fee. The entry fee
  remains part of net P&L. No synthetic sale is generated.
- Default buy/sell round trip: $0.136 total fees; resolution: $0.068 total fees.
- Example: 5 × 0.74 entry debits 3.768; 5 × 0.96 sale credits 4.732;
  net P&L is 0.964. All accounting uses Decimal and API decimal strings.

Policy provenance is `EMPIRICAL_FLAT_AVERAGE`, with configured `per_action`,
policy version, historical sample size and observed total. Entry fee evidence,
entry/exit provenance and ledger retain the assumptions and charges at execution
time; existing positions/history are never rewritten when configuration changes.
Trade `buy_fee`, `sell_fee`, `total_fees`, `net_pnl` still report persisted charges.
The market `fee` field and health `fee_model` expose the current simulation policy;
they do not reclassify research evidence as verified exchange fees.

The runtime no longer constructs, polls, stores or gates entries on an exact fee
provider. `CURRENT_FEE_ROUNDING_UNVERIFIED`, unavailable metadata and failed fee
APIs are **not execution/readiness blockers**. Discovery, ordered fresh observations,
both books, exact triggers, schedules, required underlying context, DB/worker
health, critical components and stable recovery remain enforced. There is no
maker fee exemption in this model. Observed-bid TP is still an immediate virtual
sale; no maker execution or emergency-exit feature is introduced.

The exact-fee research provider, captured public fixtures and tests are retained
for research only. BUY/SELL cash semantics were established in that work; exact
operator rounding remains unknown. Earlier release-blocker conclusions below
are historical and superseded by this explicit product decision.

---

## Preserved earlier research (superseded for DEMO execution)

# Fee evidence reconciliation — 2026-11-02

**Verdict: PARTIAL. Fee-enabled DEMO entries: NO.** The remaining blocker is
`CURRENT_FEE_ROUNDING_UNVERIFIED`. This is a source/test/documentation change;
no deployment, account action, production DB access or historical repair.

## Supporting production evidence, not authoritative fee contract

The operator supplied the following aggregate observations from isolated fills
and adjacent account-balance samples. This task did not independently rerun the
production analysis or receive raw charged-fee/tie-boundary fill records.

| Observation | Supporting evidence | Limit |
| --- | --- | --- |
| Taker BUY | 32/32 debits equal notional plus fee; shares not reduced, including reported pre-3.9 trades | Supports cash units, not exact rounding |
| Maker SELL | 189/190 credits equal notional with no fee | Supports zero for observed configuration, not every possible maker contract |
| Taker SELL | 418/559 credits exactly match the existing formula | Other windows reportedly contain balance noise; they neither establish a new formula nor prove tie rules |

Historical averages and the revised historical total are analysis results only.
None is a fee constant or a test oracle for an individual virtual trade. Rates
are obtained from fresh, identity-matched per-market metadata, never these averages.

## Authoritative fee contract and pinned sources

- [Official fee policy](https://docs.polymarket.com/trading/fees):
  shares × rate × price × (1 − price), cash fees for takers, five decimal places,
  minimum charged amount 0.00001 USDC and amounts below that minimum rounded to zero.
  This does not identify the exact tie-breaking convention.
- [Official fee configuration](https://docs.polymarket.com/market-data/market-details#trading-fees):
  `feeSchedule.takerOnly=true` means makers pay no fee. The policy is represented
  explicitly; false, missing, numeric or string flags do not authorize zero maker
  fees or reuse of the taker formula. No maker execution engine is added.
- [CTF Exchange V2 Trading.sol at ccc0596](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol):
  collateral is charged additionally on BUY and deducted from SELL proceeds.
  `matchOrders` receives operator-supplied taker and maker fee amounts; the
  contract can transfer nonzero maker fees, so the per-market policy matters.
  [`Fees.sol`](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Fees.sol)
  validates a maximum, not the operator's fee-curve rounding. Integer division
  in taking-amount/max-fee helpers is not evidence that charged fees round DOWN.
  This commit was still the official repository's current main on this check.
- [Python SDK at aa94ec4](https://github.com/Polymarket/py-sdk/blob/aa94ec431e3d4946576fb7c0d079fe1c14fcf676/src/polymarket/_internal/actions/orders/market.py#L409):
  `adjust_buy_amount_for_fees` estimates budget as notional plus platform fee;
  it does not quantize the charged fee. Its separate order-amount rounding helpers
  (including HALF_EVEN) do not define exchange fee rounding. Missing public `fd`
  defaults in the SDK are not adopted as authoritative zero in DEMO.

Fresh public GET-only discovery confirmed all 21 current families at the timestamp
in `../tests/fixtures/fee_contract_20261102.json`: protocol v1 CTF identities,
feesEnabled=true, rate=0.07, exponent=1, takerOnly=true, rebateRate=0.2. Exact market,
condition and token identities are recorded. These captured parameters are replay
fixtures, not runtime defaults or permanent guarantees. Runtime evidence expires
under the existing 300-second freshness bound. No SDK was imported or executed;
no authenticated Polymarket endpoint or trading call was used.

## Execution and accounting audit

`engine.py` executes the configured quantity once at observed best ask (entry)
or best bid (TP/SL). It does not place resting orders or consume book depth.
Therefore **entry, TP and SL are all TAKER simulations**. A real LIVE maker TP
must not cause a free simulated TP: the execution mechanisms differ.

The explicit model is `OBSERVED_TOP_OF_BOOK_SINGLE_EXECUTION`; fee application is
`ONCE_PER_VIRTUAL_EXECUTION`. These names and the TAKER role are persisted with
entry/exit and ledger provenance; `FeeEvidence` records the aggregation and
market's `taker_only` policy. Existing JSON columns accommodate the additional
evidence, so no schema migration or old-row rewrite is required.

- Taker BUY: notional = C × p; cash debit = notional + fee; net shares = C.
- Taker SELL: gross proceeds = held shares × observed bid; cash credit = gross − fee.
- Maker: zero only with explicit taker-only policy or verified fees-disabled
  evidence; no maker order is produced by DEMO. Unknown evidence still fails closed.
- Resolution: held shares × official 1/0, zero sell fee, no fabricated book sale.

The existing public provider, engine and repository already reconciled BUY cash
and preserved all CTF shares. This task makes role/policy/provenance explicit,
tests the cash ledger end to end, and distinguishes the documented minimum from
a rounding-up operation. Legacy explicit fake share-fee contracts remain testable;
they are not selected for current public CTF markets.

## Rounding and aggregation decision

No authoritative tie rule was established. For C=5, p=0.01, r=0.07, raw fee is
0.003465. DOWN produces 0.00346; HALF_UP produces 0.00347. HALF_EVEN agrees with
DOWN on this one tie, so a single matching example cannot prove a general rule.
Near-tie and minimum-threshold regressions preserve the distinction. Candidate
conventions in fake tests are not enabled for public markets.

Real maker-match splitting is **not an input to the current DEMO execution
model**. Under that model a future verified fee is applied once to the whole
virtual execution. It is not a claim that real order fills aggregate that way:
two 2.5-share matches under HALF_UP total 0.00346, while one 5-share execution is
0.00347. Splitting, differing execution prices and unmodeled depth can affect
research results; the number of real matches is unknown, so no universal error
bound or real-order replication claim is made.

Consequently, a deep multi-fill engine is not added and the blocker no longer
describes that missing feature. **Entries remain blocked.** This simplification
does not justify guessing the rounding convention or declaring fee parity with
actual fragmented orders. Exact real-fill replication would require a separate
matching/fill model and authoritative aggregation behavior.

To remove the remaining blocker, obtain either an explicit current operator fee
rounding specification, or independently attributable charged-fee evidence at
exact/near ties (including ties with both even and odd retained last digits),
below/at the minimum and across relevant prices/quantities. It must identify the
current contract/configuration, quantity, price, role, fee precision and all
matches associated with the charge. Aggregate balance averages or recomputed
historical fee columns cannot establish this contract.

## Historical source safety

Read-only source audit covered
`polymarket-collector/live/fee_accounting.py`,
`polymarket-collector/scripts/ingest_external_fills.py`, and the LIVE fee helper.
The resolver's opening warning describes reported zero bps on charged taker fills.
The ingest code writes zero for zero bps and otherwise quantizes a formula without
a taker-only role guard. The local LIVE helper's DOWN convention is implementation
behavior, not authority for the operator's charge. None was changed or imported
into DEMO fee execution.

Operator-reported pre-3.9 rows with fee_rate_bps=0, fee=0 and VERIFIED cannot prove
zero fees. Historical external_fills maker fee values cannot prove maker charges
or current rounding; historical values may come from earlier code versions.
DEMO has no importer that consumes either table. Its provider ignores those
legacy keys/status labels and requires explicit identity-matched feesEnabled=false
for public VALID zero. Regressions cover both untrusted historical-zero shapes.
No LIVE DB exists at the documented runtime path on this host; no account history
was read and no historical production record was updated.

## Validation and delivery boundary

All 233 DEMO backend tests passed, including 27 new fee reconciliation cases.
Frontend regression: 14 unit/contract tests and 3 Chrome E2E workflows passed
against temporary fake dependencies. E2E fixtures assert the new precise health
reason. No runtime DB/config/service or LIVE source changed. The pre-existing
uncommitted Rubik frontend work is preserved and excluded from this fee commit.
