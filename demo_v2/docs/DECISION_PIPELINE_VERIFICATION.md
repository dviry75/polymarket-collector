# Decision pipeline verification — 2026-11-03

## Scope and preserved baseline

Implement the approved separation of public-data ingestion, ordered decisions,
financial persistence and telemetry. No LIVE file/import boundary, database,
private config, credential value, unit, DNS, proxy or firewall is changed by the
source patch. All tracked changes remain under `demo_v2/`.

Parallel work installed release `72d79ae601b241ea71c152f4645b5f2da9b44eda`
(notifications), followed by `f2428964e56471ad82c00331194fab006ab49299`
(trade serials and per-recipient preferences). Preserve those immutable DEMO-only
source snapshots in separate baseline commits before applying the pipeline fix;
do not overwrite the other worktree's uncommitted changes. Credentials/env/state
were not copied. The final candidate includes both already-installed features.

## Evidence

- Before: 1,000 non-action observations took 1.878s (~532/s), with 1,000 write
  transactions on the ASGI loop. Same local pure-engine workload with the cache:
  0.021s (~48,263/s), no write transactions. This is a microbenchmark, not an
  end-to-end production throughput guarantee.
- Full parser/runtime/process test: 150,000 synthetic book frames / 30s, across
  all 21 definitions and three independently fenced queues. Telemetry retained
  one last value while decision queues retained every ordered observation. No
  sampling, sequence loss or queue invalidation; verified financial reconciliation.
- Extended owner soak: 3,601.04s, 18,005,056 observations (~5,000/s), 119 real-time
  market rollovers including seven hourly rollovers, 280 positions and 560 ledger
  rows. Integrity, quota totals, unique idempotency and Decimal cash sums matched.
  Max RPC 135.46ms; max parent timer lag 16.66ms. All state was disposable/fake;
  no public connection, SMTP delivery or production trading action was involved.
- The extended run covers the decision/fencing/accounting core. The later merge
  of the already-installed serial metadata and recipient preferences is validated
  separately by the full regression/browser suites and accelerated rollover/load
  checks. This distinction avoids claiming the earlier soak ran later source.
- An earlier concurrent Chrome/load run experienced host memory/swap pressure and
  correctly fenced an aged observation. Verification was rerun sequentially;
  freshness budgets were not relaxed to hide the failure.
- Browser verification found and fixed a retired-queue race: an old waiter/RPC
  cannot invalidate the next generation's fresh queue. Regression now exercises
  the actual runtime, not only standalone queue behavior.
- Crash tests kill a child before/after financial commit. SQLite preserves atomic
  position/quota/ledger state. A stalled DB keeps the parent loop responsive;
  timeout retires the owner and never replays uncertain inputs. Restart reloads
  durable state and demands fresh generations.
- Local quiet HYPE vs fresh ETH, verified-empty vs missing books, pair-local
  snapshot retries, generation/market fences after DB lock, queue/IPC deadlines,
  serialized rule edits and unobserved eligibility gaps are covered.
- Native `libatomic.so.1` checks provide lock-free sequentially consistent shared
  fence slots. No child-held process mutex can strand the ASGI loop after a kill.
- Heavy history/auth/notification DB and password-hash work stays off the event
  loop. Password hashes are serialized, including canceled HTTP requests, to keep
  parallel Argon2 verification from causing a memory spike.

## Deployment contract

The offline bundle updater checks expected installed release and exact checksums,
uses unchanged migration/schema files, validates disposable startup with a decision
child, creates online and stopped WAL-aware backups, and promotes only the DEMO
code link. Existing rule/position/ledger IDs, session state, notification preferences
and serial mappings are verified in memory without logging private values. Serial
high-water may advance for legitimate new trades. Failure restores the prior code
link; no automatic DB-history reset/restore occurs.

After promotion verify actual service cwd/PIDs and cached authenticated health:
DB/owner healthy, all three queues running, no repeated global reset due solely to
one quiet market, resource telemetry includes API+decision RSS/CPU. DEGRADED is
valid when local market/reference dependencies are unavailable; it does not permit
entries through those dependencies. Do not fabricate global READY or alter user
rules/history to make verification pass.

## Final merged candidate checks

- Backend: **370 passed** (isolated temp/fake dependencies only).
- Frontend: **24 passed**, type checking passed; **4 Chrome workflows passed**,
  including serial-number continuity, recipient-specific preferences, authenticated
  rule actions, exact-trigger simulation and desktop/mobile resource/pipeline UI.
- After the installed serial merge: full parser/runtime/owner load again processed
  150,000 frames (~4,998/s), all 150,042 resulting observations processed across
  three queues, no rejection/invalidation, queues drained, max loop lag 15.59ms.
  Queue high-water: 656/656/650 of 1024. Telemetry retained one last value.
- Accelerated final-merge lifecycle run: 150,016 observations, 126 rollovers,
  294 positions/588 ledger rows, verified Decimal cash/quota/integrity/idempotency.
- Production-pinned interpreter: disposable startup/decision child/resource checks
  passed. No real SMTP/provider/deployment operations were used as tests.
