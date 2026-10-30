# DEMO V2 deployment runbook — preparation only

**Not executed. A separate deployment approval is required for every system,
service, exposure, DNS and certificate step below.** Review baseline `6dd9992`;
see `REVIEW.md` for host evidence, validation and outstanding conditions.

## 1. Preconditions

Use this host only after rechecking `hostname`, `ss -lntup`, disk/RAM, existing
units and `sudo ufw status verbose`. Port **8010** was free on 2026-10-30; recheck.
No LIVE/proxy/tunnel installation was found here. Never reuse a LIVE venv/env/DB.
Choose exactly one exposure profile before proceeding:

- **Recommended for the inspected private-LAN home host:** a newly approved named
  Cloudflare Tunnel → nginx **127.0.0.1:8080** → API **127.0.0.1:8010**. Requires
  domain/account control and a separately provisioned tunnel credential. No router
  forwarding is needed for this proposal; outbound tunnel connectivity remains to
  be verified. `nginx-demo-tunnel.conf.example` + `cloudflared-demo.example.yml`.
- Direct HTTPS alternative: nginx 443/80 + a valid certificate and verified public
  ingress/router forwarding/firewall/DNS. CGNAT/public IP reachability is unknown.
  Use `nginx-demo-poly.conf`; do not install both profiles.

Both profiles keep the app on loopback and same-origin at
`https://demo-poly.dvirtechnologies.com/demo/`. Do not expose 8010 or 8080.
Provision Python 3.14 + venv support and Node 24+ on the **build** machine; nginx
is a new dependency here. Node is unnecessary for the deployed static frontend.
Package installation may auto-start nginx: suppress that on this new host until
configuration/exposure are approved (e.g. mask the absent nginx unit before an
approved package install, and unmask only in step 10). Never mask an existing
proxy serving other applications. No packages were installed by this review.

Run the remaining shell snippets in one operator terminal after approval:

```sh
set -euo pipefail
DEMO_ROOT=/opt/polymarket-demo-v2
DEMO_SOURCE=/home/dvir/Documents/Codex/2026-10-29/new-chat/work/polymarket-collector
```

## 2. Install/update code (immutable release)

Do not pull into the running app directory. Fetch into the workspace, select and
record the reviewed commit, then archive exactly that version:

```sh
git -C "$DEMO_SOURCE" fetch origin main
DEMO_SHA=$(git -C "$DEMO_SOURCE" rev-parse origin/main)
git -C "$DEMO_SOURCE" show --no-patch --oneline "$DEMO_SHA"
DEMO_RELEASE="$DEMO_ROOT/releases/$DEMO_SHA"
test ! -e "$DEMO_RELEASE"
sudo install -d -m 0755 "$DEMO_RELEASE/app"
git -C "$DEMO_SOURCE" archive "$DEMO_SHA" | sudo tar -x -C "$DEMO_RELEASE/app"
```

The readonly source archive includes the approved pure `live.order_book` and
`live.strategy.exact_trigger` in their original package location. It does not
use an installed LIVE application or copy its configuration. Only DEMO
requirements/entrypoint are used; no trading SDK is installed.

## 3. Python environment

```sh
sudo python3 -m venv "$DEMO_RELEASE/venv"
sudo "$DEMO_RELEASE/venv/bin/python" -m pip install -r "$DEMO_RELEASE/app/demo_v2/deploy/requirements.lock"
sudo "$DEMO_RELEASE/venv/bin/python" -m pip check
```

This is a release-specific DEMO environment. The version-pinned production closure
was installed successfully in a fresh Python 3.14.4 venv during review. Run tests
in a separate temporary test venv with `demo_v2/requirements-test.txt`, using only
fake/temp dependencies and no production environment variables.

## 4. Frontend build and validation

Build the same SHA as the backend, as the unprivileged operator. No global npm
packages, runtime dev server or mock HTML are needed:

```sh
DEMO_STAGE=$(mktemp -d /tmp/polymarket-demo-build.XXXXXX)
git -C "$DEMO_SOURCE" archive "$DEMO_SHA" | tar -x -C "$DEMO_STAGE"
(cd "$DEMO_STAGE/demo_v2/frontend" && npm ci --ignore-scripts && npm run typecheck && npm test && DEMO_API_BASE=/demo/api/v1 npm run build)
sudo install -d -m 0755 "$DEMO_RELEASE/frontend/demo"
sudo cp -a "$DEMO_STAGE/demo_v2/frontend/build/." "$DEMO_RELEASE/frontend/demo/"
sudo chown -R root:root "$DEMO_RELEASE"
sudo chmod -R go-w "$DEMO_RELEASE"
```

The build contains only index/ES modules/styles. Hash routes such as
`/demo/#rules` and `/demo/#rule/edit/<id>` work on refresh. Modules use `./src/...`
under `/demo/`; the API base is relative. Missing assets/API versions remain 404;
this hash router needs no SPA fallback. No favicon was supplied (404 is harmless).
Run the full backend suite and the documented frontend E2E against this SHA before
promotion. Do not point validation at the production DB or load its env file.

## 5. Runtime directories and release links

Create the account only if absent; never repurpose another service user:

```sh
getent passwd polymarket-demo-v2 >/dev/null || sudo useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin polymarket-demo-v2
sudo install -d -o polymarket-demo-v2 -g polymarket-demo-v2 -m 0700 /var/lib/polymarket-demo-v2
sudo install -d -o root -g root -m 0700 /etc/polymarket-demo-v2 /var/backups/polymarket-demo-v2
sudo ln -sfn current/app "$DEMO_ROOT/app"
sudo ln -sfn current/venv "$DEMO_ROOT/venv"
sudo ln -sfn current/frontend "$DEMO_ROOT/frontend"
# First install only; updates switch current in step 15 with the service stopped.
test ! -e "$DEMO_ROOT/current"
sudo ln -s "$DEMO_RELEASE" "$DEMO_ROOT/current"
```

Code/venv/static files are root-owned/read-only for the runtime user. DB/WAL/SHM
live in the private 0700 state directory. systemd supplies a private runtime dir;
logs go to the unit journal. No `/var/log` app tree or LIVE path is reused.

## 6. Environment/secrets

Recommended provisioning is a **new Argon2id hash of the operator-entered same
password**, rather than reading any LIVE env/hash. The helper creates a fresh
independent 48-byte URL-safe session secret and a new 0600 file; it refuses overwrite
and never echoes values. Run it interactively, not via shell arguments/history:

```sh
sudo "$DEMO_ROOT/venv/bin/python" "$DEMO_ROOT/app/demo_v2/deploy/provision_env.py" --output /etc/polymarket-demo-v2/demo.env
sudo chown root:root /etc/polymarket-demo-v2/demo.env
sudo chmod 0600 /etc/polymarket-demo-v2/demo.env
```

Required private values: `DEMO_AUTH_USERNAME`, `DEMO_AUTH_PASSWORD_HASH`,
`DEMO_SESSION_SECRET`. Nonsecret required production settings and queue/timeouts
are all in `demo.env.example`; public data and Secure cookies must both be true.
Never source the env file in a shell: systemd parses literal `$` in Argon2 hashes.
For credential rotation, prepare a replacement privately, retain a protected
rollback copy, install atomically and restart only DEMO after approval. Rotation
revokes DEMO sessions. Do not reuse the LIVE session secret.

No Polymarket trading key, wallet/private key, Binance key, Chainlink key or
CLOB credential is required. An optional tunnel credential belongs to cloudflared,
never the DEMO app/env. Direct HTTPS needs a certificate/key owned by the proxy.

## 7. systemd installation

```sh
sudo install -m 0644 "$DEMO_ROOT/app/demo_v2/deploy/polymarket-demo-v2.service" /etc/systemd/system/polymarket-demo-v2.service
sudo systemd-analyze verify /etc/systemd/system/polymarket-demo-v2.service
sudo systemctl daemon-reload
```

The unit binds loopback, uses **one worker**, performs config-only preflight,
restarts on failure with backoff, and allows DB writes only in the dedicated state
dir. UMask 0077 protects new SQLite sidecars. It cannot access home/LIVE resource
paths. Initial memory envelope: high 256 MiB/max 512 MiB; adjust from measurement,
not by weakening financial/data guards. Do not run a second app or `--reload`.

## 8. Reverse proxy

After the approved nginx package installation, select **one** profile:

```sh
# Recommended tunnel profile; use nginx-demo-poly.conf instead for direct TLS.
DEMO_PROXY_PROFILE=nginx-demo-tunnel.conf.example
sudo install -m 0644 "$DEMO_ROOT/app/demo_v2/deploy/nginx-demo-api.conf" /etc/nginx/snippets/polymarket-demo-v2-api.conf
sudo install -m 0644 "$DEMO_ROOT/app/demo_v2/deploy/$DEMO_PROXY_PROFILE" /etc/nginx/sites-available/polymarket-demo-v2.conf
sudo ln -sfn /etc/nginx/sites-available/polymarket-demo-v2.conf /etc/nginx/sites-enabled/polymarket-demo-v2.conf
sudo nginx -t
```

Retain unrelated sites. Review the packaged default site before exposure; do not
publish a new default server inadvertently. Static root is **frontend/**, not the
repository. API prefix/cookie path are preserved; caching is disabled. No CORS or
WebSocket proxy is added. Nginx overwrites forwarded IP headers; only 127.0.0.1 is
trusted by Uvicorn. The tunnel profile trusts CF-Connecting-IP only from loopback.
API logs omit query strings, request bodies, cookies and Authorization.

## 9. Exposure / DNS / TLS (separate approved operation)

Target hostname has no address via this host's resolver at review time. Do not
assume any current DNS/router/firewall configuration from repository history.

For the recommended Tunnel profile, the domain operator must provision a dedicated
named tunnel/public hostname using the official Cloudflare workflow, privately
install its scoped credential, and adapt `cloudflared-demo.example.yml`. Set the
route to `http://127.0.0.1:8080`, enforce browser HTTPS at the edge and disable cache
for this hostname. Validate ingress with the installed cloudflared before starting
it. Cloudflare supplies edge TLS; the local origin is loopback HTTP and no local
certificate/port forwarding is required. Do not use a temporary Quick Tunnel URL.
Do not add hostname rules to an unrelated tunnel without reviewing its owner.

For direct TLS, confirm a public route (CGNAT/ISP policy remain unknown), authorize
router/NAT and firewall 80/443, provision A/AAAA records only for reachable address
families and obtain a valid certificate before enabling the TLS example. Provision
and renew the shown Let's Encrypt paths with an approved ACME method. Never publish
an AAAA record unless IPv6 ingress/TLS was verified. No Cloudflare credential is
required for direct TLS unless separately chosen for DNS automation.

No DNS, router, firewall, tunnel, certificate or Cloudflare operation was performed
in this review. `sudo ufw status verbose` requires operator authentication here.

## 10. Start (only after separate approval)

Ensure exposure is protected/ready and recheck 8010/8080 for conflicts. No LIVE unit
is part of this procedure. If nginx was masked during the new package installation,
unmask it only now:

```sh
sudo systemctl enable --now polymarket-demo-v2.service
sudo systemctl unmask nginx.service
sudo systemctl enable --now nginx.service
# Start only the separately approved dedicated tunnel if that profile was chosen.
```

Schema migrations run on DEMO lifespan only. Back up any pre-existing DEMO DB first;
never assume a migration is backward compatible. The service preflight fails
without valid independent auth, Secure cookies, public-data mode and non-root user.

## 11. Health verification

```sh
curl --fail http://127.0.0.1:8010/health
sudo systemctl status polymarket-demo-v2.service --no-pager
ss -lntup
curl -I https://demo-poly.dvirtechnologies.com/
curl -I https://demo-poly.dvirtechnologies.com/demo/
```

`/health` is internal minimal liveness only: alive/SIMULATION/trading_enabled=false.
It is not exposed by the nginx public profiles. `/ready` returns a bounded global
summary, often 503 while fees/dependencies are blocked. Do not restart-loop or force
READY because of expected fee-related DEGRADED/NOT_READY. Authenticated
`/demo/api/v1/health` and the UI are the readiness/component sources of truth.
On restart, rules/open positions/quotas persist; no missed observations are replayed.
A public WS connection advances the durable generation, clears old snapshots and
requires fresh observations/dependencies plus continuous stability. Official
resolution remains available for held positions. There is no retrospective entry.

## 12. Browser smoke

Use the exact public HTTPS hostname: root → `/demo/`, login/current → rules and
trades/KPI → market freshness/listening → component health → logout. Refresh a hash
deep link. Check cookies are host-only, HttpOnly, Secure, SameSite=Strict, Path=/demo/.
Confirm CSS/modules load without mixed content, secrets in bundles or CORS. Market
quotes may be unavailable with no eligible subscription demand (including fee-gated
new-entry demand); catalog/research data is separate. Never relabel it fresh.
Do not create live-environment reset/trading actions for a smoke unless explicitly
authorized. Resets are tested with disposable DBs, never production history.

Fee blocker **CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED** must remain.
UNSUPPORTED fee-bearing entries stay closed; missing fees never become zero/VALID.
A first-run public-provider/soak check is still needed after the future start.

## 13. Logs and backup

```sh
journalctl -u polymarket-demo-v2.service -n 100 --no-pager
sudo tail -n 50 /var/log/nginx/polymarket-demo-v2.error.log
# Run only after deployment and backup approval; unique destination, never overwrite.
DEMO_BACKUP="/var/backups/polymarket-demo-v2/demo-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
sudo "$DEMO_ROOT/venv/bin/python" "$DEMO_ROOT/app/demo_v2/deploy/backup_sqlite.py" --source /var/lib/polymarket-demo-v2/demo_v2.sqlite3 --destination "$DEMO_BACKUP"
```

The SQLite online backup API includes committed WAL state and checks the saved
copy. Do not copy a live `.sqlite3` file alone or delete WAL/SHM. Backups are private
0600 in a 0700 directory; budget disk and copy encrypted off-host under a separate
policy. No pruning, cron or timer is installed. Do not paste raw config/logs with
credentials/cookies; application access logging is disabled and proxy access logs
use a query-free format.

## 14. Rollback

Record `readlink -f "$DEMO_ROOT/current"` before each update. With a schema-compatible
previous release, stop only DEMO, atomically repoint current and start DEMO:

```sh
DEMO_PREVIOUS=/opt/polymarket-demo-v2/releases/REPLACE_WITH_REVIEWED_PREVIOUS_SHA
test -d "$DEMO_PREVIOUS/app"
sudo systemctl stop polymarket-demo-v2.service
sudo ln -s "$DEMO_PREVIOUS" "$DEMO_ROOT/current.next"
sudo mv -Tf "$DEMO_ROOT/current.next" "$DEMO_ROOT/current"
sudo systemctl start polymarket-demo-v2.service
```

The DB is retained. Verify both schema and unit/ExecStartPre compatibility. Releases
before this preparation commit lack `deploy.preflight`; do not blindly switch to
such a release with the new unit. Use a reviewed compatible previous unit/release
when necessary. A first-install rollback may simply stop DEMO and withdraw only
its new public route after approval, preserving the DB. Do not roll older code
onto an incompatible migrated schema.
If restore is necessary, first stop DEMO and preserve the *entire current state
directory* (DB/WAL/SHM) under a private quarantine name. Restore a verified consistent
backup into a newly owned 0700 DEMO state directory, using mode 0600 and the service
owner, then validate before starting. Review loss of history since backup and session
revocation before authorizing restore. Never delete history as a rollback shortcut.

## 15. Update procedure

Fetch/review new SHA → build/test a new immutable release → record prior release
and migration compatibility → approved WAL-aware backup → stop DEMO → atomically
switch `current` to the tested release (as above) → start DEMO → internal/public
health and browser checks → retain prior release/backup for rollback. Static files
and the venv switch with the same release; nginx need not reload for a code-only
update. Keep maintenance brief; API failures during the stop window must remain
visible. Independently reprovision changed units/env/proxy only when approved.

References: [systemd execution/sandbox settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
[nginx proxy URI/header semantics](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[SQLite online backup](https://www.sqlite.org/backup.html),
[Cloudflare named tunnel configuration](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/).
