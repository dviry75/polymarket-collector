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

This bootstrap installs context documents only. Do not start Phase 0, an audit, backend/frontend implementation, refactoring, migrations, deployment, or service/DB/config/env operations without a new task.
