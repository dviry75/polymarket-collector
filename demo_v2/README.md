# Polymarket DEMO V2

Isolated simulated trading and research, with the integrated Hebrew RTL frontend
under `frontend/` (see its README for local build/preview and browser tests).
No account execution API, wallet key, real orders, service installation or deployment. Read `AGENTS.md` and
its product context before changing behavior. Current instructions supersede the
original bootstrap-only authorization in that file.

## Local validation

Python 3.11+ and a dedicated environment; from repository root:

```bash
python -m pip install -r demo_v2/requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest -c demo_v2/pytest.ini demo_v2/tests -q
```

The explicit test path avoids collecting legacy operational modules. Tests use
only temporary databases, fake providers and in-process ASGI requests. The only
external code imports are the approved pure `live.order_book` and
`live.strategy.exact_trigger`, through `providers/book_primitives.py`.

## Configuration and optional local launch

Provision credentials manually in an independent DEMO configuration. No LIVE
secrets/configuration are read or copied by this application. Accepted names:

| Setting | Meaning |
| --- | --- |
| DEMO_DATA_DIR | DEMO-owned directory; DB must be `demo_v2.sqlite3` inside it |
| DEMO_DB_PATH | Optional exact owned path, checked again on every connection |
| DEMO_AUTH_USERNAME / DEMO_AUTH_PASSWORD_HASH | Argon2id preferred; explicit `sha256:` legacy hashes supported |
| DEMO_SESSION_SECRET | Independent secret, at least 32 characters |
| DEMO_PUBLIC_DATA_ENABLED | `true` enables public discovery/WS/reference workers; default `false` |
| DEMO_RECOVERY_STABILITY_SECONDS | Continuous dependency stability, default 2 |
| DEMO_OBSERVATION_MAX_AGE_SECONDS | Quote and research freshness, default 5 |
| DEMO_SESSION_TTL_SECONDS | Default 86400; durable revocation on logout/credential rotation |
| DEMO_COOKIE_SECURE | Default `true`; local HTTP requires explicitly `false` |

The Phase 1 `DEMO_LOGIN_USERNAME/PASSWORD_HASH` aliases remain accepted; AUTH names
win when both are supplied. No dotenv loader or production env fallback exists.
Missing auth denies API access. Public-data-disabled mode makes no network calls.

Optional **local only**, with independently provisioned DEMO settings:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/polymarket-collector" python -m uvicorn demo_v2.app:create_app --factory --host 127.0.0.1 --port 8010
```

Nothing in this work package installs or runs this server. Deployment examples
under `deploy/` are inert. SQLite migrations apply only to the selected DEMO DB on
application lifespan. Never point this at existing production resources.

## API contract for the frontend

Base prefix: `/demo/api/v1`; generated schemas: `/openapi.json`. Polling is the V1
frontend transport. JSON financial values are decimal **strings**; timestamps are
aware UTC ISO-8601. There is no account execution or real-trading endpoint.

1. POST `/auth/login` with `{username,password}` sets the host-only HttpOnly,
   Secure, SameSite=Strict `demo_v2_session` cookie, scoped to `/demo/`.
2. Login and GET `/auth/current` return `{user,csrf_token}`. Send credentials/cookie
   and `X-Demo-CSRF` on every authenticated write, including logout/reset.
3. POST `/auth/logout` revokes **all DEMO sessions**, durably. LIVE cookies have no
   effect. Credential rotation also revokes prior sessions. CORS is not enabled;
   the frontend should use the same origin through a separately reviewed proxy.

| Method | Relative path | Contract |
| --- | --- | --- |
| GET/POST | `/rules` | List/create full validated RuleSpec |
| GET/PUT/DELETE | `/rules/{id}` | Read; versioned full update; soft archive |
| GET | `/rules/{id}/versions` | Immutable version history |
| POST | `/rules/{id}/{action}` | enable, disable, duplicate (disabled copy), archive |
| GET | `/trades` | Paginated OPEN/CLOSED cohort, newest entry time/id first |
| GET | `/trades/{id}` | Entry version, market, fees/provenance, research and ledger |
| GET | `/kpi` | Same cohort predicate as trades; no pagination |
| GET | `/markets` | 21 definitions, current/held instances, both token prices/freshness/listening |
| GET | `/health` | Components, blockers, data generation, stability and worker state |
| POST | `/maintenance/reset-trading-data` | `{confirmation:"RESET_TRADING_DATA"}` |
| POST | `/maintenance/factory-reset` | `{confirmation:"FACTORY_RESET_DEMO"}` |

PUT body is `{spec: RuleSpec, expected_version_id: current_version_id}`; a stale
version returns 409. Every lifecycle edit creates a version. Archive keeps history
and open-position management. Duplicate starts disabled. RuleSpec fields and
schedule/filter enums are described by OpenAPI; weekdays use Monday=0,
Asia/Jerusalem local intervals, `[start,end)`, including cross-midnight ranges.
Entry windows use seconds from verified instance start, shared across all selected
market definitions. Empty schedules/windows
mean all time/full market. Multiple underlying conditions are ANDed.

Trade and KPI filters: `rule_id`, `instance_id`, `asset`, `timeframe`, `side`,
`status`, `from_date`, `to_date` (aware timestamps; entry-time `[from,to)`),
`movement_measurement=PERCENT|ABSOLUTE`, `movement_min`, `movement_max` (inclusive,
signed open-to-entry change). Missing underlying evidence cannot match a movement
filter. Trades also accept `limit=1..200` (default 50), `offset>=0`. Responses use
`{items,total,limit,offset}`. The historical cohort is materialized in memory for
Decimal-safe movement filtering; very large histories may need indexed projections.

Rule responses also expose `net_pnl`, the Decimal string sum of all closed
positions for that rule; it excludes open positions and is independent of trade
pagination. This is a read-model enhancement only.

KPI wallet is the complete signed ledger cash flow attributable to the filtered
trade cohort, including outstanding entry debits. No implicit balance constraint.
Success rate is percent `positive/(positive+negative)*100`, null if no denominator;
open and break-even are excluded. Fees are reporting amounts and are not debited
again. Errors use `detail.code` (401 session, 403 CSRF, 409 conflict/confirmation,
422 validation, 429 login limit, 503 unavailable). Validation omits input values.
All responses are `Cache-Control: no-store`.

Both resets atomically delete positions, including open ones, counter/rearm state,
ledger and trade research. No synthetic close is generated. Trading reset retains
rules/versions; factory also deletes them. Catalog, market metadata and session
state remain. Decisions are invalidated and new subscriptions require a fresh
generation. No public-provider integration can submit an order.

## Availability and external verification

See `docs/INTEGRATION_CONTRACTS.md`: BTC public discovery, native Market WS,
Binance research and an older official resolution were smoke-tested read-only.
Phase 9 verified all 21 current/next families and all seven spot research pairs.
Exact identities/timestamps and the HYPE futures-versus-spot distinction are recorded
in the integration evidence. These are point-in-time checks, not sustained-load results.
Fee-enabled public markets intentionally block entries until current tie/per-match
rounding is verified. An explicit fees-disabled market can supply valid
zero evidence. Production provisioning/review and deployment are separate work.

## Deployment preparation

`deploy/DEPLOYMENT.md` contains the reviewed installation/update/rollback plan;
`deploy/REVIEW.md` records actual host evidence and outstanding conditions. Units,
proxy profiles and operator helpers are preparation only. Nothing was installed
or activated on the host. Fee-bearing entries remain fail-closed.
