# Polymarket DEMO V2 — Project Context

DEMO V2 is a research and strategy-testing backend for recurring Polymarket crypto Up/Down markets. It consumes real market data but performs simulated trading only. It must never submit/cancel/redeem real orders.

Initial catalog: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB x 5m, 15m, 1h = 21 market definitions.

All schedules use Asia/Jerusalem. Planned route: https://demo-poly.dvirtechnologies.com/demo/ with root redirect to /demo/.

Authentication should reuse the same credential source/values as LIVE where safe, but DEMO has a separate login page/session.

UI/frontend is owned by a separate UI workflow. Do not build pages, React components, CSS, mockups or visual redesigns. Backend must expose clean documented APIs.

Existing-code-first: inspect current implementation before writing replacements; reuse stable tested components where safe.

DEMO V2 must have separate entrypoint/service, DB, env/config, logs/runtime state and health state. It must not couple to real-order submission, private keys or LIVE financial state.

Do not automatically delete research history at this stage.
