# Onboarding Brief — Polymarket BTC LIVE Trading System

*Written 2026-09-13 for a fresh Claude instance picking up this codebase.*

## 1. WHAT IT DOES

This is an automated day-trading bot that trades **real money** on Polymarket's
"Bitcoin Up or Down" 5-minute binary markets. Every 5 minutes Polymarket lists
a new BTC Up/Down event; each event has a YES ("Up") and NO ("Down") token
that settles at $1 or $0 at expiry.

The strategy is narrow and mechanical, not predictive: it watches the book in
the final 120 seconds before an event settles, and **enters when the best ask
hits exactly $0.74** on one side (a momentum/near-certainty signal near
expiry). It then manages the position with a take-profit GTC sell at $0.96
and a stop-loss that latches once the best bid drops to $0.66 or below. The
owner (the human operator, "dvir") is the sole beneficiary — this is a
single-account personal trading system, not a multi-tenant product.

A successful run looks like: the system enters at $0.74, either the market
resolves in its favor (near $1, take-profit or resolution redeems it) or the
stop-loss exits cleanly near $0.66 with a small, bounded loss — and every
step (entry, fill, exit, resolution, redemption) is durably recorded so the
operator can reconstruct exactly what happened even if the process crashes
mid-trade. The business risk being managed is **not just "make money"** but
"never lose track of a position or send an order the system can't account
for" — most of the code exists to make failure modes fail *safely closed*
(pause and hold) rather than *silently* (double-enter, orphan an order,
misreport a position).

## 2. ARCHITECTURE

The system runs as several long-lived processes on one Linux VM
(`/opt/polymarket-btc-live`), coordinating through one SQLite database and a
Unix-domain socket.

- **Trader core** (`polymarket-trader.service`, port 8002, internal only) —
  owns everything stateful: market discovery, WebSocket order books, the
  strategy state machine, order submission, reconciliation, and pause/kill
  logic. This is the only process allowed to talk to the authenticated
  Polymarket CLOB API.
- **Dashboard** (`polymarket-dashboard.service`, port 8001) — a read-only web
  UI/API. It opens the SQLite DB read-only and asks the trader for live
  status over a Unix socket (`/run/polymarket/trader.sock`, an IPC protocol
  with a `STATUS` command). It never trades. nginx terminates HTTPS/auth in
  front of it.
- **Market resolution service** — a separate loop polling Polymarket's public
  REST API to determine event winners/losers and write resolution rows.
- **Hourly report timer/service** — summarizes recent activity into report
  rows and emails the operator (SMTP).
- **Backup and archive timers** — daily SQLite backup, and a daily job that
  uploads old rows to Google Cloud Storage and prunes them locally.
- **External fills / trade-export jobs** — reconcile/export trade history
  against Polymarket's own fill records, feeding a separate dashboard
  cash-flow view.

External dependencies: **Polymarket Gamma API** (market discovery, public
REST), **Polymarket CLOB** (authenticated REST + SDK for signed orders,
balances, positions), **Polymarket Market/User WebSockets** (live order books
and account fill events), an Ethereum-compatible **wallet/private key** (order
signing — see `PRIVATE_KEY_SECURE_SETUP_GUIDE_HE.md`), **Coinbase's public
candles API** (BTC price context, unrelated to order placement), **SQLite**
(the single source of durable truth, WAL mode), **Google Cloud Storage**
(archive), and **SMTP** (report emails). There is no external job queue —
concurrency is asyncio tasks within the trader process, guarded by DB rows and
in-process locks.

Data flow, roughly:
```
Public Gamma/CLOB discovery + Market WS
  → in-memory order books (generation-aware, staleness-checked)
  → strategy runtime (entry/exit decisions)
  → durable SQLite intent BEFORE any network order
  → authenticated CLOB adapter (signs + submits)
  → User WS + REST reconciliation confirm fills
  → durable position/fill state
  → dashboard reads DB read-only + trader IPC STATUS for live health
```

## 3. KEY ABSTRACTIONS

- **Intent** (`live_strategy_intents`) — a durable record of "we are about to
  do X" (ENTRY buy, TP sell, STOP sell) written to SQLite *before* any network
  call. This is what lets the system recover from a crash mid-order without
  double-submitting.
- **Position** (`live_strategy_positions`) — the system's durable belief about
  what it currently holds for one event/token: state (`OPEN`, `TP_OPEN`,
  `EXITING`, `EXIT_RECONCILIATION_REQUIRED`, `DUST`, `REDEEM_PENDING`, …),
  remaining/sellable shares, which intents are attached to it.
- **Reconciliation** — a background worker that periodically (and after key
  events) compares local durable state against Polymarket's authenticated
  truth (balances, open orders, trades, token positions) and either heals
  small propagation-lag gaps automatically or flags a "gap"/contradiction that
  blocks trading until a human looks at it.
- **Pause / generation** — a compare-and-swap versioned pause record in
  `live_system_state` (owner, cause, generation number). Recovery from a pause
  must present a generation newer than the one that set it, preventing a stale
  process from silently un-pausing a live safety hold.
- **Entry release gate / `EntryReleaseEvaluator`** — the single evaluator that
  decides whether new entries are allowed, folding together kill switch,
  canary, WebSocket health, strategy readiness, reconciliation readiness,
  heartbeat, geography, and risk limits. "Not paused" does not mean "entry
  allowed" — the system can be `GATED` for other reasons.
- **Canary** — a one-shot safety valve: when continuous trading is off, the
  very first entry reservation consumes/disarms the canary and then blocks
  further entries until an operator re-arms it.
- **Event lock** — a per-5-minute-event lock that prevents two entries (or an
  entry and a duplicate exit) racing on the same market event.
- **Order attempt journal** (`live_order_attempts`) — a forensic
  STARTED/RESULT log around every real order submission, independent of the
  intent/position tables, used to reconstruct exactly what was sent and what
  came back even under ambiguous/unknown API responses.
- **Provenance labels** (`PENDING`/`DERIVED`/`OBSERVED`/`VERIFIED`) — tags on
  dashboard-facing data describing how trustworthy/fresh a displayed value is,
  because much of what the dashboard shows is a projection, not live truth.
- **Hot snapshot** — an in-RAM cache of book/strategy state refreshed from
  SQLite roughly every 250ms so the entry check can be fast; if it's stale by
  more than ~1 second, entry fails closed rather than trading on stale data.

## 4. MAP

Git root: `/opt/polymarket-btc-live/repo` (not a normal user checkout — it's
the live deployment; systemd units run directly from this working tree).

```
repo/
├── SYSTEM_CONTEXT.md       — authoritative architecture reference (read first)
├── AGENTS.md               — non-negotiable safety rules for anyone/anything editing this
├── POLYMARKET_LIVE_*_HE.md — Hebrew design/runbook/stability docs
├── polymarket-btc-local/   — older/legacy local collector, NOT production
└── polymarket-collector/   — the actual production application
    ├── trader_app.py       — production trading-core FastAPI entrypoint (port 8002)
    ├── dashboard_app.py    — read-only dashboard entrypoint (port 8001)
    ├── live_app.py         — legacy monolithic entrypoint, service disabled, don't use
    ├── app.py              — older demo collector, unrelated to live trading
    ├── live/               — all core trading logic (see below)
    ├── scripts/            — one-shot/cron-style operational scripts (resolution, reporting, archive, backfills)
    ├── tests/              — 46 test files, temp-DB + mock-adapter based
    ├── deploy/             — example systemd unit / env files (installed /etc units are authoritative, not these)
    └── tools/              — misc dev tooling
```

Most important files:

1. [`SYSTEM_CONTEXT.md`](SYSTEM_CONTEXT.md) — the map of the whole system,
   sources of truth, trade lifecycle, safe read-only diagnostic commands. Read
   this before touching anything.
2. [`AGENTS.md`](AGENTS.md) — the rules of engagement (read-only-first,
   never bypass safety, never touch secrets/DB directly).
3. [`polymarket-collector/live/strategy.py`](polymarket-collector/live/strategy.py) —
   the entry/exit policy decision logic (the $0.74/$0.96/$0.66 rules live
   here conceptually).
4. [`polymarket-collector/live/strategy_runtime.py`](polymarket-collector/live/strategy_runtime.py) —
   turns policy decisions into actual actions/state transitions.
5. [`polymarket-collector/live/reconciliation.py`](polymarket-collector/live/reconciliation.py) —
   the local-vs-remote truth reconciler; central to system safety.
6. [`polymarket-collector/live/repository.py`](polymarket-collector/live/repository.py) /
   [`strategy_repository.py`](polymarket-collector/live/strategy_repository.py) —
   the SQLite schema/migrations and durable state access layer.
7. [`polymarket-collector/live/pause_recovery.py`](polymarket-collector/live/pause_recovery.py) /
   [`recovery_policy.py`](polymarket-collector/live/recovery_policy.py) —
   pause/generation/auto-recovery machinery.
8. [`polymarket-collector/live/adapters/polymarket.py`](polymarket-collector/live/adapters/polymarket.py) —
   the real authenticated CLOB adapter (signing, order submission).
9. [`polymarket-collector/live/order_attempts.py`](polymarket-collector/live/order_attempts.py) —
   the forensic order-attempt journal used to debug fill anomalies.
10. [`polymarket-collector/live/config.py`](polymarket-collector/live/config.py) —
    the arming/config model; defines what "fully armed for real trading"
    actually requires.

## 5. HOW IT RUNS

Everything runs via systemd on the production VM — there is no separate
staging environment described in the docs; safety is enforced through
config flags, gates, and tests, not by environment separation.

- `polymarket-trader.service` → `uvicorn trader_app:app --loop uvloop --host 127.0.0.1 --port 8002`, cwd `polymarket-collector`, env from `/etc/polymarket-live/trader.env`.
- `polymarket-dashboard.service` → `uvicorn dashboard_app:app --host 127.0.0.1 --port 8001`, env from `dashboard.env`.
- `polymarket-market-resolution.service` → `python scripts/run_market_resolution.py`.
- `polymarket-hourly-report.timer/.service`, `polymarket-live-backup.timer/.service`, `polymarket-live-archive.timer/.service` — periodic jobs.

Config is entirely env-var driven (see `polymarket-collector/deploy/live.env.example`
and `.env.example`); key flags include `LIVE_KILL_SWITCH`, `LIVE_PAUSE_ENTRIES`,
`LIVE_CANARY_ARMED`, `LIVE_TRADING_ENABLED`, `LIVE_MODULE_ENABLED`,
`LIVE_ORDER_SUBMISSION_ENABLED`, `LIVE_STRATEGY_ENTRY_PRICE` /
`_TAKE_PROFIT_PRICE` / `_STOP_PRICE`, `LIVE_MAX_TRADE_AMOUNT_USD`,
`LIVE_MAX_TRADE_TOKENS`. "Real submission armed" requires several of these
simultaneously true — no single flag turns trading on. **Never print these
env files' contents** — they're the explicit line AGENTS.md draws around
secrets.

Local vs. production is *not* a code branch — it's the `LIVE_ADAPTER`
(real vs. `mock`) and `LIVE_PAPER_TRADING_ENABLED` flags plus which env file
is loaded. Tests always use a temp SQLite DB and the mock adapter
(`live/adapters/mock.py`); running tests never touches the live DB or
Polymarket if invoked as documented:
```bash
cd /opt/polymarket-btc-live/repo/polymarket-collector
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests/<file>.py
```
Never source `/etc/polymarket-live/trader.env` before running tests, and
never run `scripts/*` as if they were read-only tests — several of them
(migration, reporting, archive, resolution, backup/restore) write real state
or call external services.

## 6. CONVENTIONS

- **Read-only-first, always.** AGENTS.md mandates checking live systemd
  state, IPC `STATUS`, and DB state via documented read-only queries *before*
  proposing any change — this is enforced culturally/by instruction, not by
  a technical sandbox.
- **Fail-closed everywhere.** Anywhere the code is unsure (stale book, unknown
  order result, ambiguous cancel), the convention is to pause/block rather
  than guess. Don't "fix" a pause by loosening a check — SYSTEM_CONTEXT.md and
  AGENTS.md both explicitly warn against this.
- **Durable-write-before-network-call.** Every real order is preceded by a
  DB-durable intent row; this ordering is deliberate and load-bearing for
  crash recovery — never reorder it "for simplicity."
- **No conventional migrations directory.** Schema changes are additive
  Python methods (`LiveRepository.migrate`, `StrategyRepository.migrate`,
  `migrate_dashboard_schema`) rather than a migrations framework.
- **Hebrew-language incident/design docs.** Many `*_HE.md` files (forensics
  reports, stability runbooks, product plans) are written in Hebrew — this is
  the operator's working language for narrative docs; code/comments are
  English.
- **Numerous stray backup files** (`repository.py.bak.20260807_152402`,
  `router.py.backup-before-continuous-resume-20260808-205727`) sit alongside
  live modules — these are intentional manual snapshots taken before risky
  live edits, not build artifacts; `.gitignore` excludes `*.bak*`/`*.backup*`
  patterns so they don't pollute git history.
- **The dashboard is a separate mental model from the trader.** It's not just
  "the UI layer" — it deliberately runs as a distinct, credential-free,
  read-only process so a dashboard bug or exposure can never let someone
  trade. Don't casually merge dashboard and trader code paths.
- **A companion repo exists** at `github.com/dviry75/polymarket-dashboard`
  (checked out locally at `/home/dvir/polymarket-dashboard-preview`, branch
  `feature/complete-live-dashboard-real-data-20260812`) — this appears to be
  where dashboard frontend work is iterated/previewed before being folded
  into `polymarket-collector/dashboard_app.py` and its static assets; treat it
  as a separate, related project, not part of this repo.

## 7. GLOSSARY

- **TP** — take-profit (GTC sell reserved once a position is sellable, at $0.96).
- **STOP / latched exit** — the stop-loss path, latched one-way once best bid ≤ $0.66; "latched" means once triggered it commits to exiting rather than flip-flopping.
- **FAK** — Fill-And-Kill order (used for both entry BUYs and STOP SELLs): fill what you can immediately, cancel the rest.
- **GTC** — Good-Til-Cancelled (used for the TP sell).
- **DUST** — a position reduced to a remaining share count too small to economically exit further.
- **CLOB** — Polymarket's Central Limit Order Book API/SDK (authenticated trading surface).
- **Gamma API** — Polymarket's public market-metadata/discovery REST API.
- **Canary** — the one-shot arm/disarm safety valve on the first live entry (see §3).
- **IPC `STATUS`** — the one supported read command over the trader's Unix socket, used by the dashboard and by operators for live health.
- **Reconciliation "gap"** — a detected mismatch between local durable belief and remote Polymarket truth; blocks readiness until resolved.
- **A1 / A1-lean** — internal nickname (from memory/commit history) for the "latched SELL off the shared event lock" isolation fix (commit `d1df845`); "A1" appears to be an internal option/approach label from prior design discussion, not a term defined in the docs read so far — flagging this as inferred, not confirmed.
- **Entry-liquidity instrumentation** — the measurement (not gating, per commit `350e8a5`) of bid-side depth/simulated exit-ability at entry time.
- **HE suffix** (`*_HE.md`) — Hebrew-language document.
- **Emergency close / EMERGENCY_060** — an operator-triggered exit path at a $0.60 threshold; SYSTEM_CONTEXT.md explicitly notes the threshold is configured but no automatic trigger call was found in source — don't assume it fires on its own.

## 8. CURRENT STATE

As of this writing (repo `HEAD` = `93cc771 "Add verified recovery for
redeemed unknown entries"`, all three core services `active`):

**Finished/stable, per recent commit history:**
- Entry-liquidity instrumentation (measurement-only, no gate) — `350e8a5`.
- Exit-latency hardening (critical-frame coalescing so a volatile book can't
  bury a STOP SELL, audit-log indexing) — `ac95c4e` and surrounding commits.
- Exit forensics (root-cause capture + classifier for stop-loss exits,
  auto-finalize/auto-classify) — `bbeb61f` through `12187f0`.
- A1-lean latched-exit isolation (SELL off the shared event lock, DUST
  exclusion from the latched tier) — `3a70efe`, `d1df845`.
- Entry position size raised from 5→8 shares/$8 — `90eeba6` (2026-09-10; per
  memory, daily-loss cap intentionally left at $10, i.e. unchanged).
- Hardened final entry order submission checks — `3f9eb48`.
- Most recent commit adds verified recovery for a redeemed-but-unknown-entry
  edge case — `93cc771` — I have not read this commit's diff in detail; treat
  it as recent and unreviewed by me.

**Known live/operational issues** (from persistent memory, not verified fresh
in this session — re-check current `STATUS`/pause state before relying on
these):
- A chronic `ENTRY_FILL_OUTSIDE_POLICY` pattern (FAK orders filling against a
  stale book) has recurred since around Sep 1; forensics exist
  (`FORENSICS_ENTRY_FILL_OUTSIDE_POLICY_e98ca6_HE.md`,
  `FORENSICS_0.04_signal_to_submit_4.2s_HE.md` in `/home/dvir`) but this looks
  like a standing/monitored risk rather than a fixed bug.
- A dashboard "trading off" banner was previously traced to a false-alarm
  0.75s IPC timeout, with the real fix deferred to a larger planned dashboard
  overhaul — cosmetic, not a trading-safety issue.
- A liquidity-instrumentation branch (`entry-liquidity-instrumentation`) and a
  dashboard external-fills feature branch were, as of early September, tested
  but not yet fully committed/rolled out — worth checking `git log`/branches
  again before assuming they've landed (the current `main` history above
  suggests the liquidity work *did* land as `350e8a5`).

**Where to be careful:**
- Anything touching `live/pause_recovery.py`, `live/recovery_policy.py`,
  `live/reconciliation.py`, or the entry release gate — these encode a lot of
  hard-won safety logic against real financial loss; AGENTS.md's instruction
  to trace root cause before patching applies most strongly here.
- The legacy `live_app.py` / `live_orders` / `live_positions` path — explicitly
  documented as superseded by `live_strategy_*` tables and the trader/dashboard
  split; don't extend it.
- `polymarket-btc-local/` — a separate, older collector kept in the same repo;
  don't confuse it with the production `polymarket-collector/` app.
- Never treat this session's read of "current state" as current — SYSTEM_CONTEXT.md
  and AGENTS.md are emphatic that pause/kill/reconciliation/position state must
  be re-read live before any action, every time.
