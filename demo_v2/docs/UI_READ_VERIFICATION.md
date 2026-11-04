# DEMO VM history-read repair — 2026-11-04

Historical evidence only. Recheck the guest and current release for future work.
This is not a completed authenticated browser acceptance check.

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
guest `current` points to the release above; app PID 10733, NRestarts=0 at the
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

## Remaining browser acceptance

The visible in-app browser is open at `http://127.0.0.1:8080/demo/` and awaits the
operator's normal login. After login, inspect the actual overview, pagination,
filters, trade details, rules/editor (cancel edits), health/resource/stream panels
and maintenance/notification screens. Do not send emails, save test rules or
reset real history merely for acceptance. Until those checks run, do not claim
the entire authenticated UI or all 21 market paths were verified.
