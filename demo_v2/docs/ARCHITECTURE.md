# DEMO V2 architecture

Option C + narrow Option A: own configuration, DB, domain, providers, runtime,
sessions and REST API. No LIVE files or runtime resources are changed.

```text
demo_v2/
  app.py, config.py, auth.py     independent factory and DEMO-only settings/sessions
  domain/                      catalog/identity, immutable rules, entry eligibility
  persistence/                 dedicated SQLite, checksummed migrations, atomic repository
  providers/                   public GET/discovery/WS/resolution/fee/research contracts
  engine.py                    ordered exact entry, rearm/quota and purchased-token exits
  streams.py                   decision FIFO and independent lossy telemetry buffer
  runtime.py, health.py         public workers, demand, freshness, recovery and lifecycle
  api/                         versioned auth/rules/trades/KPI/markets/health/reset
  tests/                       temporary DB, fake providers, isolation and ASGI verification
  deploy/                      inert examples only
```

Only `providers/book_primitives.py` imports `live.order_book` and
`live.strategy.exact_trigger`. The launch environment supplies their source path;
application code never changes sys.path. LIVE configuration, repositories, router,
entrypoints, adapter, account/User WS, reconciliation, order submission, keys and
IPC are forbidden. PublicHTTP has only allowlisted unauthenticated GET endpoints;
PublicMarketWS uses only the public market endpoint. No trading SDK is installed.

```text
DEMO config -> app lifespan -> dedicated migrations/repository + independent auth
public Gamma -> verified families/instances -> entry/held-position demand -> public WS
WS snapshots + ordered delta transitions -> DecisionStream -> DemoEngine -> atomic DB
                                       -> TelemetryBuffer -> optional diagnostics only
public fee/research workers -> timestamped evidence cache -> entry/exit provenance
public official resolution -> independent atomic 1/0 settlement + immutable cash ledger
REST -> same repositories/read-model cohorts; reset -> invalidate generation + atomic delete
```

The engine never awaits network I/O. Run one ASGI worker/decision owner per DEMO
database; separate workers must not share this DB/runtime. A single event-loop
consumer handles contiguous ordinals after expanding wire transitions. Connection generation,
identity and exchange/receipt freshness are checked at ingress, delivery and
transaction time (including time spent waiting for SQLite). Loss, stale/reordered
observations or overflow invalidates queued actions and requires new snapshots.
First fresh data in a new generation may act if dependencies are eligible/stable;
there is no missed-event replay. Telemetry coalescing cannot select decision prices.

Entry checks the current immutable rule version, schedule/window, fresh applicable
fee/research evidence and per-family data health. Shared rule+instance counters,
side rearm, independent position, snapshot/research and ledger commit together.
Current-rule edits never change existing TP/SL. Disable/archive/schedules stop
entries; purchased-token fresh bids still manage exits. Official settlement uses
net shares at 1/0 and no invented sell fee/bid. Unavailable exit fee evidence blocks
a simulated book sale; verified official resolution remains independent.

SQLite ownership is enforced by exact path, namespace, alias checks, WAL/FK and
checksummed schema. Money is Decimal TEXT; no SQL REAL sums. Global/per-rule/cohort
wallets are signed immutable ledger sums without a balance floor. No automatic
research/history pruning occurs. Explicit reset semantics are in README.

Runtime retries providers independently; per-instance research workers prevent a
blocked asset from starving other fresh research. Demand retains old held tokens
through rollover and can unsubscribe exhausted/ineligible rules without positions.
Actual book freshness is separate from socket connectivity. Stability defaults to
2 seconds; discovery expires at 30s, fee evidence at 300s, prices at 5s. Affected
rules fail closed; other healthy families can continue. Global readiness is a
conservative all-components summary: DEGRADED is not a promise that every rule is
blocked; individual rule/market dependencies are authoritative. `/health` is shallow
public liveness, `/ready` is the bounded global summary; authenticated health gives
the component detail. Disabled/unverified public integrations remain explicit.

Auth reads only DEMO credentials with an independent signing secret, host-only
cookie and durable session version/fingerprint. Logout revokes all DEMO sessions.
No LIVE credential lookup/copy occurs. All writes require CSRF after login.
REST financial values are strings, polling is supported, and generated OpenAPI
plus README define the frontend contract. Public fee-enabled entries remain
UNSUPPORTED until the missing current fee-unit/rounding policy is verified.
