# DEMO V2 implementation status

Working base: local `main`, Phase 1 `ce76b60a76a8adba76578cd51308aeeffcb3eae5`.
The approved audit is `239d6bd81221f9145cde03fc879bd92cdccf2645`.
GitHub authentication is not an implementation blocker. Push is reserved for one
attempt at the end of this work package; no credentials will be changed.

## Phase 2 — domain and persistence (completed)

Implemented validated immutable rule specs, generic catalog/verified instance
identity, schema/version checks, transactional DEMO repositories, versioned rules,
independent positions, shared entry counters/side state, immutable cash ledger,
atomic settlement/reset and durable generations. **61 tests passed**, including
21 persistence/domain cases and all Phase 1 isolation tests. Entry and settlement
failure injection verifies complete rollback. No LIVE imports beyond the approved
book/trigger helper surface; no production resources accessed.
Local commit: the phase commit containing this entry (`git log`, recorded by SHA
in the next phase update to avoid a self-referential commit hash).

Phase 2 commit: `7b5405e`.

## Phase 3 — public market pipeline (completed offline; public verification partial)

Implemented read-only GET allowlist; series-verified 21-family discovery with
strict explicit labels, time/condition matching and ambiguous/unavailable status;
metadata-driven rollover/demand including held older tokens; DEMO public WS with
bounded ingress, reconnect/backoff, heartbeat, dynamic subscriptions, fresh
generations and ordered intermediate book transitions independent of telemetry.
Official Gamma resolution verifies exact identities and finalized 1/0 outcomes.
**73 tests passed**, including fake socket lifecycle, all 21 fake families,
ambiguous/missing family fail-closed and exact-edge preservation.

Public checks: Gamma series/market metadata succeeded; only 20 candidate series
were returned (`SOL-1h` unavailable). CLOB market GET returned 403; no bypass.
Public WS smoke remains pending. See INTEGRATION_CONTRACTS.md for source evidence.
Local commit: phase commit containing this entry; next update records its SHA.

## Remaining phases

4: verified fees and reference prices;
5/6: rule/position engine; 7: health/recovery; 8: authenticated REST API.
Public provider availability/fee/reference contracts will be verified explicitly.
## Phase 4 — complete offline, external fee policy fail-closed

- Phase 3 local commit: `f843715`.
- Completed: typed fee evidence/freshness, exact gross/net share and cash arithmetic;
  explicit Binance research mapping, verified symbol/open candle and timestamped
  current reference; movement percent/absolute/sign/between predicates.
- Tests: 88 passed, zero failed/skipped (all offline temporary/fake dependencies).
- Important decision: current fee-enabled public markets remain UNSUPPORTED until
  debit units and tie rounding are verified. No remembered/default rate. Research
  proxy is explicitly distinct from settlement authority.
- Remaining external limitations: SOL hourly family unverified, CLOB HTTP 403;
  public fee debit/rounding contract incomplete. Independent work continues.
- Local commit: `feat: add DEMO V2 fee and underlying providers` (SHA in next phase).
