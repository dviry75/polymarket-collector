# DEMO V2 implementation status

## Resource health dashboard — 2026-11-03

Authenticated health now includes read-only backend CPU/RAM, host CPU/RAM, disk
and host network telemetry. Four responsive cards, background sampling, cached API
reads, explicit scope/freshness and selected local Rubik typography are verified.
Clean candidate: **286 backend tests, 18 frontend tests and 4 Chrome E2E workflows
passed**. The approved fee change is included. Offline update/backup/rollback
artifacts are prepared; installation requires local Ubuntu administrator
authentication. Runtime outcome is recorded in the task delivery, not inferred
from source/test readiness. See `RESOURCE_HEALTH.md`.

## Current fee release — 2026-11-03

**FEE BLOCKER: NONE. SIMULATED ENTRIES: ENABLED when other guards pass.**
The source uses `EMPIRICAL_FLAT_AVERAGE`, default $0.068 per BUY/SELL,
no fee on resolution. Decimal configuration, immutable action provenance,
ledger/API fee reporting and sub-cent frontend fee display are implemented.
The exact fee provider is retained for research and is not run by the app.
Other freshness, WS/discovery, schedule, trigger, stability, underlying and
critical dependency guards remain. See `FEE_MODEL_REVIEW.md`.

Validation of the clean staged candidate (unrelated Rubik/forensic work excluded):
**277 backend tests passed, 0 failed/skipped**, including 45 flat-fee cases and the
native fake-WS authenticated rule → ordered observations → exact entry → TP sale
pipeline with an injected exact-fee provider that raises if called (zero calls).
**15 frontend unit/contract tests passed**; build, TypeScript and JS syntax passed.
**4 real Chrome E2E workflows passed** with loopback fake providers/temp DB. The
new workflow creates a rule through the UI on a fee-enabled fixture market,
proves a fresh non-trigger creates no position, then an exact trigger creates
one position with 5 shares, cash debit 3.768 and buy fee 0.068. No exact-fee
provider is configured. Browser table and methodology show 0.068 accurately.
Screenshots were visually checked. Other workflows cover auth, cohorts,
maintenance, outage/mobile behavior and conflicting rule edits.

The earlier exact-fee research fixtures/tests remain; no claim of exact fee
rounding verification is made. Pre-existing uncommitted fonts and rounding
forensics are preserved and excluded from this release.
This task changes source only: **no deployment, restart, installed DB/env/config
change or LIVE change**. The previously installed release does not acquire these
changes until a separately authorized deployment. Earlier checks below are
point-in-time historical records, not current host/readiness claims.

## Files in the empirical fee release

All 29 changed files are under `demo_v2/`; no LIVE file, migration, dependency
lockfile, installed DB/env/config, service or deployment artifact is changed.
Paths below are relative to `demo_v2/`:

- `README.md`
- `api/router.py`
- `api/schemas.py`
- `config.py`
- `docs/ARCHITECTURE.md`
- `docs/DECISIONS.md`
- `docs/FEE_MODEL_REVIEW.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/INTEGRATION_CONTRACTS.md`
- `docs/SOURCE_OF_TRUTH.md`
- `docs/VALIDATION_MATRIX.md`
- `engine.py`
- `frontend/src/app.js`
- `frontend/src/model.js`
- `frontend/tests/e2e/backend_fixture.py`
- `frontend/tests/e2e/integration.spec.js`
- `frontend/tests/model.test.mjs`
- `persistence/repository.py`
- `providers/contracts.py`
- `providers/fees.py`
- `runtime.py`
- `tests/test_backend_e2e.py`
- `tests/test_deployment.py`
- `tests/test_engine.py`
- `tests/test_fee_evidence_reconciliation.py`
- `tests/test_runtime.py`
- `tests/test_validation_edges.py`
- `domain/fees.py`
- `tests/test_flat_fees.py`

## Historical implementation and verification records


Local main; backend Phases 2–8 completed 2026-10-29; Phase 9 verified 2026-10-30. Frontend integrated subsequently under `frontend/`; no deployment.
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
  markets; fee-enabled schedules remain UNSUPPORTED until charged-fee rounding is
  verified. The 2026-11-02 review explicitly models one virtual taker execution,
  not real maker matches or a resting maker TP. No default rate.
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

Fee formula, current parameters, BUY/SELL CASH units, taker-only maker exemption,
five-decimal precision and the documented minimum are verified. Exact charged-fee
rounding remains the critical public-entry blocker: CURRENT_FEE_ROUNDING_UNVERIFIED.
Counterexample: 5 × 0.07 × 0.01 × 0.99 = 0.003465; legitimate rounding conventions
give different cash results. Real multi-match parity remains a documented limit of
the single-execution product model, not a feature claimed by DEMO. All captured
markets charge fees, so public entries remain fail-closed; no fallback or default-zero
trade was created. Supporting balance observations do not resolve exact ties.

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
| Fees | BLOCKED | CURRENT_FEE_ROUNDING_UNVERIFIED; UNSUPPORTED retained; real multi-match replication not modeled |
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

## Fee evidence reconciliation — 2026-11-02

**PARTIAL; fee-enabled entries remain disabled.** See `FEE_MODEL_REVIEW.md` for
the authoritative sources, operator-supplied supporting production evidence,
historical-data exclusions, virtual-execution boundary and remaining tie proof.
The current official exchange source and Python SDK were reviewed read-only; fresh
public fee configuration was verified for 21/21 families without account actions.

Explicit TAKER entry/TP/SL provenance and `taker_only` policy are persisted through
existing JSON fields; no schema migration. The documented minimum is applied before
rounding. Public evidence still has unknown rounding and UNSUPPORTED state. The
health component now exposes its specific common blocker, retaining the generic
unavailable reason for absent or mixed evidence. No guard is weakened.

Validation: **233 backend tests passed**, including 27 new fee regressions;
**14 frontend unit/contract tests and 3 Chrome E2E tests passed** using temporary
fake dependencies. No deployment, service restart, LIVE source/DB/config mutation
or historical-row repair. Pre-existing Rubik frontend work remains outside this
fee commit. Commit and push results are reported in the task delivery.

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

## Frontend integration — 2026-10-30

Supplied vanilla ES modules connected to existing auth, rules, trades/KPI, markets,
health and confirmed resets. Original CSS retained. Only backend runtime change:
additive RuleView `net_pnl` read projection (all closed positions, Decimal string).
Frontend README describes build/local verification; no LIVE or deployment changes.

## Deployment Review & Preparation — 2026-10-30

See `../deploy/REVIEW.md` and `../deploy/DEPLOYMENT.md`. Dedicated service,
production env/venv, nginx profiles, private provisioning, WAL-aware backup and
rollback were prepared and tested locally. No actual deployment, proxy/tunnel,
DNS/TLS issuance, firewall or LIVE change. Infrastructure/secrets remain operator
preconditions; fee-enabled entries intentionally remain UNSUPPORTED.
