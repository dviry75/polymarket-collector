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
## Phases 5/6 — rule and position engine complete

- Phase 4 commit: `6d7dcad`.
- Ordered exact ask equality, side rearm/shared quota, independent concurrent
  rules/positions; Jerusalem schedules/DST and start-relative windows; fresh
  movement filtering and persisted research evidence; immutable entry snapshots.
- Purchased-token observed bid TP/SL, current valid fee evidence, atomic cash
  accounting; official 1/0 idempotent settlement remains an independent path.
- Fresh-generation checks include durable generation, so reset/restart cannot
  replay queued decisions. Entry gates do not stop reliable exits.
- Tests: 111 passed, zero failed/skipped; jump prices, archive/edit preservation,
  stale/mismatched reference, gap/no-backfill, rearm, schedules and all timeframes.
- No new product blockers. Public fee policy restriction remains as Phase 4.
- Local commit: phase commit containing this entry (SHA recorded next phase).
## Phase 7 — health and recovery complete

- Phases 5/6 local commit: `212cbbc`.
- DEMO-owned discovery/fee/research/resolution retry workers, demand-driven public
  WS, independent ordered decision consumer, graceful cancellation and persisted
  incidents. Public transports disabled unless explicitly configured.
- Per-rule dependencies gate entries, actual token freshness is distinct from
  socket connection, and recovery requires two continuous healthy seconds.
  Discovery expires after 30s; fees 300s; observations/reference 5s. Unaffected
  families can work while other families are unavailable. Resolution is separate.
- Tests: 116 passed, zero failed/skipped. Fake end-to-end provider/consumer path,
  failure/retry, connected-without-books, stability, reset/generation and shutdown.
- Public limitations unchanged. Next: wire independent auth and REST application.
- Local commit: phase commit containing this entry (SHA recorded next phase).
## Phase 8 — authenticated backend API complete

- Phase 7 local commit: `150e4ba`.
- Versioned `/demo/api/v1`: independent auth/current/logout; rule CRUD, CAS versions,
  enable/disable/duplicate/archive; trades/detail/pagination; shared-filter KPI;
  market/health views; confirmed trading and factory reset. No UI/streaming needed.
- DEMO-only Argon2id or explicit legacy SHA256 validation; host-only HttpOnly/Secure
  SameSite cookie and CSRF; durable global session revocation and credential
  rotation; bounded rate limit. Missing config fails closed. No plaintext hash mode.
- Tests: 124 passed, zero failed/skipped, including real ASGI requests with fake
  credentials/temp DB, reset with open positions, schema and decimal-safe filters.
- Public smoke: actual BTC current+next discovery, two native WS book snapshots,
  Gamma fee schedule and fresh Binance BTC research reference succeeded. Fee
  schedule intentionally remains UNSUPPORTED because debit/tie policy unverified.
  Latest closed resolution was still pending; older final sample check in progress.
- Local commit: phase commit containing this entry (SHA in final validation entry).
