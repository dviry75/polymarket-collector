# DEMO VM history repair and authenticated UI review — 2026-11-04

Historical evidence only. Recheck the guest and current release for future work.
Normal-login browser checks are recorded below, with their operational limits.

## Fault and repair

After normal login, simultaneous rules/trades/KPI requests loaded all 12,178
positions and all 6,027 historical market instances. Wide JSON evidence was
materialized before pagination. Concurrent reads drove the app cgroup above its
384 MiB soft limit, causing reclaim stalls rather than an OOM. Browser requests
aborted at 12 seconds; raising the login headroom alone did not resolve this.

Release `7925d704d1da5ab70bacd59906f0a25427d05eaa` now selects/counts a shared
cohort in SQLite, pages narrow identifiers before loading evidence, and streams
exact Decimal KPI/rule totals. Financial writes, fees, DB schema, serial allocation,
authentication, resource limits and isolation gates are unchanged.

## Deployment and preservation

Two ordinary deployment attempts correctly refused the unresponsive old service
before creating/promoting a release. A restart briefly recovered liveness, but
ongoing browser polling immediately overloaded the old code again. A short guest
nginx maintenance window prevented new requests while the existing checksummed
updater ran. Ingress was restored as the new release was promoted; the wrapper
also restored nginx on failure. No proxy configuration was changed.

The updater passed disposable startup as the real service user, decision-child
startup, dependency closure, schema identity, online/stopped backups, frontend
hashes, preserved record IDs and private settings/serial comparisons. Actual
guest `current` pointed to the release above; app PID 10733, NRestarts=0 at that
post-deploy check. The host writer remains disabled/inactive (PID 0); QEMU stayed
on PID 1295590 with no restarts. No LIVE, credentials, public DNS/router, schema
or financial-history edits were made. Do not restart the retired host writer.

## Validation

- All 435 backend tests passed with temporary DBs/fake dependencies.
- All 24 frontend unit/preview tests and JavaScript/TypeScript checks passed.
- New regression loads >15 MiB fake history with a <3 MiB Python allocation
  budget for page/summary reads, preserves serials and exact totals, and covers
  sub-float-precision movement bounds plus timezone-aware date boundaries.
- Existing API tests cover authenticated CRUD, CAS/version history, CSRF,
  revocation, paging, filters, fees, detail and reset against disposable data.
- Guest liveness: 200 SIMULATION, trading_enabled=false, 0.016 seconds.
- Guest readiness: 200 DEGRADED, entries_allowed=true, 0.004 seconds. This does
  not establish entry eligibility/execution in every one of the 21 definitions.
- Post-deploy memory.current: 329,752,576 bytes; memory PSI some/full and high/max/
  OOM events all zero at that check. MemoryHigh=384M, MemoryMax=512M retained.
- Existing authenticated browser polling in safe nginx logs returned 200:
  trades 0.249–0.616s, rules 0.900–1.615s, KPI 0.955–1.789s; health/markets under
  0.1s in the sampled cycles. These are observations, not latency guarantees.
- Host relay frontend 200; unauthenticated rules/trades/KPI/markets/health/
  notifications all 401 with the expected VM routing marker. No cookies or
  operator credentials were read and no session was forged.

Direct read-model measurements as the guest service user against existing state
(no app startup/writer/session creation, no financial payload printed):

| Read | Seconds | Python peak MiB | Returned / total |
| --- | ---: | ---: | --- |
| Trade page, limit 10 | 0.113 | 0.187 | 10 / 12,178 |
| Trade page, limit 200, offset 10,000 | 0.246 | 4.552 | 200 / 12,178 |
| Full-cohort KPI | 0.562 | 0.009 | 12,178 |
| Lifetime rule summary | 0.307 | 0.007 | one rule |

Python allocation peaks exclude SQLite/native memory; actual cgroup pressure
and HTTP latency were checked separately. The service remained active outside
the bounded deployment/recovery restarts; no synthetic real rule/trade or test
email was created. Destructive actions were exercised only with temporary data.

## Normal-login browser review

The operator logged in through the visible in-app browser at
`http://127.0.0.1:8080/demo/`. No password, cookie or session value was read.
The authenticated session survived the subsequent code-only deployment/reload.

Verified through normal UI controls:

- Overview/KPI/trade table loaded, next-page navigation changed stable serials
  from the first page to the next without renumbering historical records.
- Asset filtering (ETH), status filtering (OPEN with a correct empty state),
  inclusive single Jerusalem day, movement bounds and combined timeframe/side
  filtering (1h/DOWN). Trade and KPI counts agreed in the observed scopes.
- Page size 100 returned exactly 100 rows; the preference was restored to 10.
- Trade #12189 details matched the table, with gross/net shares, separate buy/
  sell fees, total fees, actual entry/exit and missing research values explicit.
- All seven assets appeared in each of the 5m/15m/1h tabs. All 21 discovery
  component states were READY. A current 5m slug advanced during the review.
- Existing rule editor loaded all 21 selected markets and its saved thresholds,
  size, quota, schedule and conditions. A window/condition was added only to
  the draft, then discarded through the unsaved-changes dialog. No saved rule
  was altered or synthetic real-state position created by the review.
- Maintenance and notification settings/delivery-status controls loaded.
  Preferences were not saved, no mail was sent and no actual reset was run.
  Write/reset/revocation behavior is exercised against disposable databases.
- Health drawer displayed guest CPU/RAM/storage/network cards, decision-owner
  liveness, queue metrics and scoped component/freshness reasons.

Some checks ran at the narrow default in-app viewport; no viewport override was
left behind. A local screenshot was saved outside Git at
`work/demo-ui-review-20261104/health.jpg` in the chat workspace.

## Authenticated-review follow-ups

### Authenticated clock synchronization

The guest chrony service had never synchronized: all Canonical NTS sources had
zero reach, Stratum 0 and `Leap status: Not synchronised`. The host clock was
synchronized. The dedicated host policy allowed UDP 123 but lacked NTS-KE TCP
4460, which Ubuntu chrony needs for authenticated key establishment.

Host policy repair commit `8f199a4` permits public TCP 4460 only after the same
private/home-address guards. Its checksummed narrow repair validates installed
module/initializer hashes and COMPLETE target, preserves the old renderer for
rollback and applies the dedicated nft table atomically. Ubuntu administrator
authentication was obtained through polkit. No inbound access, private/LAN
access, router change or VM/app restart was introduced. Guest chrony alone was
restarted to acquire authenticated time immediately; no time was manually set.

22 real packet checks and three network-initializer checks passed in disposable
namespaces. Actual guest-root containment checks then passed again for router,
host LAN/bridge/API denial and public Gamma/Binance DNS/TLS. Canonical NTS TLS
with certificate verification negotiated `ntske/1` in 0.216s. Chrony subsequently
reported `Leap status: Normal`, Stratum 3 and `NTPSynchronized=yes`.

An independent 15-second public 5m observation from the guest received 26,208
frames/26,220 timestamp samples with zero future timestamps, ages 34.7–305ms.
It used public subscriptions only, with no app startup/session/financial writes.

Reference: [Ubuntu chrony/NTS setup](https://ubuntu.com/server/docs/how-to/networking/chrony-client/).

### Bound runtime market recovery and retention

The authenticated review exposed another full-history allocation: runtime startup
loaded every historical market and retained its parsed metadata indefinitely.
At measurement, the database held 6,216 instances but only 42 were needed for
unexpired/held runtime recovery. Release
`fb1e0158d81323b6d548dfc5839150ea578520d2` now loads only that runtime set and
evicts expired volatile metadata after current/subscription/held/reference-worker
ownership ends. SQLite history and detail endpoints retain all research records.

All 440 backend tests passed, including expired held-position recovery, no history
deletion and protecting active subscription/reference work during rollover. The
ordinary VM controller/updater deployed the commit, verified disposable startup,
backups, served artifacts and record/settings/serial preservation. App PID 12930
with NRestarts=0; host writer still disabled/inactive; QEMU PID unchanged.

The first post-deploy cgroup sample was 156,622,848 bytes (about 149 MiB), compared
with approximately 400 MiB beforehand. A later sample after normal polling was
181,645,312 bytes (about 173 MiB). Memory PSI and high/max/OOM events remained
zero. MemoryHigh=384M/MemoryMax=512M are unchanged. UI RSS includes shared pages
and is a different measure. Example UI owner RPC fell to 2.8ms, loop lag 0.6ms.

All 80 recently sampled authenticated GET requests returned 200, none over 10s.
Sample maxima: health 0.071s, markets 0.088s, trades 0.475s, rules 4.613s and KPI
6.508s. These include concurrent browser polling and are not latency guarantees.

The final read-query refinement avoids historical market joins for filters that
do not need market identity and loads page display identity only after paging.
A read-only process comparison measured KPI 0.439→0.260s and page 0.121→0.072s;
the actual database continued receiving legitimate simulated trades meanwhile.

## Limits and current market observations

The inspected UI/read paths passed; this is not proof of executing a trade in
every definition, uninterrupted quote availability or sustained load capacity.
READY and DEGRADED samples were both observed. Some market sockets still showed
transient `ConnectionClosedError`/resynchronization or stale-book conditions;
affected markets remain gated until fresh books recover. Database/auth/owner,
discovery and research providers were healthy in the later health sample. No
freshness, queue-age, exact-trigger or isolation guard was weakened to make the
display green. All destructive/write-path tests used temporary/fake dependencies.
