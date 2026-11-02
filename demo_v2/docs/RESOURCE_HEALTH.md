# DEMO resource telemetry

Authenticated `GET /demo/api/v1/health` now includes `resources`. Liveness remains
minimal and unauthenticated; no host counters are exposed there. No new dependency,
key, permission, port or database schema is needed.

A separate background task samples every two seconds via a thread. Requests read
an atomic cached snapshot; observations/decision FIFO and trading eligibility
never wait on telemetry or depend on it. Failures are partial/unavailable data,
never zero consumption or a new trading blocker. Values expire in the UI after
15 seconds or immediately when health fetch fails. The existing ten-second API
poll updates the open health drawer without stealing focus or scroll.

- CPU: DEMO backend process CPU-time delta / monotonic elapsed time. 100% is one
  logical core; multithreaded values may exceed 100%. Host CPU busy fraction uses
  `/proc/stat` (guest time is not counted twice). The first sample has no rate.
- RAM: current backend RSS and virtual memory from `/proc/self/statm`; total/used/
  available host RAM and swap from `/proc/meminfo`. Available RAM excludes
  reclaimable cache from the apparent usage. Backend RSS excludes nginx/browser.
- Disk: capacity/used/available on the DEMO state filesystem, plus file sizes of
  `demo_v2.sqlite3`, its WAL and SHM. Files are stat'ed, not opened as databases;
  no recursive directory scans, DB mutations or history cleanup occur.
- Network: received/sent bytes and interval bytes/second summed over host NICs
  excluding loopback from `/proc/net/dev`. Includes other programs and LAN traffic;
  virtual interfaces may double-count traffic. This is neither per-DEMO usage nor
  the household's router bandwidth. Counter resets/interface changes rebaseline
  rates rather than showing negatives. Totals reflect interface counters.

The UI uses four responsive cards in the existing health drawer, with host/backend
scope labels, small utilization bars and local Rubik typography. No external font
request is needed. API values are ordinary numeric telemetry, separate from
Decimal-string financial values. No env, cookies, credentials, hostname, IP/MAC,
process command lines or production trading state is inspected by the sampler.

## Deployment of this change

The previously installed release is `6361ae3b0495787457d77270485d0df295918fba`.
This release also includes the approved empirical flat-fee change already on main.
No schema changes: previous/current migration files are identical. The reviewed
`deploy/update_release.py` accepts a fixed-checksum offline bundle, verifies the
expected previous release, builds a separate production venv, takes online and
quiescent WAL-aware backups, then stops only DEMO and atomically switches `current`.
It verifies liveness, running source path, static artifact hashes and preservation
of existing rule/position/ledger records. Failures after stopping restore the
previous code link/service, without overwriting DB history. It does not change
env, service definitions, nginx, DNS, firewall, tunnels or LIVE.

Ubuntu administrator authentication is required for `/opt`, private backup/state
access and systemd. Source approval does not itself grant Linux root privileges.
The prepared artifact has a one-command launcher; exact SHA and runtime outcome
are reported in the task delivery after the operator authentication step.


## Validation of the clean release candidate

- Backend: 286 passed, 0 failed/skipped (includes the unchanged fee/trading/isolation
  suite, five resource regressions and four pure bundle/promotion tests).
- Frontend: 18 unit/contract tests passed; build, TypeScript and JS syntax passed.
- Chrome: all four authenticated E2E workflows passed, including the simulated
  fee-enabled entry flow, resource cards, live updates while open, stale/error
  handling, mobile fit and actual local Rubik font loading. Desktop/mobile
  screenshots were inspected.
- The standalone sampler was also checked against real host counters using a
  temporary offline app; no installed DB/env was read by that check.
- Existing production dependencies stay pinned; a complete wheel set is prepared
  for offline release installation. Migration source matches the installed release.
- The original upstream OFL license is preserved byte-for-byte, including its
  whitespace; its attributes/checksum document that choice.

Uncommitted rounding-forensics work remains outside this release. All Git changes
for this task are under `demo_v2/`.


## Installed verification — 2026-11-03

The operator completed the administrator authentication. Release
`8dcf2a26932de530d39092207a33bf1bc78eb25f` is installed, with the DEMO service
running from the new release since 01:29:06 Asia/Jerusalem, PID 134800 and zero
automatic restarts at verification. The deployment log confirms both verified
backups and preservation of existing rule/position/ledger IDs. No env/unit/proxy/
DNS/firewall/LIVE changes were made.

The existing authenticated session survived. Live UI resource cards update, local
Rubik is served, and Maintenance reports the configured empirical fee 0.068.
Installed resource/engine/runtime hashes match the reviewed source. All 21 market
families are discovered. This proves installation, not sustained trading readiness.

**Operational readiness is NOT_READY, not an all-green release sign-off.**
The authenticated UI reports `BOOKS_STALE_OR_MISSING`; the public market socket
alternates between connected and reconnecting. Some underlying research contexts
also intermittently report unavailable. A separate bounded in-memory public WS
probe (no DB/engine) observed stale/missing HYPE-1h/HYPE-5m books and a conservative
reconnect, followed by fresh snapshots. The installed readiness endpoint also
showed blocked responses and an intermittent 3-second request timeout. These are
recorded issues, not a claim that all runtime instability has one proven cause.
The old exact-fee blocker is absent. Guards were not loosened, no synthetic entry
was injected, and no rules/history were modified to make verification pass.
