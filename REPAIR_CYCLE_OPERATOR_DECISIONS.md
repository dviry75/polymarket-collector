# Repair Cycle — Operator Decisions

## PROD rollout approval

Engineering verdict: **READY FOR GATED PROD ROLLOUT**.

Operator approval is required before changing the production checkout, running the schema migration, restarting `polymarket-trader.service`, or enabling entries.

- Candidate branch: `repair/stop-entry-reconciliation-hardening-20260916`
- Validated code SHA: `36fda7fa6d37a5c623f561a7d9346729eccb0346`
- Current production SHA: `93cc771b42094edcd416a61ffe36f418b3d9a702` (unchanged)
- Recommended rollout: backup → deploy exact SHA → migrate while entries remain disabled → restart fail-closed → verify all release gates → one canary entry → continuous monitoring.
- Rollback target: `93cc771b42094edcd416a61ffe36f418b3d9a702`.

Decision: **Approve / Reject gated PROD rollout**.
