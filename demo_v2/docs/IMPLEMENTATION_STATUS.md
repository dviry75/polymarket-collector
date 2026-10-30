# DEMO V2 implementation status

Local main; backend Phases 2–8 completed 2026-10-29; Phase 9 verified 2026-10-30. No UI or deployment.
Audit `239d6bd`, Phase 1 `ce76b60`; remote base `d26dda4`.

| Phase | Completed | Local commit | Tests at phase boundary |
| --- | --- | --- | --- |
| 2 | Domain/catalog, checksummed WAL/FK schema, immutable versions/ledger, atomic positions/counters/reset | 7b5405e | 61 passed |
| 3 | Public discovery, metadata rollover/demand, native WS, ordered decision pipeline | f843715 | 73 passed |
| 4 | Typed fee evidence/Decimal arithmetic; explicit research source mapping/movement filters | 6d7dcad | 88 passed |
| 5/6 | Exact entry/rearm/shared quota/concurrency, schedules/windows, observed-bid TP/SL, official settlement/accounting | 212cbbc | 111 passed |
| 7 | Granular health, freshness, stable recovery/retry, provider orchestration and shutdown | 150e4ba | 116 passed |
| 8 | Independent auth/sessions/CSRF, versioned REST/OpenAPI, cohorts/KPI and confirmed resets | 62fa3a4 | 124 passed |
| Final validation | Cross-layer regressions, independent research workers, full fake-WS authenticated ASGI path and current docs | 1729146 | 141 passed, zero failed/skipped |

## Important decisions

- Only approved LIVE book/exact-trigger helpers are imported. Everything else is
  owned by demo_v2; no trading SDK, key, real account action or production env/DB.
- Raw actionable observations are FIFO, never telemetry input. Every generation
  discards old decisions; freshness is rechecked after DB lock acquisition.
- Immutable entry snapshots, shared quota, independent positions and ledger commit
  together. Share fees are not debited twice; settlement has no sell fee.
- Missing fee evidence blocks entries. Phase 9 verified CASH units for current CTF
  markets; fee-enabled schedules remain UNSUPPORTED until operator tie/per-match
  aggregation rounding is verified. No default rate.
- Binance USDT is a labelled research proxy, distinct from Chainlink TWAP oracle.
  Per-instance workers keep blocked feeds from starving healthy ones.
- Missing auth fails closed. Separate DEMO credentials/secret/cookie/revocation;
  logout revokes all DEMO sessions. No credentials were copied. Manual provisioning
  of LIVE-equivalent values is future operator work.
- Trading reset deletes simulation positions including open ones, runtime, ledger
  and research; retains rules/versions. Factory adds rules/versions deletion.
  Neither reset fabricates exits. Both invalidate decision generations.
- REST prefix /demo/api/v1; decimal strings; polling; shared trade/KPI cohort.
  The README and generated OpenAPI are the concrete frontend integration contract.

## Public integration evidence and remaining limits

Phase 9 supersedes the earlier 20-family/BTC-only check. 21/21 families have exact
identity, contiguous current/next and a finalized historical 1/0 resolution.
SOL hourly uses verified series 10122 (`solana-up-or-down-hourly`). All 7 research
pairs and all 21 open/current contexts were verified. HYPE hourly authority is a
Binance futures candle; supplied spot data remains a research proxy.

Public native WS/FIFO/rule-state persistence, reconnect/new generation,
unsubscribe and data-health transitions passed without CLOB HTTP. The original
urllib URL still returns 403; current HTTPX public CLOB reads return 200. Cause
cannot be uniquely attributed, but the runtime has no CLOB HTTP dependency.

Fee formula, current parameters, BUY/SELL CASH units and five-decimal precision
are verified. Exact tie and per-match aggregation rounding remain the only critical
public-entry blocker. Counterexample: 5 × 0.07 × 0.01 × 0.99 = 0.003465; legitimate
rounding conventions give different cash results. All captured markets charge fees,
so public entries remain fail-closed; no fallback or default-zero trade was created.

Local loopback API smoke passed using temporary resources/test credentials; it
stopped cleanly. Evidence and exact matrix are in INTEGRATION_CONTRACTS.md and
`demo_v2/tests/fixtures/phase9_public_evidence.json`.

## Release Candidate Readiness

**Verdict: PARTIAL.** Frontend/API connection is ready for review; activating public
fee-bearing simulation entries and deployment readiness require the missing fee
rounding contract. No new product feature, UI or deployment was added in Phase 9.

| Area | Status | Blocker |
|---|---|---|
| 21 market families | READY | 21/21 verified snapshot; none unavailable in this check |
| Market WS | READY | Native public snapshots, reconnect/generation/unsubscribe verified |
| Fees | BLOCKED | Exact operator tie and per-match aggregation rounding unverified; UNSUPPORTED retained |
| Underlying feeds | READY | 7/7 spot research providers verified; all 21 contexts; proxy/oracle distinction explicit |
| Resolution | READY | 21/21 historical final winners verified; no fabricated sell fee |
| Rule Engine | READY | Exact triggers, rearm/quota and full validation pass; public entries intentionally fee-gated |
| Accounting | READY | Ledger/Decimal reconciliation and official non-boundary fee examples pass |
| Health/Recovery | READY | Public data transitions and offline stability/stale/retry/rollover regressions pass |
| Auth | READY | Independent DEMO-only credentials/sessions/CSRF; local test login/logout verified |
| API/OpenAPI | READY | Loopback CRUD/trades/KPI/markets/health/resets and response schemas pass |
| Isolation from LIVE | READY | Approved imports only; temporary resources; no real trading or LIVE mutation |

Tests: **199 passed, 0 failed, 0 skipped** (141 prior + 58 Phase 9 regressions).
Command: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -c demo_v2/pytest.ini demo_v2/tests -q`.
Phase 9 commit: `test: verify DEMO V2 public integrations and release readiness`
(the commit containing this section; final SHA/push outcome in the delivery reply).

## Earlier package Git delivery (2026-10-29)

All phase commits are local and restricted to demo_v2/. Exactly one final
`git push origin main` attempt was made. It failed with missing HTTPS authentication:
`could not read Username for 'https://github.com': terminal prompts disabled`.
No credentials were changed and no retry/reset/rebase/clean was performed.
Static import audit, cumulative diff and every local commit passed the demo_v2-only
check. Earlier documentation commit `a182d65` records that failed attempt.
The earlier delivered Git bundle retains Phases 2–8; Phase 9 is in this checkout.

Phase 9 started from clean `a182d65`, with all ten earlier local commits present.
The completed verification is committed before exactly one final push attempt;
no credential change/reset/rebase/clean or deployment is authorized. The final
Phase 9 reply records the actual push result.
