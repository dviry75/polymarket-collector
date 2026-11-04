# DEMO V2 architecture

Option C + narrow Option A. All app/config/state belongs to DEMO. The sole LIVE
imports remain `live.order_book` and `live.strategy.exact_trigger`, through
`providers/book_primitives.py`. No trader/router/adapters/signing/account WS,
LIVE env/DB, reconciliation or real trading IPC is loaded. Public providers use
only allowlisted unauthenticated GET endpoints and the public market WebSocket.

```text
app.py / config.py / auth.py        independent ASGI factory, DEMO settings/session
runtime.py / health.py              discovery, demand, reference caches, supervision
providers/market_pool.py            3 independent connections: 5m / 15m / 1h
providers/market_ws.py              public books, ordered wire transitions, recovery
streams.py                         bounded lossless decision FIFOs; lossy telemetry
 decision_worker.py                one spawned state/financial owner, private pipe
 engine.py / domain/                exact triggers, schedules, exits, Decimal policy
 persistence/                      dedicated WAL SQLite, atomic accounting, history
 api/                              authenticated APIs; heavy reads in thread workers
 resources.py                      cached read-only CPU/RAM/disk/host-NIC telemetry
 frontend/                         authenticated presentation, never decision inputs
 tests/                            temp DBs, fake I/O, crashes, load and browser tests
 deploy/                           reviewed offline release checks/promotion/rollback
```

## Ownership and flow

ASGI owns public connections, verified discovery/current instances, timed research
contexts and bounded ingress queues. Three timeframe shards retain UP/DOWN together,
including old held instances. Each shard has its own generation and contiguous
observation ordinal. A wire frame's intermediate top transitions all reach decisions;
telemetry sampling/coalescing cannot choose the prices the engine sees.

One spawned process owns decisions, parsed rule/open-position/quota caches, rearm
state and all rule/financial mutations. A private inherited pipe allows one request
in flight, with at most 64 ordered observations per request. Each FIFO remains
bounded (default 1024). At half capacity, the receiver waits before reading the
next wire frame; every queued observation retains its existing deadline. A large
atomic frame can still overflow, and a sustained stall still fails the unchanged
queue-age budget. Either failure fences/resynchronizes the connection instead of
dropping selected prices. No executor task is created per
observation. ASGI continues receiving data while the owner waits for SQLite.

Commands (rule version edits, reset, discovery writes, official settlement) serialize
with observations. Context calculated before a rule/reset command is rejected.
Read-only current projections refresh after actions/commands and on the owner
heartbeat; historical trade/KPI/version reads and auth DB/hash work run outside the
ASGI event loop. Process liveness, heartbeat and consumers are supervised. An
uncertain request is never retried: fence ingress, retire the owner, reload durable
state and request fresh generations. This is one ASGI worker plus one decision
process per DEMO DB; do not run multiple independent apps against the same DB.

History reads select/count the shared cohort in SQLite without loading all market
metadata or trade evidence. Trade pagination sorts narrow IDs before fetching JSON
for at most 200 rows. Cohort totals join market identity only when the filters
need it; page display loads identity only for selected rows. KPI and rule totals stream narrow financial columns using
Decimal arithmetic, with no floating-point SQL sums or unbounded ID lists. Related
totals/page rows share a short WAL read snapshot. Movement filters use exact Decimal
comparisons and date filters retain timezone-aware inclusive/exclusive boundaries.
Read endpoints never allocate serials or mutate financial/session state.

Runtime recovery reads only unexpired market instances and expired instances
with open positions. Discovery still owns current/future identity. After demand
updates, expired metadata is evicted only when no current/held/subscribed market
or reference worker needs it; associated volatile research/gate caches follow
that lifecycle. Historical instances/evidence remain in SQLite for API detail
and research. This avoids duplicating the full historical catalog in every
running service or carrying expired metadata across rollovers.

## Financial and recovery boundaries

Every observation is evaluated. A non-action tick changes volatile rearm state,
without opening a SQL connection/transaction. Possible actions recheck identity,
current durable generation, rule version, verified/open instance, eligibility,
shared UP/DOWN quota and exchange/receipt freshness inside a write transaction.
Entry/exit, action-time rule snapshot/provenance, counter, research and signed cash
ledger remain atomic. Success returns only after commit. A crash before commit
rolls back everything; a crash after commit retains idempotency/quota/ledger. No
old observation is replayed to discover which side of that boundary occurred.

Shared local generation/market leases fence in-flight actions even when loss is
noticed while SQLite is locked. Guards run immediately after acquiring the writer
lock, before financial writes. This is the decision linearization point; an already
selected transaction still commits atomically. Local market invalidation advances
its lease without blocking healthy peers; removed lease slots are safely fenced
before reuse. Rearm state is volatile by design: a recovery starts a new generation.
An eligibility epoch records gaps even when no price tick arrived during the gap;
a rule edit alone does not rearm an unchanged price.

Book and rule health are separate from socket transport. Quiet/stale loaded books
block affected new entries; silence does not close a healthy shared connection.
Missing/invalid snapshots get bounded pair-local snapshot retries. Transport loss,
heartbeat failure, subscription-set changes or ordering/queue failures retire only
the affected timeframe shard. Resubscribe clears affected depth/epochs before new
snapshots. No old/new generation mixing or entry/TP/SL backfill is allowed.
Verified empty books are `READY_EMPTY` with null prices; missing books remain
`AWAITING_SNAPSHOT`. Neither invents bids or asks.

Shard connection health is recomputed from its current transport and FIFO state;
a recovered connection clears the old active alert while counters/incidents retain
history. Book/source readiness remains separate and is never forced healthy.

Health is hierarchical: DB/owner/auth integrity failures block all entries;
connection and market failures block their own dependencies; underlying availability
blocks only rules requiring it. DEGRADED allows healthy rules/markets to continue.
`entries_allowed` describes global permission, not a promise that every rule is
eligible. Actual entry requires per-rule/market gates and a continuous recovery
stability interval. `/health` is shallow liveness; `/ready` is 503 for NOT_READY and
200 for READY/DEGRADED. Authenticated health includes local/global reasons, shard
state, queue depth/age/high-water/failures, owner processing/commit/RPC timings,
event-loop lag and separate reference fetch/source age. Old source prices never
become fresh merely because an HTTP fetch succeeded.

Timers have distinct defaults: quote freshness 5s, decision queue/IPC budget 1s,
snapshot retry 10s, PING 10s, transport/heartbeat timeout 30s, stable connection
before resetting reconnect backoff 30s, owner heartbeat 2s/expiry 5s, discovery
expiry 30s and recovery stability 2s. Queue/retry/loop timers use monotonic time;
exchange/source timestamps and UTC accounting provenance remain explicit.

DEMO fees remain empirical cash charges: configurable 0.068 per virtual BUY/SELL,
unchanged shares, zero additional fee for official 1/0 settlement. Exact exchange
fee providers are research only. Rule edits/disable/archive/schedules affect entries;
trustworthy purchased-side bids continue managing existing TP/SL snapshots.

SQLite schema/path/namespace/alias validation, Decimal TEXT accounting and durable
auth revocation remain independent of LIVE. No history is automatically deleted.
Only explicit confirmed DEMO reset clears simulated financial state. Resources are
telemetry only; API+decision process RSS sums include shared pages, and NIC counters
cover the host rather than measuring the household link or DEMO-only traffic.

In-flight fences have one parent writer and one child reader. `fences.py` uses
shared storage with `libatomic.so.1` uint64 load/store operations and sequential
consistency; plain RawArray reads/writes are never treated as synchronization.
Startup verifies aligned operations are lock-free, so a killed child cannot leave
the event loop waiting on a semaphore. Missing library/unsupported atomic storage
fails startup. The disposable release check exercises child startup/shutdown before
promotion; installation still requires actual unit-sandbox and authenticated-health
verification. System dependency: Linux `libatomic1` (already present on this host).

Atomic API reference: [GCC atomic builtins](https://gcc.gnu.org/onlinedocs/gcc/_005f_005fatomic-Builtins.html).
Python explicitly warns that [ordinary RawArray accesses can be non-atomic](https://docs.python.org/3/library/multiprocessing.html#module-multiprocessing.sharedctypes).
