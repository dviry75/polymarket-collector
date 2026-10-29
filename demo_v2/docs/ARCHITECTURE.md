# DEMO V2 — Phase 1 Architecture

Option C + narrow Option A. All DEMO ownership stays in this subtree. No LIVE
files, configuration, schema, services or extraction are changed. The latest
user-approved authentication decision supersedes the audit's provisioning question.

## Package and ownership

```text
demo_v2/
  app.py                   independent FastAPI factory, no module-level app/startup
  config.py                DEMO_* only; secret fields excluded from repr
  domain/models.py         seven assets, three timeframes, identity/provenance DTOs
  providers/contracts.py   discovery, observations, resolution, fees, underlying, credentials
  providers/book_primitives.py  the complete approved direct LIVE import surface
  persistence/             dedicated SQLite connection boundary + future repository/session ports
  streams.py               ordered fresh decision ingress; separate lossy telemetry buffer
  health.py                READY / DEGRADED / NOT_READY and typed component reasons
  api/                     independent router and fail-closed session placeholder
  tests/                   temporary databases, fake observations, import/I/O guards
  deploy/                  inert env/resource examples; no installed units or proxy changes
```

`DemoConfig.from_env()` reads only explicitly named `DEMO_*` keys, never a dotenv
file or LIVE fallback. DB must resolve to `DEMO_DATA_DIR/demo_v2.sqlite3`; symlink
escapes, hard-link aliases and foreign table namespaces are rejected. Configuration
and app imports do not create files. Lifespan probes only this dedicated database;
Phase 1 creates no business tables and executes no migrations. The connection
skeleton is not yet a transactional domain repository.

Credentials will be provisioned into DEMO configuration separately with the same
username/password values as LIVE. No values are copied/read in development.
The session signing secret, `demo_v2_session` cookie scoped to `/demo/`, and future
`SessionStateStore` belong to DEMO. There is no LIVE DB/session lookup. Credential
validation is a port only: login, session signing and revocation storage are not
implemented yet, so `/api/*` denies access with `DEMO_AUTH_NOT_IMPLEMENTED`.

## Imports and provider flow

Only `providers/book_primitives.py` may directly import `live.order_book` helpers
and `live.strategy.exact_trigger`. Their stdlib-only dependency closure is tested
in a fresh process. The legacy source directory is added by the explicit launch
environment, not by application code mutating `sys.path`. No LIVE config/repository,
router, entrypoint, WebSocket manager, strategy runtime, adapter, account
reconciliation or IPC command dependency is allowed. SDKs/private keys are absent.

```text
DEMO config -> factory -> DEMO database probe / health / API boundary
future public providers -> verified instance/token metadata + ordered observations
                        -> DecisionStream -> future rule engine
                        -> optional TelemetryBuffer -> future telemetry writer
future official resolution -> independent settlement path (not synthetic bid/sell)
future credential validator -> DEMO session/revocation store -> DEMO API
```

Catalog definitions cover 7 x 3 families without network discovery. Instance start
and end are explicit metadata; no BTC slug parsing or fixed five-minute rollover.
Provider objects are injected and never started by the skeleton. Absent fee or
reference evidence is represented by `None`, not a fabricated zero/default.

## Decisions and telemetry

One `DecisionStream` belongs to one ordered provider connection on one event loop.
Providers assign local contiguous observation ordinals after expanding ordered
frame transitions; these are not invented exchange sequence numbers. Every accepted
observation reaches the FIFO independently of telemetry sampling/coalescing.
Publish and delivery check readiness, generation, exchange-observation age and
local receipt age. Bounds are DEMO settings, not inherited LIVE thresholds.

Providers call `invalidate(reason)` immediately on disconnect or detected loss.
Sequence loss/reorder, stale/future data or overflow rejects the observation,
invalidates pending decisions and requires a strictly newer generation. Beginning
a generation discards old queued observations. The first fresh ready observation
can be delivered immediately. No historical entry/exit replay is provided.
`TelemetryBuffer` may coalesce or evict values and has no reference back to the
decision stream; persistence never authorizes or selects prices for decisions.
No provider pump, rule evaluator, retry worker or trade engine is implemented.

`/health` is public process liveness only. `/ready` is a bounded public readiness
summary and returns 503 while dependencies are unimplemented. The registry models
all three states but does not yet poll providers or perform automatic recovery.
The skeleton remains NOT_READY even after its DB probe succeeds. No trading is enabled.

## Local validation and future launch

Python 3.11+; dependencies are separate from the LIVE requirements. From repo root,
using an isolated virtual environment:

```bash
python -m pip install -r demo_v2/requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest -c demo_v2/pytest.ini demo_v2/tests -q
# Optional local launch only; do not run this against production resources.
PYTHONPATH="$PWD/polymarket-collector" python -m uvicorn demo_v2.app:create_app --factory --host 127.0.0.1 --port 8010
```

The launch example does not load an env file. A future operator provisions only
DEMO settings. Resource examples are placeholders: ports/paths/route installation
and secret provisioning are not deployed in this phase. Business schema, real
providers, full health recovery/auth, rule engine/exits and accounting remain later work.
