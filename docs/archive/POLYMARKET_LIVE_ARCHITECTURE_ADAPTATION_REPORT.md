# Polymarket LIVE Architecture Adaptation Report

Date: 2026-07-21

## 1. LIVE / DEMO Separation

LIVE was adapted from an embedded `/live` area inside the DEMO FastAPI process into a standalone service-ready application.

- DEMO remains `polymarket-collector/app.py` with `app:app`.
- LIVE now has its own entrypoint: `polymarket-collector/live_app.py` with `live_app:app`.
- DEMO no longer includes the LIVE router or runs LIVE migrations by default.
- A legacy opt-in switch remains only for compatibility: `ENABLE_LEGACY_LIVE_IN_DEMO=true`.
- LIVE uses its own DB file and does not read, write, join, or migrate the DEMO DB.

No deployment, real trade, allowance, redemption, or Live Rule creation was performed.

## 2. Future Entrypoints And Services

Prepared target:

```text
DEMO:
  service: polymarket.service
  entrypoint: app:app
  port: 8000
  database: /opt/polymarket-btc/poly_data.sqlite3

LIVE:
  service: polymarket-live.service
  entrypoint: live_app:app
  port: 8001
  database: /opt/polymarket-btc-live/poly_live.sqlite3
  domain: https://live-poly.dvirtechnologies.com
```

Prepared artifacts:

- `polymarket-collector/deploy/polymarket-live.service`
- `polymarket-collector/deploy/nginx-live-poly.conf`
- `polymarket-collector/deploy/live.env.example`
- `polymarket-collector/deploy/DEPLOYMENT_CHECKLIST.md`

## 3. Separate Databases

LIVE DB target:

```text
/opt/polymarket-btc-live/poly_live.sqlite3
```

The LIVE migration creates only `live_*` tables in the LIVE DB, including the new `live_backups` audit table. Tests verify:

- DEMO `init_db()` creates DEMO tables only.
- LIVE migration creates LIVE tables only.
- DEMO tables are absent from the LIVE DB.
- LIVE tables are absent from the DEMO DB.

## 4. Login And Sessions

Implemented/adapted:

- Single initial username: `Admin@system.com`.
- Argon2id password verification for production hashes.
- SHA-256 compatibility retained only for existing tests/local compatibility.
- Sessions persist server-side by version until logout, password/session-secret rotation, or explicit revoke-all.
- `POST /live/sessions/revoke-all` increments session version and invalidates existing sessions.
- Cookies are `HttpOnly`, `Secure`, and `SameSite=Strict`.
- Login rate limiting remains in place.
- Write routes now require CSRF.
- Critical actions require password re-authentication.

Prepared CLI:

```bash
python scripts/create_live_admin.py
```

It generates a 32+ character password, prints it once, and prints an Argon2id hash intended for Google Secret Manager. It was not run to create a real production password.

## 5. Maintenance Flow

Added a `Maintenance` tab and API:

- `GET /live/maintenance/status`
- `POST /live/maintenance/drain`
- `POST /live/maintenance/cancel`
- `POST /live/maintenance/readiness`

The state machine supports:

- `RUNNING`
- `DRAINING`
- `stop_ready=true` only when exposure is `0`, open orders are `0`, and open deals are `0`.

The app does not stop the process directly. It only exposes readiness for a future constrained systemd/helper flow.

## 6. Backup And Logging Strategy

Added backup infrastructure:

- SQLite backup API.
- Temporary backup file.
- gzip compression.
- Atomic rename.
- SHA-256 checksum.
- Retention cleanup by 7 days and 1GB max total storage.
- Warning threshold at 80%.
- Audit rows in `live_backups` and `live_audit_log`.

Logging guidance was added to deployment docs:

- Backend: journald.
- Retention: 14 days or 500MB.
- No private keys, API secrets, passphrases, cookies, session secrets, or raw private WebSocket auth payloads in logs.

No real server backup was created.

## 7. nginx / systemd / Deployment Artifacts

Prepared nginx server block includes:

- HTTP to HTTPS redirect.
- Proxy to `127.0.0.1:8001`.
- WebSocket upgrade headers.
- Long WebSocket timeouts.
- Basic security headers.

Prepared systemd unit includes:

- `EnvironmentFile=/etc/polymarket-live/live.env`.
- `WorkingDirectory=/opt/polymarket-btc-live`.
- `ExecStart` for `uvicorn live_app:app --host 127.0.0.1 --port 8001`.
- Restart on failure.
- Basic hardening flags.

No DNS, nginx, Certbot, firewall, systemd, or deployment command was executed.

## 8. Files Changed

Core:

- `polymarket-collector/app.py`
- `polymarket-collector/live_app.py`
- `polymarket-collector/live/auth.py`
- `polymarket-collector/live/config.py`
- `polymarket-collector/live/repository.py`
- `polymarket-collector/live/router.py`
- `polymarket-collector/live/backup.py`

Tests:

- `polymarket-collector/tests/test_live_system.py`

Docs/config:

- `polymarket-collector/.env.example`
- `polymarket-collector/LIVE_SYSTEM.md`
- `polymarket-collector/README.md`
- `polymarket-collector/requirements.txt`
- `polymarket-collector/scripts/create_live_admin.py`
- `polymarket-collector/deploy/polymarket-live.service`
- `polymarket-collector/deploy/nginx-live-poly.conf`
- `polymarket-collector/deploy/live.env.example`
- `polymarket-collector/deploy/DEPLOYMENT_CHECKLIST.md`
- `POLYMARKET_LIVE_ARCHITECTURE_ADAPTATION_REPORT.md`

Pre-existing user/untracked files were not modified unless listed above.

## 9. Tests And Results

Passed:

```text
python -m unittest discover -s polymarket-collector/tests
Ran 39 tests
OK
```

Passed:

```text
python -m compileall polymarket-collector
```

Coverage added/updated:

- LIVE standalone entrypoint.
- DEMO entrypoint without LIVE.
- Separate DB files and isolated migrations.
- Public health redaction.
- Login with `Admin@system.com`.
- Argon2id verification.
- Persistent sessions until logout/revocation.
- Revoke all sessions.
- CSRF write protection.
- Re-auth for Kill Switch deactivation.
- Maintenance drain/readiness/cancel.
- Backup creation/checksum/audit.
- Existing DEMO regressions.
- Existing LIVE regressions.

## 10. Security Review

Reviewed:

- No real trading flags enabled.
- Real adapter writes remain blocked.
- No Live Rule was created.
- No allowance or redemption was performed.
- No deployment was performed.
- DEMO no longer initializes LIVE DB by default.
- Public `GET /health` exposes only `{"status":"ok"}` or `{"status":"degraded"}`.
- Detailed LIVE health remains behind login.
- Cookie flags are `HttpOnly`, `Secure`, `SameSite=Strict`.
- CSRF required for write routes.
- Re-auth required for critical routes.
- Secret scan found placeholders and code paths only, not hardcoded real secrets.

## 11. Git / Commit / Push Status

Local commit created:

```text
5ff0e3a Separate live service architecture
```

Push target requested:

```text
origin/main
```

Push attempt:

```text
git push origin main
```

Result: blocked by Codex safety policy because `origin/main` points to an external GitHub remote that was not verified as trusted by the approval reviewer. No force push, workaround, or indirect push was attempted.

The local commit remains ready. The owner can run a manual push from the trusted local environment after confirming the remote.

## 12. Remaining User Actions Before Deployment

Do not paste secrets into chat or Git.

Before deployment:

1. Confirm Python version on the Linux VM.
2. Create `/opt/polymarket-btc-live`.
3. Create `/etc/polymarket-live/live.env` from the non-secret example.
4. Generate admin password/hash on the server and store the hash in Google Secret Manager.
5. Generate `LIVE_SESSION_SECRET` as a 64-byte secret and store it in Google Secret Manager.
6. Store Polymarket credentials in Google Secret Manager.
7. Configure service account access to Secret Manager.
8. Install dependencies in the LIVE virtualenv.
9. Configure nginx and Certbot.
10. Start only `polymarket-live.service`.
11. Verify DEMO stays healthy during LIVE startup.
12. Use dry-run/read-only flows only until explicit future approval for any real trade.
