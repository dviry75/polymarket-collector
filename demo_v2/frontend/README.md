# Polymarket DEMO V2 frontend

The supplied Hebrew RTL vanilla JavaScript ES-module application is integrated,
not replaced by a framework. The ZIP's `dist/` contained its original source; that
source now lives in `src/`. `styles.css` is byte-identical to the supplied file.
`integration.css` only fits real bid/ask pairs and longer backend IDs into the
existing cards/table. Layout, icons, forms, dialogs, colors and typography remain.
The standalone mock HTML and seed/local-storage repository are not shipped.
`docs/UX.md` preserves the original prototype design notes, not the runtime contract.

## Local development and build

Node 24+, npm (lockfile committed). No runtime npm dependencies.

```sh
cd demo_v2/frontend
npm ci --ignore-scripts
npm run typecheck
npm test
npm run build
DEMO_BACKEND_TARGET=http://127.0.0.1:8010 npm run dev
# Or serve the completed build:
DEMO_BACKEND_TARGET=http://127.0.0.1:8010 npm run preview
```

Both local servers bind **127.0.0.1 only**; the frontend defaults to port 5173
(`DEMO_FRONTEND_PORT` overrides it). Open `/demo/`. The local preview/dev server
proxies `/demo/api/v1/` to the separately started loopback backend, preserving
cookies and CSRF without enabling backend CORS. Provision an independent DEMO
backend configuration as described in `../README.md`; local HTTP needs explicitly
`DEMO_COOKIE_SECURE=false`. Never source production env files. The dev server is
not a production deployment artifact.

`DEMO_API_BASE` is the only public build-time setting. Default `/demo/api/v1` uses
the same origin, also suitable for a future reviewed DEMO proxy. For example,
`DEMO_API_BASE=/demo/api/v1 npm run build`. Only that string is written to
`build/src/config.js`; no environment object or secret is exposed. Absolute HTTP(S)
bases are supported for explicitly configured environments; cookie SameSite/Secure
and CORS rules still apply. Same-origin is the supported default.

`build/`, dependencies, traces/screenshots and local env files are ignored. Build
copies native browser ES modules and styles; it does not introduce a bundler.
There was no existing lint configuration, so no lint framework was added.
Typecheck runs strict TypeScript/JSDoc checks on the API/mapping boundary plus a
syntax check of the retained JavaScript UI renderer. It is not a fully typed UI.

## Screen/API ownership

All requests go through `src/api/client.js` with `credentials: include`, no-store,
a bounded timeout and an in-memory CSRF token. Passwords/session state are never
written to browser storage. Login/current establish DEMO state; 401 clears it;
logout uses the backend's **all DEMO sessions** revocation semantics.

| Existing screen/component | Backend source / write |
| --- | --- |
| Login, header/logout | `/auth/login`, `/auth/current`, `/auth/logout` |
| Overview six KPIs | `/kpi`, identical cohort query as `/trades`, no pagination |
| Trades, filters/pagination, drawer | `/trades?limit=10&offset=...`, `/trades/{id}` |
| Rules and complete form | `/rules`, version-checked PUT, enable/disable/duplicate/archive |
| Market cards / timeframe selector | `/markets` current instance, independent UP/DOWN bid/ask, timing/fresh/listening |
| Health header/drawer | `/health` state, entries_allowed, components/blockers |
| Maintenance confirmations | `/maintenance/reset-trading-data`, `/maintenance/factory-reset` |

Raw financial values remain decimal strings. Display rounds with BigInt, without
floating-point financial arithmetic. KPI/wallet/success/P&L definitions belong to
the backend. Rule `net_pnl` is the backend's lifetime realized P&L, not a sum of the
current trade page. Null/missing prices are dashes, not zero. Inclusive date inputs
become entry-time `[from,to)` instants via IANA `Asia/Jerusalem`, including DST.

The backend has **shared entry windows for all selected market definitions**;
the form labels this explicitly instead of pretending each timeframe has its own
independent windows. Weekdays map Sunday-first UI to Monday=0 API; seconds,
overnight/multiple weekly ranges and all underlying enums survive editing.
Archiving preserves history and existing position management. New edits use the
current immutable version ID; 409 keeps the user's draft and reports the conflict.

REST refreshes every 10 seconds on the overview; elsewhere only markets/health
poll automatically. Hidden tabs pause polling. Navigation/actions refresh complete
read models. Cohort requests are cancelled/revision-checked to prevent old filters
or sessions winning a race. Failed reads show errors/stale state, never mock
fallbacks. Locally elapsed quote age may mark a snapshot stale, never ready.
Fee-unsupported blockers remain enforced by the backend and explained in the UI.

## Browser verification

```sh
DEMO_TEST_PYTHON=/path/to/demo-test-venv/bin/python npm run test:e2e
```

Requires the backend test requirements and Chrome at `/usr/bin/google-chrome`
(or `DEMO_TEST_CHROME`). Playwright starts a real Uvicorn server and production
frontend preview on loopback ports 52971/52972. The fixture creates/removes its own
temporary DB, fake credentials/positions/books, and never loads production config
or calls public providers. Browser tests cover full login/logout, aligned server
KPIs and pagination, trade details, all rule fields/actions, reset confirmation,
expired/invalid auth, request failure with no fallback, and desktop/mobile layout.
Servers receive SIGTERM and stop after the run. No deployment is performed.

Validated on 2026-10-30: 14 Node contract/unit tests, 3 real Chrome E2E
workflows, strict API/model typecheck + UI syntax check, production build and
200 backend regression tests passed. Desktop (1440px) and mobile (390px) layouts
were visually checked. No lint configuration existed. Fee-enabled entries remain
blocked pending the existing fee-contract verification; this integration does not
relax that gate. Deployment Review is a separate next step.
