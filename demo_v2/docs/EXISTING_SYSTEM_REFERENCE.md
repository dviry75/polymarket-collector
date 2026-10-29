# DEMO V2 — Existing System Reference

Always verify current source before reuse.

Repository context: AGENTS.md, SYSTEM_CONTEXT.md, POLYMARKET_LIVE_STABILITY_RUNBOOK_HE.md, docs/archive/. Historical reports are reference, not product truth.

Legacy DEMO: polymarket-collector/app.py. It already has rules/deals, exact best-ask entry, best-bid exits, resolution and SQLite. However old semantics such as one-open-deal restrictions, side-specific limits and configured-threshold exit storage may conflict with DEMO V2; use as reference only where matching current spec.

Current LIVE entrypoints: polymarket-collector/trader_app.py (trading core), dashboard_app.py (query/dashboard), live_app.py (legacy monolith).

High-value market-data references: live/market_websocket.py, live/order_book.py.

Strategy/runtime references: live/strategy.py, live/strategy_runtime.py, live/strategy_repository.py.

Resolution: live/market_resolution.py and scripts/run_market_resolution.py.

Recovery/readiness: live/pause_recovery.py, live/recovery_policy.py, plus current reconciliation/readiness logic in SYSTEM_CONTEXT.md.

Accounting/fees: scripts/audit_fee_accounting.py, current accounting/adapter code and fee reports under docs/archive/.

Existing Paper Trading is described in polymarket-collector/LIVE_SYSTEM.md and already includes public Market WS, current/next subscription, exact best-ask entry, best-bid exit, resolution, simulated fee model and paper APIs. Major reuse/reference candidate, but DEMO V2 adds 21 markets, concurrent positions, richer schedules, wallets, underlying filters and separate UI ownership.

Authentication: inspect current LIVE dashboard auth/session implementation before creating any new credential store. DEMO must reuse credentials where safe but keep its own session.

Deployment references: polymarket-collector/deploy/ and nginx-live-poly.conf. Installed LIVE units remain authoritative; do not mutate LIVE deployment unless explicitly authorized.

Tests to inspect early: test_rules_deals.py, test_paper_trading.py, test_market_data_safety.py, test_market_resolution.py, test_market_ws_telemetry.py, test_order_book_sync_regression.py, test_pause_recovery.py, test_architecture_isolation.py and fee/accounting regression tests.

Separate repo dviry75/polymarket-dashboard is a Next.js frontend prototype with mock data. Codex/backend work must not edit or implement DEMO V2 UI there unless future explicit scope changes.
