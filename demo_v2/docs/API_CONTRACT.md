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

Notifications: authenticated `GET/PUT /demo/api/v1/notifications` exposes/saves
enabled state, up to 10 recipients, opt-in categories, Jerusalem daily time and
1/3/6-hour periodic reports. PUT requires CSRF and expected_revision.
`POST /demo/api/v1/notifications/test` queues an explicit rate-limited test to
saved recipients only when both preference and private server transport gates
are enabled. Status distinguishes SMTP acceptance, failure and uncertain results;
neither credentials nor message bodies are returned. See EMAIL_NOTIFICATIONS.md.

Resource notification additions: opt-in event codes `NETWORK_HIGH`, `CPU_HIGH`,
`RAM_HIGH`, `DISK_HIGH`; settings `network_mib_per_second` (0.01–100000, default 10),
`cpu_percent`, `ram_percent`, `disk_percent` (1–100, default 90), and
`resource_duration_seconds` (10–3600, default 60). Metrics use existing host
telemetry; alert once per incident, rearm after 120 seconds below 80% of threshold.

Trade list/detail responses include required `serial_number` (positive safe JSON
integer). It is a global durable label, never a page index or per-rule counter.
UUID `id` and `/trades/{position_id}` paths remain unchanged. Read endpoints never
allocate numbers. Backfill is performed transactionally before runtime workers
start; new allocation commits with position/ledger insertion. Metadata keys are
`trade_serial:<position UUID>` and `trade_serial_counter`, in the existing DEMO
metadata table. No schema migration or new dependency. Explicit trading/factory
reset deletes trade mappings and retains the counter to prevent number reuse.

Notification settings now support `recipient_events`, a mapping of lowercase
mailbox to an array of selected event codes, with at most 10 keys. Keys must
belong to the saved recipients; invalid/duplicate mailbox variants or event
codes are rejected. An explicit empty array opts that recipient out of automatic
alerts. A missing key inherits the legacy `events` array, preserving existing
settings without any migration or writes on GET. Enabled settings require at
least one effective recipient/event subscription; `events: []` is allowed when
an individual subscription is nonempty. Example:

```json
{
  "recipient_events": {
    "ops@example.com": ["SYSTEM_FAULT", "NETWORK_HIGH"],
    "reports@example.com": ["DAILY_SUMMARY", "PERIODIC_SUMMARY"]
  }
}
```

All collectors use the union of effective recipient choices to decide what to
observe, and both queue insertion and delivery check the individual recipient.
Changing preferences cancels pending old-preference messages as before. Shared
report times, periodic hours and resource thresholds retain their existing
semantics. The explicitly confirmed test action targets all saved recipients,
including recipients that opted out of automatic alerts.
