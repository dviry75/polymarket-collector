# Polymarket LIVE Agent Rules

Before starting any task, read the operational documentation in this order:

1. `AGENTS.md`
2. `SYSTEM_CONTEXT.md`
3. `CURRENT_STATE.md`

`CURRENT_STATE.md` is informational only. All dynamic state relevant to the task must still be re-verified before acting.
Never assume documented runtime values are current.
Always verify live runtime state directly before acting.
This is a **REAL_TRADING production system**.
Safety mechanisms must not be bypassed merely to make the system appear healthy.

## Scope and locations

- Git repository: `/opt/polymarket-btc-live/repo`
- Trading application: `/opt/polymarket-btc-live/repo/polymarket-collector`
- Runtime root and active SQLite DB: `/opt/polymarket-btc-live`
- Active trading service: `polymarket-trader.service`; the disabled legacy `polymarket-live.service` is not the trading-core source of truth.
- The services execute the checked-out working tree directly. A dirty tree or files modified after process start may differ from the code currently loaded in memory.

## Non-negotiable safety rules

- Treat every task as production trading work unless explicitly proved otherwise.
- Begin read-only. Inspect Git status, service state, runtime `STATUS`, pause/recovery state, unresolved intents, positions, orders, reconciliation, WebSockets, heartbeat, DB/WAL size, and recent errors before proposing a mutation.
- Never expose or copy secrets. Do not print `/etc/polymarket-live/*.env`, `/proc/*/environ`, private keys, API credentials, tokens, passphrases, password hashes, cookies, mnemonics, or raw authenticated payloads.
- Never use a bypass, disable a guard, clear state, alter history, or force readiness to make the system appear healthy.
- Do not manually edit the SQLite DB. Do not mutate `live_system_state`, intents, positions, orders, fills, reconciliation rows, pause state, canary state, or kill switch.
- Do not send/cancel orders, trade, redeem, run emergency close, run reconciliation/recovery, restart services, deploy, migrate, or change runtime flags without explicit task authorization and an operation-specific safety review.
- A `GET /health` result of `ok` is shallow. It does not prove entries are enabled, books are fresh, reconciliation is clean, or financial state matches Polymarket.
- Dashboard values and reports may be cached, derived, historical, stale, or display-only. Use the Sources of Truth in `SYSTEM_CONTEXT.md`.
- Never treat a historical row (especially an old intent, event, report, account snapshot, or system-state timestamp) as current without checking its lifecycle semantics.

## Required workflow before a change

1. Read `SYSTEM_CONTEXT.md` and the current implementation, not only old reports/runbooks.
2. Run `git status --short --branch` and preserve all pre-existing user changes. Never overwrite or “clean up” a dirty tree.
3. Verify the installed systemd unit, `ExecStart`, `WorkingDirectory`, `EnvironmentFiles` paths, service start time, and source-file mtimes. Do not assume repository deployment artifacts equal installed units.
4. Use only the documented read-only checks. Confirm the live trading service, IPC `STATUS`, pause owner/cause/generation/policy, recovery blockers, strategy and reconciliation readiness, WebSocket/book freshness, heartbeat, unresolved intents, active positions, and recent reconciliation results.
5. Identify the authoritative source for every value being changed. Distinguish remote financial truth, durable local intent, hot RAM cache, DB history, and UI projection.
6. Trace root cause and failure path before patching. Do not patch the symptom, loosen a threshold, suppress an alert, or reclassify a pause without evidence.
7. Keep scope minimal. Avoid unrelated refactors, schema changes, dependency upgrades, config changes, or operational actions.
8. Define rollback and validation before editing. Any action that can affect orders, positions, credentials, DB schema, systemd, nginx, or runtime state requires explicit approval.

## Required workflow after an authorized change

- Review the exact diff and verify that no secrets, backups, DB files, env values, or unrelated dirty-tree changes were included.
- Run the smallest relevant isolated tests first; tests must use temporary DBs/fake adapters and must not load `/etc/polymarket-live/trader.env`.
- Run broader tests only after confirming they cannot target the live DB, network trading APIs, SMTP, GCS, or production services.
- Re-check config validation and architecture isolation where relevant.
- Do not restart/deploy merely because tests pass. Deployment and restart require separate explicit authorization.
- After an authorized deployment, verify systemd identity/start time, shallow health, IPC `STATUS`, pause ownership, all release gates, reconciliation evidence newer than the change/pause, WebSocket/book freshness, heartbeat, unresolved intents, active positions, open remote orders, logs, and resource usage.
- Never auto-resume or deactivate the kill switch as a validation step. A healthy safety pause is preferable to false green status.

## Safe default validation

Source-only unit tests are normally run from `polymarket-collector` with the project venv and fake/temp dependencies. Before running them, confirm no test inherits `LIVE_DB_PATH=/opt/polymarket-btc-live/poly_live.sqlite3` and no production env file is sourced:

```bash
cd /opt/polymarket-btc-live/repo/polymarket-collector
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests/<relevant_test_file.py>
```

Do not run operational scripts as tests. In particular, migration, reporting, archive, resolution, WebSocket smoke/soak, backup/restore, and adapter-readiness scripts can write state, send email, call external services, or load production configuration.

