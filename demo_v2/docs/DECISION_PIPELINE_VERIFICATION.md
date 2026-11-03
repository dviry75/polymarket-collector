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

## Installed-feed verification and follow-up

Release `3618fa472fa770055685d6424964f9c89d422097` was installed at
2026-11-03 10:50:29 Asia/Jerusalem. The updater verified backups, existing IDs,
private settings and serial continuity. The service remained active with zero
restarts; liveness and readiness returned 200. Authenticated UI showed the new
API+owner CPU/RAM, disk/NIC cards and all three queues; login was retained.

Actual feed inspection found repeated 5m/15m FIFO overflow during bursts and a
separate active-health alert that stayed unhealthy after the shard recovered.
Do not treat the earlier shallow deployment success as complete feed validation.

The follow-up adds receiver backpressure between wire frames at half FIFO capacity,
retaining every observation and the original one-second queue deadline. A stalled
owner, oversized atomic frame, disconnect or retired generation still fences input.
Shard health now reflects current transport/FIFO state independently of book/source
readiness, while counters and incident history retain past failures.

- Backend after follow-up: **374 passed** with temp/fake dependencies.
- New regressions cover a paused consumer plus a 64-price wire burst, unchanged
  deadline failure, disconnect/generation wakeups, a retired wait timing out during
  recovery, and clearing a historical alert without hiding active queue/book faults.
- Synthetic wire-receiver load: 123,392 frames, 123,434 observations in 30.83s,
  all delivered/processed, zero rejection/invalidation, all three queues drained.
  Producer bursts were 512 frames; queue high-water was 512 of 1024. Observed
  throughput was ~4,003 frames/s, maximum loop lag 9.59ms. The requested producer
  rate was 8,000/s; bounded backpressure limited the achieved rate. This is not an
  8,000/s sustained-capacity claim or a guarantee for an arbitrary public feed.
- The additional public diagnostics only read unauthenticated data and counted
  frame/parser statistics; they did not submit orders or change running rules.

## Follow-up deployment verification

Release `14c96ac7c6ef40e74e58492a6f2a207d9b69e445` was promoted at
2026-11-03 11:07:51 Asia/Jerusalem. The updater verified both WAL-aware backups,
existing records, private settings and trade serials; prior release was retained.
The service runs this release with PID 450892 and zero automatic restarts.
No env/unit/nginx/DNS/firewall/LIVE changes were made.

Authenticated UI checks from approximately 11:09 through the 11:10 five-minute
rollover showed the owner alive, all three shard connections healthy, progressing
FIFO delivery and fresh current-market books after new market IDs appeared.
The earlier active overflow alerts did not recur in the observed samples, and old
transport alerts no longer remained active after recovery. Sampled queue ages were
0–80.3ms and event-loop peak lag was 22.9ms. CPU, API+owner RSS, disk/WAL and host NIC
telemetry refreshed in the UI; the existing login was retained.

Queue totals span retired generations and deliberate subscription resynchronization,
so cumulative published/delivered differences are not claimed to be zero. Bounded
queues still fence rather than replay obsolete generations. This is a short observed
production window, not a new one-hour soak of this follow-up release.

Liveness returned the simulation-only payload and readiness returned HTTP 200.
Overall health can alternate READY/DEGRADED as optional underlying source timestamps
age; those reasons remain visible and gate rules requiring that reference. DB/auth/
owner remained healthy, and scoped permission for eligible entries remained enabled.
No user rule/history reset, real order or test notification was used for validation.
