# DEMO V2 implementation status

Local main; completed backend work package on 2026-10-29. No UI or deployment.
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
- Missing fee evidence blocks entries; fee-enabled public schedules remain
  UNSUPPORTED until current debit units/tie rounding are verified. No default rate.
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

Actual unauthenticated checks: BTC current+next discovery, two native WS book
snapshots, current Gamma fee schedule, fresh Binance BTC reference and an older
closed/UMA-resolved BTC 1/0 winner. Twenty candidate series returned; SOL hourly
unverified. CLOB HTTP market lookup returned 403. Other current families/reference
pairs and sustained WS operation are not claimed verified. See INTEGRATION_CONTRACTS.

No unresolved product decision blocks the completed offline backend. Public
fee-bearing entries remain deliberately blocked by missing authoritative fee-unit/
rounding evidence. Future production review/provisioning/deployment is outside scope.

## Git delivery

All phase commits are local and restricted to demo_v2/. Exactly one final
`git push origin main` attempt was made. It failed with missing HTTPS authentication:
`could not read Username for 'https://github.com': terminal prompts disabled`.
No credentials were changed and no retry/reset/rebase/clean was performed.
Static import audit, cumulative diff and every local commit passed the demo_v2-only
check. This final documentation-only commit records that failed attempt; it is
also local. The checkout and delivered Git bundle retain all completed work.
