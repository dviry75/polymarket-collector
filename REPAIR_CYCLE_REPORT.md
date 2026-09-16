# Polymarket LIVE — Final Repair Cycle Report

## Verdict

**READY FOR GATED PROD ROLLOUT** on validated code SHA `36fda7fa6d37a5c623f561a7d9346729eccb0346`.

Production remains on `93cc771b42094edcd416a61ffe36f418b3d9a702`; PID `1346284`, started `2026-09-12 08:10:26 UTC`, `NRestarts=0`. No production deployment, restart, database write, configuration change, or real order occurred during validation.

## Material changes

- Non-blocking bounded STOP forensics in a separate SQLite database.
- Per-position STOP mailbox and dedicated durable-latch executor, independent of the event lock and ENTRY worker.
- Isolated exact-entry worker with final revalidation and phase telemetry intact.
- Trading/API traffic isolation, endpoint-scoped `Retry-After`, bounded endpoint telemetry, and one-call collateral snapshot.
- Targeted sellability reconciliation that cannot publish `READY`; financial mismatch escalates to full reconciliation.
- Authoritative generation check before STOP latch. A stale pre-resync obligation is rejected; a valid new-generation obligation remains actionable.
- Critical indexes for entry-by-event and position-by-token paths.

## Validation evidence

### Regression and invariants

- Full repository: `621 passed`, `9 subtests passed`, `0 failed` in `129.04s`.
- Focused repair suite before final blocker fix: `224 passed`.
- STOP fault matrix: `19 passed`; covers blocked ENTRY, simulated 8-second reconciliation, HTTP 429/Retry-After, SQLite contention, 100-update burst, duplicate prevention, and generation/resync.
- ENTRY safety retained: exact `0.74`, stale/final revalidation, generation/integrity checks, simultaneous-trigger rejection, one-entry-per-event, and no double entry.

### STOP latency — 60 full paper cycles

| Phase | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| book applied → first seen | 0.001ms | 0.001ms | 0.001ms | 0.001ms |
| first seen → obligation published | 0.090ms | 0.120ms | 0.131ms | 0.143ms |
| obligation published → consumer started | 0.083ms | 0.122ms | 0.125ms | 0.125ms |
| consumer started → durable latch | 10.389ms | 11.815ms | 13.322ms | 14.278ms |
| first seen → durable latch | 10.579ms | 12.001ms | 13.541ms | 14.445ms |
| latch → exit orchestration | 24.912ms | 29.733ms | 30.024ms | 32.658ms |
| latch → paper order submit | 24.912ms | 29.733ms | 30.024ms | 32.658ms |

No unexplained multi-second pre-latch tail was observed.

### SQLite critical path

- Baseline durable latch: p50 `5.354ms`, p95 `6.692ms`, p99 `7.893ms`, max `8.947ms`.
- Twenty forced `BEGIN IMMEDIATE` contention runs with a 350ms writer lock: first-seen→latch p50 `372.275ms`, p95/p99 `372.779ms`, max `373.111ms`; one bounded retry per run; zero duplicate latches.
- Event-loop lag under contention: p50 `1.169ms`, p95/p99 `1.962ms`, max `2.556ms`.
- `integrity_check=ok`, `quick_check=ok`, zero foreign-key violations, WAL enabled, passive checkpoint clean.
- Query plans use indexed searches for STOP position, event state, active exit, entry event, and targeted token lookup.

### Reconciliation and rate limits

- `/positions` Retry-After is scoped to reconciliation traffic; EXIT order traffic completes independently.
- No automatic retry loop exists in the observer, preventing request storms.
- Real adapter collateral snapshot uses one remote balance/allowance request.
- Targeted reconciliation updates sellability only when remote shares exactly match local remaining shares, never publishes `READY`, and delegates mismatches to full reconciliation.

### Clean soak

- SHA: `36fda7fa6d37a5c623f561a7d9346729eccb0346`; clean worktree.
- Config hash: `ab689f5e7884a67c333c4abe10fb5bc88366a0b1357927bab4356819a3a17862`.
- Duration: `316.530s`; 12 cycles; 133 safety/fault tests per cycle; 1,596 cumulative passes; zero failures; max cycle `27.174s`.
- The obsolete fixture soak was not used: it lacks current generation/update metadata and is correctly blocked fail-closed. No guard was weakened.

## Remaining blockers

None.

## Deferred non-blockers

- 72-hour production soak.
- DEMO capacity/implementation work.
- Optional migration of legacy telemetry writes off the event loop.
- Entry-frequency optimization and non-critical dashboard cleanup.

## Exact gated rollout and rollback

Deployment requires explicit operator approval. Keep entries disabled and kill switch active throughout backup, checkout, migration, and initial restart.

1. Record current PID, service start time, `git rev-parse HEAD`, dirty-tree status, IPC `STATUS`, unresolved intents, active positions, and remote open orders.
2. Run `sudo systemctl start polymarket-live-backup.service`; require `Result=success`, then verify the newest `live_backups` row has `status='ok'`, positive size, and SHA-256.
3. Preserve the existing dirty production tree separately. Do not reset, overwrite, or include its unrelated changes. Materialize a clean release checkout at exact SHA `36fda7fa6d37a5c623f561a7d9346729eccb0346` and verify `git status --porcelain` is empty.
4. With entries disabled, stop only `polymarket-trader.service`. Install locked dependencies with `/opt/polymarket-btc-live/.venv/bin/pip install -r polymarket-collector/requirements.lock.txt` only if the lock file differs from production.
5. From the release checkout run `/opt/polymarket-btc-live/.venv/bin/python -c "from pathlib import Path; from live.config import LiveConfig; from live.repository import LiveRepository; from live.strategy_repository import StrategyRepository; c=LiveConfig.from_env(); r=LiveRepository(Path(c.live_db_path)); r.migrate(c.live_kill_switch_default); StrategyRepository(r).migrate()"` using `/etc/polymarket-live/trader.env`. Require successful exit and `PRAGMA integrity_check='ok'`.
6. Point the service working directory to the clean release checkout, run `sudo systemctl daemon-reload`, then `sudo systemctl start polymarket-trader.service`. Do not enable entries yet.
7. Require `active/running`, new PID, `NRestarts=0`, shallow health, valid IPC `STATUS`, reconciliation `READY` with fresh successful evidence, both WebSockets fresh, all books ready/aligned, heartbeat current, no unresolved/unknown intent, no financial contradiction, and no open operator-action quarantine.
8. Arm one canary through the official control path. Enable entries only through `RESUME_ENTRIES`/Dashboard after `EntryReleaseEvaluator` returns no blockers; never edit pause state directly in SQLite.
9. Watch STOP phase p99/max, mailbox depth/retries/rejections, stale-generation rejection, exit submissions/duplicates, event-loop lag, DB lock latency, reconciliation duration, endpoint 429/Retry-After, request queue wait, WS resync/disconnects, forensic queue/drops, CPU and memory.
10. Abort immediately on multi-second unexplained first-seen→latch, duplicate order, stale-generation exit, deadlock, queue growth, resync/retry storm, reconciliation blocking trading, state corruption, crash, or failed release gate. Re-enable kill switch and pause entries through official controls, stop the trader, return the service to clean SHA `93cc771b42094edcd416a61ffe36f418b3d9a702`, run the same migration/import checks, and restart fail-closed. Restore the database only as a separately approved maintenance event after checksum verification; never replace live SQLite while the process is running.
