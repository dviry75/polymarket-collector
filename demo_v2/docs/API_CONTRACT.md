# DEMO V2 — Backend API Contract Requirements

Product-level contract; exact schemas may evolve. Frontend is separate.

Principles: authenticated JSON APIs, unambiguous timestamps, decimal-safe money/prices, pagination, documented filters, stable machine-readable errors, health/freshness semantics, no secrets.

Auth: DEMO login using same credential source/values as LIVE where safe; separate DEMO session; logout/current-user.

Rules API: list/get/create/edit-version/enable/disable/duplicate-or-copy/delete-archive. Include id, name, markets, exact trigger, share size, frequency, TP/SL, weekly schedule, internal windows, underlying conditions, status, timestamps, trade count, rule-wallet/accounting.

Trades API: list/filter OPEN+CLOSED by rule, asset, market, timeframe, side, status, date range and underlying movement dimensions. Trade detail should include rule/version snapshot, market instance, side, entry/exit timestamps/prices, gross shares, entry notional, buy fee, net shares, exit reason, sell fee, total fees, net proceeds/P&L, underlying open/entry/exit values, movements and provenance.

KPI API: same filter semantics as trade list. Return wallet/scoped net cash flow, success rate, positive, negative, total trades, total fees. Break-even excluded from success denominator.

Market/live-price API: catalog, active market instance/times, UP/DOWN bid/ask summary, freshness and useful listening state. Frontend must not discover token IDs itself.

Health API: READY/DEGRADED/NOT_READY, machine-readable reasons, component status, freshness, reconnect/recovery state.

Maintenance API: Reset Trading Data, Factory Reset Demo. Never touch LIVE.

Streaming: if WS/SSE is used, document contracts for live prices, health changes and trade changes. Polling is acceptable where simpler but live UX must stay timely.

Prefer generated OpenAPI where practical and keep contract updated for the external UI agent.
