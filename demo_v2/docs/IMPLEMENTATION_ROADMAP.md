# DEMO V2 — Implementation Roadmap

Frontend/UI is out of scope.

Phase 0 — Read-only audit: read demo_v2/AGENTS.md and its ordered context documents, plus repository-level AGENTS.md and SYSTEM_CONTEXT.md as applicable safety references; inspect Git/current code; identify existing Paper Trading, DEMO, WS, order book, resolution, auth, fee and health code; produce reuse map.

Phase 1 — Architecture/isolation plan: DEMO package boundary, dedicated app entrypoint, DB/config/service/port/API, shared vs adapted modules, explicit forbidden dependency on real order submission. Generic multi-timeframe architecture.

Phase 2 — Persistence/domain: rules, versions/snapshots, schedules, internal windows, underlying filters, positions/trades, wallet ledger, fees, underlying context, useful health incidents. Temp DEMO DB migrations only.

Phase 3 — Market data/lifecycle: 21 market catalog, active recurring discovery, token mapping, subscribe/unsubscribe, rollover, bid/ask state, freshness, resolution, dynamic listening. No trade backfill.

Phase 4 — Underlying asset context: appropriate price sources, market-open/entry/exit values, provenance, movement calculations, rule filters, fail-closed if required data stale.

Phase 5 — Rule engine: exact entry, side-agnostic triggers, re-arm, frequency, concurrent positions, Asia/Jerusalem schedules, multiple internal windows, underlying filters, version snapshots, independent rules.

Phase 6 — Exits/resolution: best-bid TP/SL, observed jump price, independent position management, resolution, positions survive schedule/rule disable/delete.

Phase 7 — Accounting: shares, entry notional, buy/sell fees, net shares/proceeds/P&L, global/per-rule wallets, scoped aggregates. Validate against trustworthy existing trade examples without mutating/exposing LIVE.

Phase 8 — Health/self-healing: READY/DEGRADED/NOT_READY, feed freshness, fee readiness, discovery/resolution, DB/worker health, reconnect/re-subscribe/re-resolve, stable auto return to READY, fail-closed entries.

Phase 9 — Backend API: auth, rules, trades, KPI, market/live prices, health, reset. No UI.

Phase 10 — Tests: targeted isolated tests first; add validation-matrix regressions; temp/fake DEMO only; no production env/trading calls; architecture isolation.

Phase 11 — Deployment prep: separate systemd/env/DB/runtime/port/nginx/TLS/health/logs. Actual deploy/restart requires explicit authorization.

Phase 12 — Online verification: login, market resolution/subscription, triggers, frequency/concurrency, schedules/windows, underlying filters, fees, TP/SL, resolution, disconnect/no-backfill, self-healing, API contract, LIVE isolation.

Definition of Done: isolated service/DB; impossible to submit real trades; all 21 definitions; spec-compliant rules; explainable tested accounting; underlying context/filtering; fail-closed auto-recovering health; documented APIs; no retroactive trades; documented reuse; LIVE unaffected.
