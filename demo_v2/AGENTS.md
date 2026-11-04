# Polymarket DEMO V2 — Local Agent Rules

This directory belongs exclusively to **Polymarket DEMO V2**. This is **not LIVE**.
DEMO V2 consumes real market data but performs **simulated trading only**.
These rules apply to this subtree; repository-level safety rules remain applicable.

## Required reading before every DEMO V2 task

Read these files in order, relative to `demo_v2/`:

1. `AGENTS.md`
2. `docs/PROJECT_CONTEXT.md`
3. `docs/SOURCE_OF_TRUTH.md`
4. `docs/DECISIONS.md`
5. `docs/EXISTING_SYSTEM_REFERENCE.md`
6. `docs/REUSE_GUIDE.md`
7. `docs/API_CONTRACT.md`
8. `docs/IMPLEMENTATION_ROADMAP.md`
9. `docs/VALIDATION_MATRIX.md`

`docs/SOURCE_OF_TRUTH.md` is the source of truth for product behavior.
Priority for product conflicts: explicit current user instruction > SOURCE_OF_TRUTH.md > DECISIONS.md > current implementation/tests > historical plans.
Read applicable repository-level instructions as well; root LIVE documents are references and must not be replaced by this context.

## Isolation and forbidden operations

- No real trading, private keys, signing, authenticated order submission, real cancellation, redemption, emergency close, or other real execution.
- Do not use real-trading adapters or authenticated trading write methods in DEMO execution.
- Never modify LIVE runtime state, financial state, positions, intents, orders, fills, or LIVE DB.
- Any future DEMO app entrypoint, DB, config/env, service, logs, runtime and health state must be separate from LIVE.
- Do not load production trading env files or expose secrets. Tests must use isolated temporary/fake dependencies.
- Do not edit root `AGENTS.md`, `SYSTEM_CONTEXT.md`, or existing LIVE documents as part of DEMO context work.
- Deployment, migrations, service operations, DB/config/env changes and implementation require a future task that explicitly authorizes their scope.

## Existing-Code-First and code boundaries

- Reading existing code outside `demo_v2/` is allowed for audit, reference and reuse.
- Existing-Code-First: inspect working infrastructure and tests before writing any replacement. Classify reuse/adaptation/reference and explain the decision before broad implementation.
- Reuse safe tested public market-data, order-book, resolution, accounting and recovery primitives when they match the product spec and do not couple DEMO to real execution.
- By default all new DEMO V2 code belongs under `demo_v2/`.
- Shared-code or LIVE-code changes are allowed only when a future task explicitly requires them and only after impact analysis.
- Legacy DEMO behavior is reference only where it conflicts with SOURCE_OF_TRUTH.md.

## Frontend ownership

Frontend/UI is developed elsewhere by a separate agent and is entirely outside DEMO V2 backend work.
Do not build pages, React components, CSS, UI mockups or visual designs.
Future backend work should expose clean documented APIs/data contracts for the external UI workflow.

## Current authorization boundary

The bootstrap-only restriction is historical. Implementation and deployment have
subsequently been explicitly authorized. The 2026-11-03 task authorizes migrating
the existing DEMO runtime/state to an isolated KVM guest, while keeping development
on this physical Ubuntu host. Future tasks must still authorize their operational
scope; do not infer authority to change LIVE, router, public exposure or credentials.

## Runtime target and mandatory deployment workflow

Read `deploy/vm/README.md` before ANY deployment, service operation or migration.
The source/development checkout stays on the physical host; DEMO runtime, frontend,
private DEMO config, DB, accounting state and app service belong in the dedicated
`polymarket-demo-vm` KVM guest once cutover is verified. Never mount/share the source
checkout, host home, host LIVE/config directories, Docker socket or management keys
inside the guest. Host nginx is then only a loopback ingress relay on port 8080.

Do not assume migration happened because these files exist. Verify actual host
`polymarket-demo-vm.service`, pinned SSH guest hostname/virtualization, active release,
and the root-owned `/etc/polymarket-demo-vm/migration-state.json` phase. `COMPLETE`
plus actual service/network checks establish cutover. Until then the existing host
DEMO service remains authoritative. Never start both writers or mask a failed check.

Initial cutover was verified on 2026-11-04 (see `docs/VM_MIGRATION_VERIFICATION.md`).
That record is historical; it never replaces current marker/service/guest checks.

After cutover, use `deploy/vm/controller.py deploy` from the development checkout:
commit/test/push the intended source, export that exact commit, build frontend,
upload source/frontend/pinned wheels using pinned SSH, then invoke the existing
checksummed updater INSIDE THE VM. The guest updater backs up the guest DB, checks
schema compatibility, promotes immutable code, preserves records/settings/serials,
and rolls code back on failure. Do not run the host's old deployment wrappers or
updater as a workaround, and do not edit installed guest source in place.

Before cutover, verify the exported release with disposable state as the actual
`polymarket-demo-v2` service user from its app working directory, and static-file
access as `www-data`. A root-only startup check misses permission failures. Public
code/venv/frontend need read/traverse access; private env/DB/backups remain private.

Management uses the existing operator-only SSH key under
`/home/dvir/Documents/Codex/2026-10-29/new-chat/work/demo-vm-private/` and the pinned
guest host key under `/etc/polymarket-demo-vm/guest-host-key.pub`. Never print/read key
contents, disable host-key verification, add host sudoers exceptions or copy actual
env/DB files into Git, ordinary release bundles or tests. Initial state transfer is
an explicit exception for opaque DEMO-only files over pinned SSH to private guest
paths; no private values or fingerprints may appear in the operator report.

Host `/etc/polymarket-demo-vm/writer-in-vm` and the DEMO unit condition deliberately
prevent the retired host app from starting. Preserve them. A reverse migration must
first stop/disable the guest writer and recover its latest committed database; the
old host DB is a historical backup, not current truth. If the guest is unreachable,
leave the host writer stopped and report the blocker. Never restore a stale DB just
to bring the old service up. Service/resource telemetry inside the VM describes
the guest, not total resource consumption on the physical host.
