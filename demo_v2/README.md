# Polymarket DEMO V2

Isolated context subtree for simulated trading/research inside `polymarket-collector`. This is not the LIVE system. Frontend/UI belongs to a separate external workflow.

## Context structure

```text
demo_v2/
├── AGENTS.md
├── README.md
└── docs/
    ├── SOURCE_OF_TRUTH.md
    ├── PROJECT_CONTEXT.md
    ├── DECISIONS.md
    ├── EXISTING_SYSTEM_REFERENCE.md
    ├── REUSE_GUIDE.md
    ├── API_CONTRACT.md
    ├── IMPLEMENTATION_ROADMAP.md
    └── VALIDATION_MATRIX.md
```

Before each future DEMO V2 task, read `AGENTS.md`, then the eight documents in the order specified there. `docs/SOURCE_OF_TRUTH.md` is authoritative for product behavior. Repository-level `../AGENTS.md` and `../SYSTEM_CONTEXT.md` remain applicable LIVE safety references; they are unchanged.

Priority when product documents disagree: explicit current user instruction > SOURCE_OF_TRUTH.md > DECISIONS.md > current implementation/tests > historical plans.

## Package provenance and adaptations

The eight context documents were extracted from the tracked `polymarket-demo-v2-context.zip`. The archive is retained unchanged. Its bundled installation script was not run: it targets `docs/demo-v2/` and appends to root `AGENTS.md`, contrary to the requested isolation.

Relevant safety/reuse/UI guidance from `AGENTS_DEMO_V2_APPEND.md` was incorporated into local `AGENTS.md`. The package README was adapted for this subtree. The roadmap's Phase 0 reading paths were updated to the local layout; its phase descriptions are future plans, not authorization to execute them.

Authentication reuse in the source documents is a future design requirement only. It does not authorize reading/copying credentials, modifying LIVE config, or sharing LIVE sessions/state in this bootstrap.

Only documentation is installed. No Phase 0, audit, implementation, DB/config/service change, or deployment has been started.
