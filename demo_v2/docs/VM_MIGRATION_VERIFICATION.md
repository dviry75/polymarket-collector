# Initial VM migration — verified 2026-11-04

Historical evidence only; recheck actual services, target marker and guest release
before future operations. Development remains on the physical host. Follow
`AGENTS.md` and `deploy/vm/README.md` for all future deployments.

## Verified runtime

- Guest: `polymarket-demo-vm`, KVM, Ubuntu kernel `7.0.0-42-generic`.
- Deployed source: `7647781c7eb66fd90f5191f77958bf5b9041060f`.
- Guest app: enabled/active, two child processes including the decision owner,
  no service restarts during migration/post-cutover checks. QEMU also had no
  restarts following the bounded-worker configuration change.
- Physical host app: disabled/inactive, MainPID 0. The persistent writer guard
  prevents accidental starts against its historical DB.
- Root-owned `/etc/polymarket-demo-vm/runtime-target.json`: COMPLETE, VM target,
  host writer disabled. Controller reported `DEMO_VM_MIGRATION_PASS`.
- Existing local URL `http://127.0.0.1:8080/demo/`: HTTP 200 with VM relay marker;
  unauthenticated `/demo/api/v1/health`: HTTP 401 with the same marker.
- Guest liveness confirmed SIMULATION with real trading disabled. Guest package
  audit was clean. Private env, DB and active application reside in the guest;
  retained host files are private historical/recovery copies, not another writer.

## Preservation and containment

The migration took a fresh quiescent host backup after stopping the old writer,
transferred it and the opaque DEMO config over pinned SSH, and verified existing
rule/position/ledger IDs plus session/auth, notification settings and trade serial
metadata. Values and comparison fingerprints were not printed. Final checks
repeated guest readiness, release/PID identity and preservation after proxy cutover.

Guest-root connection probes confirmed blocked access to the home router, host
LAN, host bridge non-DNS canaries and host API. Public Gamma/Binance DNS/TLS probes
passed. The firewall also passed the 16 disposable-namespace packet tests and
three initializer checks. No shared host directories, LIVE runtime changes,
router/public DNS/tunnel changes or host sudoers exceptions were introduced.

## Validation limits

71 targeted tests passed, including real synthetic nginx reload convergence,
private-state preservation, permissions under umask 0077 and app isolation.
Actual guest app-user/temp-DB startup, systemd sandbox startup and nginx static
file access also passed before cutover. Relay tests used a temporary fake API;
real production readiness/auth-denial checks passed during migration afterward.

Post-cutover readiness snapshot: `DEGRADED`, `entries_allowed=true`. Critical
global prerequisites were ready; market/rule eligibility remains independently
gated. This evidence does not claim fresh books or successful execution in all
21 definitions. Detailed market diagnosis and browser login require normal DEMO
authentication; no session was forged or password read to obtain it.
