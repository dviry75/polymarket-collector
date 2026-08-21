# Polymarket BTC LIVE System Context

This document records relatively stable architecture and safe discovery commands. It intentionally does **not** freeze current runtime values. Any service state, pause, readiness, intent, position, order, balance, WebSocket condition, heartbeat, reconciliation result, Git branch/status, or disk usage is dynamic and must be re-read before acting.

## 1. Stable topology

| Item | Path / identity | Notes |
|---|---|---|
| Runtime root | `/opt/polymarket-btc-live` | DB, venv, backups, deployment metadata and repository live here. |
| Git root | `/opt/polymarket-btc-live/repo` | Remote currently named `origin`; branch and dirty state are dynamic. |
| Main application | `/opt/polymarket-btc-live/repo/polymarket-collector` | Trading core, dashboard, live package, tests, scripts and deployment examples. |
| Legacy/local collector | `/opt/polymarket-btc-live/repo/polymarket-btc-local` | Separate older/local collector; not the production trading core. |
| Python environment | `/opt/polymarket-btc-live/.venv` | Used by installed systemd units. |
| Active DB | `/opt/polymarket-btc-live/poly_live.sqlite3` | SQLite WAL database; `-wal` and `-shm` are part of the live state. Never copy only the main file as a “consistent backup”. |
| Backups | `/opt/polymarket-btc-live/backups` | Contains managed and historical/manual backups; retention and disk use require runtime verification. |
| Deployment metadata | `/opt/polymarket-btc-live/deployment-state` | Historical runbooks/checks; may describe the old monolithic service and may be stale. |
| Installed units | `/etc/systemd/system/polymarket-*.service`, `*.timer` | Installed units are authoritative over files under `deploy/`. |
| Config files | `/etc/polymarket-live/trader.env`, `dashboard.env`, `live.env`, `smtp.env` | Paths only. Never print their contents. Backup/original env files also exist and are secrets-sensitive. |
| nginx site | `/etc/nginx/sites-available/live-poly` | HTTPS/static dashboard/auth proxy to dashboard on `127.0.0.1:8001`. |
| Trader IPC | `/run/polymarket/trader.sock` | Unix socket owned by the trading process; dashboard sends commands through it. |

The systemd processes import Python directly from the working tree; there is no copied release artifact or container boundary. Therefore Git `HEAD`, current files, installed units, and the code already loaded by a running process can differ. Compare `ActiveEnterTimestamp`/`ExecMainStartTimestamp`, file mtimes, `git diff`, and deployment evidence before attributing runtime behavior to current source.

## 2. Repository map and important files

### Entrypoints

- `polymarket-collector/trader_app.py`: production trading-core FastAPI app on loopback port 8002. Owns migrations/startup hold, market discovery, reconciliation, market/user WebSockets, strategy workers, heartbeat, pause recovery, geographic checks, DB-growth sampling, and IPC server.
- `polymarket-collector/dashboard_app.py`: credential-free/query-only DB process on loopback port 8001. Owns login and dashboard APIs; obtains live in-memory health from trader IPC.
- `polymarket-collector/live_app.py`: legacy monolithic control-center entrypoint. Its installed service is disabled; do not confuse it with the active trader/dashboard split.
- `polymarket-collector/app.py`: older DEMO collector entrypoint, not the production trading core on this server.
- `polymarket-collector/scripts/run_market_resolution.py`: separate cold-path resolution loop.
- `polymarket-collector/scripts/run_reporting.py`: reporting DB writer/email sender used by the hourly oneshot.
- `polymarket-collector/scripts/run_live_archive.py`: archive job; it migrates/opens the live DB and can upload/delete archived rows. It is not a read-only diagnostic.

### Core code

- Configuration/arming: `live/config.py`
- Service wiring and HTTP actions: `live/router.py`, `live/trader_commands.py`, `live/ipc.py`
- Market and User WebSockets: `live/market_websocket.py`, `live/order_book.py`
- Strategy policy/decisions: `live/strategy.py`
- Strategy runtime/actions: `live/strategy_runtime.py`
- Durable strategy state: `live/strategy_repository.py`
- Base schema/control/history: `live/repository.py`
- Real/mock adapters: `live/adapters/polymarket.py`, `live/adapters/mock.py`
- Order API forensic journal: `live/order_attempts.py`
- Legacy order/risk path: `live/order_manager.py`, `live/risk_manager.py`, `live/trading_engine.py`
- Reconciliation: `live/reconciliation.py`, `live/reconciliation_stability.py`
- Pause/recovery: `live/pause_recovery.py`, `live/recovery_policy.py`
- Market resolution: `live/market_resolution.py`
- Dashboard projection/provenance: `live/dashboard_read_model.py`, `live/dashboard_api.py`, `live/dashboard_schema.py`
- Reporting/archive/backup: `live/reporting.py`, `live/archive.py`, `live/backup.py`
- Tests: `polymarket-collector/tests/`
- Deployment examples: `polymarket-collector/deploy/`; installed `/etc` units remain authoritative.

There is no conventional migrations directory. Schema creation/additive migration is implemented in Python (`LiveRepository.migrate`, `StrategyRepository.migrate`, `migrate_dashboard_schema`, reporting `SCHEMA`) plus `scripts/migrate_dashboard_schema.py`. These paths write to DB and must never be run casually against LIVE.

## 3. Installed services and timers

All status values below are dynamic; use `systemctl` at task time.

| Unit | Role and command | Config / dependencies | Status and logs |
|---|---|---|---|
| `polymarket-trader.service` | Active trading core: `uvicorn trader_app:app --loop uvloop --host 127.0.0.1 --port 8002 --no-access-log` | Working directory `polymarket-collector`; `/etc/polymarket-live/trader.env`; network; owns `/run/polymarket/trader.sock`; ordered before dashboard. | `systemctl status polymarket-trader.service`; `journalctl -u polymarket-trader.service`. |
| `polymarket-dashboard.service` | Read-only dashboard: `uvicorn dashboard_app:app --host 127.0.0.1 --port 8001` | `/etc/polymarket-live/dashboard.env`; ordered after trader; DB opened `mode=ro`/`query_only`; repository/venv/DB are systemd read-only paths. | `systemctl status polymarket-dashboard.service`; corresponding journal. |
| `polymarket-market-resolution.service` | Continuous public REST resolution reconciler: `python scripts/run_market_resolution.py` | Inline `LIVE_DB_PATH` and interval/grace/batch settings in installed unit; network. It writes resolution and audit rows. | Status/journal for the unit. |
| `polymarket-hourly-report.timer` | Runs at minute 03 each hour with small randomized delay. | Triggers `polymarket-hourly-report.service`. | `systemctl list-timers --all`; timer status. |
| `polymarket-hourly-report.service` | `python -m scripts.run_reporting tick`; finalizes report rows and may send email. | `trader.env` and `smtp.env`; DB/network. This is not read-only despite “observability only” description. | Service status/journal. |
| `polymarket-live-backup.timer` | Daily with randomized delay. | Triggers installed-only backup oneshot. | Timer status and backup service journal. |
| `polymarket-live-backup.service` | Uses SQLite backup manager and writes compressed backup/audit metadata. | `live.env`; writes under runtime root. Unit is installed but no matching tracked deploy file was found. | Service status/journal. |
| `polymarket-live-archive.timer` | Daily around 02:17 UTC plus randomized delay. | Triggers archive oneshot. | Timer status and archive service journal. |
| `polymarket-live-archive.service` | `python scripts/run_live_archive.py`; validates config, archives old snapshots to GCS, verifies readback, then deletes archived local rows. | `live.env`, network/GCS, live DB. Highly state-changing. | Service status/journal. |
| `polymarket-live.service` | Legacy monolithic `uvicorn live_app:app --host 127.0.0.1 --port 8001`. | `live.env`; conflicts conceptually/port-wise with dashboard. Installed disabled/inactive at discovery time; verify, never start casually. | Status/journal if investigating legacy history. |

Installed units use systemd hardening (`NoNewPrivileges`, strict filesystem protection, private tmp/devices, restricted address families, empty capability sets where applicable) and explicit `ReadWritePaths`.

## 4. Runtime architecture

```text
Public Gamma/CLOB discovery + Market WS
  -> in-memory market metadata and generation-aware order books
  -> atomic frames / exact-trigger queue
  -> LiveStrategyRuntime
  -> durable event lock + intent in SQLite before any network submission
  -> RealPolymarketTradingAdapter / authenticated CLOB SDK
  -> User WS events + REST reconciliation
  -> durable fills, position, exit intents and lifecycle

Authenticated CLOB REST/SDK -----------------------> ReconciliationWorker
Durable SQLite strategy/control/history ----------> ReconciliationWorker
Reconciliation + WS + heartbeat + config ----------> PauseRecoveryCoordinator

Dashboard (query-only SQLite) + trader IPC STATUS -> authenticated web UI/API
Resolution service (public REST) ------------------> market resolution rows
Hourly timer --------------------------------------> report rows + email
Archive/backup timers -----------------------------> storage maintenance
```

The hot entry path uses RAM snapshots refreshed from SQLite approximately every 250 ms. If this hot snapshot is older than one second or cannot refresh, entry fails closed. The durable DB gates are rechecked before reservation/submission. RAM improves latency; it does not replace durable state or remote financial truth.

## 5. Sources of Truth

| State | Authoritative source | Cached / historical / display-only alternatives |
|---|---|---|
| Service running state | systemd installed unit: `ActiveState`, `SubState`, `MainPID`, process start time | A DB heartbeat or dashboard status can remain stale after a crash. |
| Code/config loaded by process | Installed `ExecStart`/working directory + process start time + deployment evidence; effective `LiveConfig` was loaded at startup | Git `HEAD`, current disk files and env files alone do not prove what a running Python process loaded. Never dump process env. |
| Trading submission armed | In-process `LiveConfig.real_submission_armed()` plus current durable kill/pause/canary/release gates | Individual env flags or dashboard labels are insufficient. Arming requires LIVE + REAL_TRADING + module/trading/order-submission + polymarket adapter + continuous/canary + safe defaults. |
| Kill switch | `live_system_state.key='kill_switch'` | Config default only initializes missing state; it is not current state. |
| Pause, owner, cause, policy, generation | Coherent `live_system_state` pause record read by `StrategyRepository.pause_record()` | `pause_reason`/`pause_entries` timestamps can differ because state evolved; old audit/report rows are historical. Use the full record and recovery evaluation. |
| Strategy readiness | Trader in-memory Market WS/books and `live_system_state.strategy_readiness/block_reason`; IPC `STATUS` is the best live view | DB keys are last persisted state; dashboard/report may cache/project them. |
| Reconciliation readiness | Latest completed reconciliation evidence plus `reconciliation_readiness`, `live_blocked_by_reconciliation`, block reason and last successful timestamp | A later `running`/failed run matters; a historical `ok` row alone is insufficient. |
| Entry release safety | `EntryReleaseEvaluator` output exposed through IPC `STATUS.recovery.evaluation` | `pause_entries=false` alone does not mean entry is allowed; state can be `GATED`. |
| Local open positions | `live_strategy_positions` states and shares, interpreted with intents/fills | Legacy `live_positions`, strategy deals, dashboard values and reports are derived/history and may disagree. |
| Actual Polymarket token position | Authenticated conditional-token balance when available; reconciler also uses remote positions/trades and applies propagation grace | Public/data-API positions can lag. Latest account snapshot is only last-known evidence. Local positions are the system's durable belief, not external truth. |
| Execution intents | `live_strategy_intents` plus `live_order_attempts` and `live_strategy_fills` | `live_orders` is the older/legacy order path; an intent without remote ID may still be unknown after submission, except explicitly proven local-only states. |
| Open remote orders | Authenticated adapter `get_open_orders()` / `get_order()` | Local intent/order rows are durable expectations/history. Dashboard open orders are projections. |
| Fills/trades | Remote account trades/User WS evidence, deduplicated into `live_strategy_fills`; reconciliation verifies/apply fills | Intent aggregate fields are derived from durable fills; reports are summaries. Maker child matched amount is used for maker exits. |
| Wallet collateral balance/allowance | Authenticated CLOB `get_balance_allowance` read | `live_account_snapshots` is last-known; dashboard equity can be unavailable/derived. Never infer current cash from an old snapshot. |
| Market WS connection/books/freshness | Trader's live `MarketWebSocketManager.health()`/`event_freshness()` via IPC STATUS | `market_ws_status`, `live_markets`, and `live_market_snapshots` are persisted last-known/history. A connected socket can still have unready/stale/misaligned books. |
| User WS status/freshness | Trader's live `UserWebSocketManager.health()` via IPC STATUS | DB `user_ws_*` keys and websocket event rows are last-known/history. |
| Order heartbeat | Trader state plus `order_heartbeat_status` and `last_successful_heartbeat_at`; for recovery from heartbeat pause, success must be newer than pause acquisition | `status=OK` without timestamp ordering is insufficient. |
| Market resolution | Public market resolver evidence persisted to `live_markets.market_resolved/winner`; strategy position resolution is then durable in strategy tables | Reporting rows are derived. Redemption is separate. |
| Redemption | Confirmed on-chain/adapter result plus durable redemption/position lifecycle evidence | A `REDEEM_PENDING` position is not redeemed. No production redemption caller was found; adapter method is guarded by explicit authorization. |

## 6. Trade lifecycle implemented in current source

1. Market discovery maintains current BTC Up/Down 5-minute markets and token mapping. Market WS subscribes to the YES/NO assets, rebuilds generation-aware books, rejects stale/future/out-of-order/misaligned frames, and schedules atomic frames.
2. Entry signal is an exact best ask of `0.74`, during the last 120 seconds of the event. Both sides at the trigger causes a durable skipped event. Entries are schedule-gated in `Asia/Jerusalem`; inspect `entry_schedule_status` because it contains code-level calendar logic.
3. Eligibility requires verified BTC 5m market scope/token mapping, event-ready books, no event lock, capacity/exposure, valid tick/minimum/fee constraints, valid config, fresh hot state, and all durable safety gates.
4. `reserve_event_entry` transactionally locks the event and inserts an ENTRY intent/deal before network I/O. Unique indexes and idempotency prevent duplicate unresolved entry/exit intents. In canary mode the first reservation consumes/disarms the canary and pauses further entry.
5. REAL entry changes the intent to `SUBMITTING`, verifies allowance/balance without automatic approval, signs a capped BUY FAK order, journals STARTED/RESULT attempts, posts it, then immediately reconciles. A confirmed FAK zero-fill can unlock the event; an uncertain/live/matched response remains reconciliation-required.
6. Remote trades/User WS/reconciliation persist deduplicated fills. Positive entry fill opens `live_strategy_positions`; partial FAK entry becomes the actual position and remaining unfilled request is final, not silently retried.
7. A GTC SELL take-profit at `0.96` is reserved after the position is sellable. If balance propagation lags, it remains local-only `WAITING_SELLABLE` until remote sellability is verified.
8. Best bid `<=0.66` durably latches the stop one-way before remote action. The TP/other exit must be cancelled and reconciled before a SELL FAK is sent. Stop retries only on materially changed executable liquidity and uses a `0.01` floor. Cancellation or remote-order identity uncertainty moves the position to reconciliation-required and blocks another SELL.
9. SELL FAK partial fills reduce remaining/sellable shares and may leave OPEN/EXITING/DUST depending on size and purpose. Unknown/cancel-uncertain state remains fail-closed.
10. Operator emergency close first pauses entries and reconciles, then attempts position exits through the same guarded path. The configured `0.60` emergency threshold exists, but no automatic `EMERGENCY_060` trigger call was found in current source; do not assume it is active.
11. Resolution marks winners/losers and winner positions `REDEEM_PENDING`; paper mode redeems synthetically. Real adapter redemption exists but no production caller was found, so redemption must not be assumed automatic.

Primary failure paths: stale/unready books; WS disconnect/auth/staleness; hot-state staleness; schedule/window/scope mismatch; simultaneous trigger; exposure/entry slot; invalid dynamic market constraints; config/arming/kill/pause/canary; insufficient balance or allowance; unknown result after submission; zero/partial fill; TP balance propagation; uncertain cancellation; missing remote order identity; remote/local position contradiction; reconciliation backoff/failure; dust; unresolved redemption.

## 7. Safety and entry blockers

The sole release-gate evaluator is `EntryReleaseEvaluator`; actual entry also has strategy and adapter gates. Major blockers:

| Blocker group | Trigger / effect | Durable owner and recovery |
|---|---|---|
| Operator pause / kill switch | Explicit operator state; blocks entry. | `live_system_state`; manual-only. Never clear as troubleshooting. |
| Configuration/arming | Validation error or incomplete REAL arming. Adapter refuses submission without full arming and durable intent. | Process config + DB guard state; fix config only through approved deployment, then re-verify. |
| Canary | Required when continuous mode is off; first reservation consumes it. | `canary_armed/consumed`; operator-owned/manual. |
| Market WS/books | Not connected, stale/future/out-of-order frames, missing snapshot, generation mismatch, top/depth mismatch, no subscribed books. | Live RAM + persisted readiness/block reason; transient causes can auto-recover only after fresh ready evidence and stability. |
| User WS | Not connected/stale/auth failure; queue loss or persistence uncertainty. | Live RAM + DB. Reconnect triggers reconciliation; loss/uncertainty requires clean financial verification. |
| Strategy readiness/hot state | NOT_READY, stale hot snapshot, processing failure, invalid market scope/constraints. | RAM + DB. Transient data causes may auto-recover; internal failure/scope/config may be manual. |
| Reconciliation | Not ready, blocked, stale/unclean evidence, gap/contradiction/failure/rate limit. | Reconciliation tables + system state. Repairable gaps need clean reconciliation newer than pause; contradictions/failures are manual review. |
| Financial uncertainty | Unresolved intent, unknown fill/cancel, exit reconciliation required, active position/exposure, occupied entry slot. | Strategy intents/fills/positions + remote truth. Never delete/terminalize to free the slot without proof. |
| Heartbeat | REAL armed heartbeat not OK; recovery requires a success newer than pause. | DB state plus live worker; transient auto recovery only with timestamped evidence. |
| Geography | Official preflight not ALLOWED or evidence older than TTL. | DB last evidence plus periodic preflight; failed compliance state is manual review. |
| Risk limits | Daily loss, consecutive failed orders/losses, amount/token/exposure/open order/deal/rule caps. | Daily limits and strategy state; risk pauses are non-auto-recoverable. |
| Schedule/event | Outside schedule, before/after entry window, event already locked, simultaneous sides, resolved event. | Code policy + durable event state; no bypass. |

Pause state is generation-based. Recovery uses compare-and-swap on generation/owner to reject stale releases. Transient blockers are debounced; when all gates are clean, they must remain stable for the configured window before auto release. Repairable pauses require `recovery_financial_verified_generation` and a clean reconciliation newer than pause acquisition. Manual resume uses the same gates and only bypasses the policy's requirement for an automatic release; it does not bypass readiness.

## 8. Reconciliation and recovery

Reconciliation runs at startup, periodically (faster while unresolved intents/active positions exist), on Market/User WS reconnect, after user order/trade events, and synchronously around entry/exit/cancel/emergency actions. Actual intervals are config-driven and dynamic.

It reads authenticated identity/account mode, collateral balance/allowances, remote open orders, account trades, remote positions, and conditional-token balance cross-checks. It compares those with legacy non-final orders and durable strategy intents/fills/positions. It can ingest missing fills, finalize known terminal orders, open a position from confirmed fills, apply maker-child exit fills, and reconcile bounded remote position corrections.

Gap classes include local order missing remote, remote order missing local, intent without remote ID outside the in-flight grace, unknown remote status, exit fill without position, unknown-market remote position, remote position after confirmed exit, local position missing remote/balance contradiction, and repair verification mismatch. Short submission/position propagation and missing-position suspect windows avoid premature gaps.

Any gap makes the run `gaps`, persists JSON evidence, raises alerts, and blocks readiness. Only a narrowly bounded set of propagation corrections is classified auto-recoverable; contradictions are manual-only. Exceptions disarm canary, persist failed runs, and distinguish rate-limit/temporary network errors from hard failures.

Storm safeguards: one asyncio reconciliation lock; adaptive periodic interval; 5–60 second exponential backoff with jitter after gaps/errors; a monotonic retry deadline; User WS queue bounds; intent and position propagation grace; missing-position suspect grace; pause acquisition debounce; recovery stability window; pause generation CAS.

Proof of HEALTHY requires all of the following at the same observation: service live; config valid; Market WS connected and every subscribed book ready/fresh; User WS connected/fresh; strategy READY; reconciliation READY/unblocked with a completed clean run newer than the relevant pause; no unresolved/unknown financial state; heartbeat OK with required timestamp ordering; geography allowed/fresh; kill switch/canary policy satisfied; recovery engine healthy; no current evaluator blockers; and pause actually released (or intentionally remains manual). A shallow health 200 or latest old clean run is not proof.

## 9. Persistence and historical-state hazards

The DB uses WAL. Important tables:

- Control: `live_system_state`, `live_reconciliation_runs`, `live_account_snapshots`, `live_audit_log`, `live_alerts`.
- Market/history: `live_markets`, `live_market_snapshots`, `live_websocket_events`.
- Durable strategy: `live_event_states`, `live_strategy_intents`, `live_order_attempts`, `live_strategy_fills`, `live_strategy_positions`, `live_strategy_deals`, `live_audit_timeline`.
- Legacy execution path: `live_orders`, `live_order_fills`, `live_positions`, `live_deals`, `live_rules`.
- Reporting/provenance: `live_event_reports`, `live_hourly_reports`, `live_strategy_runs`, `live_position_events`, `live_redemptions`, dashboard cutover/verification tables.
- Operations: `live_backups`, `live_archive_runs`, schema migration tables.

Hazards:

- `live_orders`/`live_positions` are not the primary current strategy lifecycle; use `live_strategy_*` plus remote truth.
- A DB WebSocket key is last-known, not live connection truth.
- Account snapshots, reports and dashboard metrics are samples/projections. They may be stale or reconstructed and cannot prove current cash/positions/orders.
- Dashboard provenance labels (`PENDING`, `DERIVED`, `OBSERVED`, `VERIFIED`) describe evidence quality; legacy rows may retain `UNKNOWN`.
- Reporting finalization reads system state at report-generation time, so report readiness/WS labels are summaries, not exact replay of every instant in an event.
- `pause_entries=false` can coexist with release-gate blockers (`GATED`). Conversely a clean reconciliation does not automatically override a manual-only pause.
- An unresolved intent with no remote ID must not be deleted based on absence alone; unknown-after-submission can represent a real remote order.

## 10. Safe read-only operational checks

Run from a normal shell without sourcing production env files. These commands do not intentionally mutate state.

```bash
# Repository identity (dynamic)
cd /opt/polymarket-btc-live/repo
git branch --show-current
git status --short --branch
git remote -v
git diff --stat

# Installed runtime identity
systemctl status polymarket-trader.service polymarket-dashboard.service polymarket-market-resolution.service --no-pager
systemctl show polymarket-trader.service -p FragmentPath -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp -p ExecStart -p WorkingDirectory -p EnvironmentFiles
systemctl list-timers --all --no-pager | grep polymarket

# Recent logs (keep bounded; never dump env or raw authenticated payloads)
journalctl -u polymarket-trader.service -n 100 --no-pager -o short-iso
journalctl -u polymarket-dashboard.service -n 50 --no-pager -o short-iso
journalctl -u polymarket-market-resolution.service -n 50 --no-pager -o short-iso
journalctl -u polymarket-live-backup.service -n 30 --no-pager -o short-iso
journalctl -u polymarket-live-archive.service -n 30 --no-pager -o short-iso

# Shallow liveness only
curl -fsS http://127.0.0.1:8002/health
curl -fsS http://127.0.0.1:8001/health

# Best live in-memory status: STATUS is read-only. Do not substitute another IPC command.
cd /opt/polymarket-btc-live/repo/polymarket-collector
/opt/polymarket-btc-live/.venv/bin/python -c "import json; from live.ipc import TraderIPCClient; print(json.dumps(TraderIPCClient('/run/polymarket/trader.sock', timeout_seconds=2).call('STATUS'), sort_keys=True, default=str))"

# Durable pause/readiness/heartbeat state; DB must be opened read-only.
sqlite3 -readonly -header -column /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT key,value,updated_at FROM live_system_state WHERE key IN ('kill_switch','pause_entries','pause_owner','pause_cause','pause_reason','release_policy','pause_generation','strategy_readiness','strategy_block_reason','reconciliation_readiness','reconciliation_block_reason','live_blocked_by_reconciliation','last_successful_reconciliation_at','market_ws_status','user_ws_status','user_ws_last_message_at','order_heartbeat_status','last_successful_heartbeat_at','recovery_status','recovery_engine_status','canary_armed','canary_consumed','geographic_availability','geographic_checked_at') ORDER BY key;"

# Recent reconciliation state
sqlite3 -readonly -header -column /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT id,started_at,finished_at,status,gaps_count,error FROM live_reconciliation_runs ORDER BY id DESC LIMIT 20;"

# Local durable financial state. These are local beliefs, not remote truth.
sqlite3 -readonly -header -column /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT position_id,event_id,token_id,state,remaining_shares_text,sellable_shares_text,stop_stage,tp_intent_id,active_exit_intent_id,updated_at FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED','DUST','REDEEM_PENDING') ORDER BY created_at;"
sqlite3 -readonly -header -column /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT intent_id,event_id,position_id,action,purpose,state,remote_order_id,filled_shares_text,remaining_shares_text,reason_code,created_at,updated_at FROM live_strategy_intents WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED') ORDER BY created_at;"
sqlite3 -readonly -header -column /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT record_id,attempt_id,phase,occurred_at,operation,purpose,intent_id,result_status,success,remote_order_id,error_code,http_status FROM live_order_attempts ORDER BY occurred_at DESC LIMIT 50;"

# Resource state
df -h /opt/polymarket-btc-live
du -sh /opt/polymarket-btc-live/poly_live.sqlite3 /opt/polymarket-btc-live/backups /opt/polymarket-btc-live/repo
```

Authenticated remote reads are the financial truth but can consume rate limits and require credentials. Prefer the running reconciler's bounded evidence. Do not instantiate ad-hoc production adapter scripts unless explicitly authorized. Never use `runtime_readonly_checks.py` as a pure read-only command without reviewing it: it creates session material and exercises POST guard paths.

For integrity, do not run a full `PRAGMA integrity_check` on the multi-GB active DB during trading without an I/O-impact review. Verify a consistent restored backup copy instead. `deployment-state/backup_restore_test.py` writes a restore-test file, so it is not a read-only command even though it does not replace LIVE.

## 11. Testing safety

The tracked `tests/` suite is designed around temporary SQLite DBs, mock adapters/fake secure clients and fixture WebSockets. Real-adapter tests inspected here inject fake clients. Run a focused file first, without loading production env:

```bash
cd /opt/polymarket-btc-live/repo/polymarket-collector
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests/test_pause_recovery.py
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests/test_reconciliation_stability.py
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests/test_live_full_strategy.py
```

Before any full-suite run, confirm tests do not inherit the live DB path or service credentials. Safe validation groups include architecture isolation, strategy, market-data safety, pause recovery, reconciliation stability/accounting, order attempts, user WS, dashboard read model, resolution with fake client, reporting with fake transport, and archive with temp/fake storage.

Do **not** treat scripts as tests. The following are stateful or externally active: `migrate_dashboard_schema.py`; `run_reporting.py` (DB writes/email); `run_live_archive.py` (DB/GCS/deletion); `run_market_resolution.py` (DB writes/network loop); `run_live_archive.py`; `run_market_ws_smoke.py`; latency/soak/capture scripts; backfill scripts; admin creation; backup/restore tooling; and any adapter/readiness script loading live env. Coinbase integration/investigation scripts also use the network and may write outputs.

## 12. Deployment architecture and safe change boundary

No CI/CD release pipeline or container build was found. systemd executes the checkout directly. Deployment examples and older rollback documentation describe a monolithic service and are partly stale relative to the installed trader/dashboard split. The `deployment-state/current-commit` marker observed during discovery did not match current `HEAD`; never use it alone as deployed-version proof.

An authorized deployment must be treated as a coordinated production operation: establish clean/known source provenance; preserve dirty-tree work; obtain a consistent backup; inspect installed unit/config drift without printing secrets; validate config/imports/tests; define pause/position/order/reconciliation preconditions; update only approved artifacts; validate systemd/nginx; restart only explicitly approved units; then prove financial/runtime health through current sources of truth. Do not make deployment/restart part of an ordinary code edit by implication.

## 13. Common failure modes supported by current evidence/code

- Market WS reconnect loop or exchange timestamps outside the one-second threshold: socket may say CONNECTED while strategy/books remain NOT_READY.
- User WS receive timeout/reconnect/auth failure: entries gate closed and reconnect reconciliation runs.
- Book top/depth alignment mismatch or stale generation: resync, discarded frames, readiness hold.
- Reconciliation contradiction or unknown intent/order identity: manual-only pause even after later clean runs until policy/gates permit a reviewed release.
- `WAITING_SELLABLE`: position tokens not yet visible as sellable; exit/TP remains local-only and occupies the entry/exit slot.
- Cancellation uncertainty: no second SELL is sent; position moves to exit reconciliation required.
- FAK zero/partial fill: only confirmed zero-fill is terminalized/unlocked; unknown result remains unresolved.
- Working-tree/runtime drift: a source edit after service start is not loaded, making code inspection alone misleading.
- Installed-unit/deploy-doc drift: legacy `polymarket-live` documentation can point to the wrong service and port owner.
- Archive job config validation failure prevents retention/archive progress; DB growth can continue.
- Large live DB/backups can consume disk and make backup/integrity operations expensive.
- Historical dashboard/report/system-state rows can look current unless timestamp/provenance/lifecycle is checked.

## 14. Dated discovery observations requiring re-verification

These are findings from **2026-08-21 UTC**, not permanent facts:

- The working tree was already heavily modified/untracked before these context files were added. No existing changes were altered.
- `polymarket-trader`, dashboard and market-resolution were running; legacy `polymarket-live` was inactive. Re-check systemd.
- Trader shallow health was `ok` while strategy readiness was `NOT_READY`, demonstrating why shallow health is insufficient.
- Runtime STATUS showed a manual-only reconciliation-contradiction pause, an unresolved `WAITING_SELLABLE` intent, Market WS freshness failures/reconnects and User WS reconnecting. Later values may differ; do not act from this snapshot.
- Recent clean reconciliation runs coexisted with the manual pause and unresolved intent; clean reconciliation alone did not prove resume safety.
- The archive oneshot had failed repeatedly with `invalid safe LIVE configuration`. Do not fix within unrelated work; inspect config validation safely under an authorized task.
- The backup directory was about 13 GB on a 38 GB filesystem, while the live DB was about 2.3 GB. Re-check disk immediately before any backup/deploy operation.
- A key source file had a modification time after the trader process start, so the on-disk working tree could not be assumed identical to loaded runtime code.
- No automatic real redemption caller and no automatic `EMERGENCY_060` trigger were found in current source; both capabilities must be treated as absent until code proves otherwise.

