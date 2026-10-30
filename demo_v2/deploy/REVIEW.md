# Deployment review — 2026-10-30

**Verdict: READY WITH CONDITIONS. Preparation only; no deployment performed.**
Reviewed clean main at `6dd999286e44238d4c60cb0a8c03f0684b1518a1`. New files are
restricted to `demo_v2/`. The proposed service/exposure still require separate
approval and real private provisioning; this is not a public-runtime readiness claim.

## Observed host (read-only)

| Item | Evidence |
|---|---|
| Identity | `dvir-technologies`, operator `dvir`; Ubuntu 26.04.1 LTS x86_64, kernel 7.0.0-38-generic |
| Resources | 4 logical CPUs, 7.2 GiB RAM / 5.8 GiB available, 4 GiB swap unused; root filesystem 203 GiB available (9% used) |
| Runtime tools | Python 3.14.4, systemd 259.5; no Node/npm on system PATH |
| Web/exposure | No nginx/caddy/apache/cloudflared executable, relevant system/user unit or proxy/tunnel configuration directory found; no Docker/Podman binary found |
| Network | Private RFC1918 WLAN with private default gateway; no public ingress/NAT/CGNAT status established |
| Listeners | SSH TCP 22 on all interfaces; DNS 53 and CUPS 631 on loopback; UDP mDNS/chrony/DHCP. No TCP 80/443/8010/8080 listener |
| LIVE | No Polymarket/LIVE service/process name or `/opt/polymarket-btc-live` tree found here. Repository descriptions of an older LIVE host are not evidence for this host |
| Target DNS | Host resolver `getent ahosts demo-poly.dvirtechnologies.com` returned code 2/no address; not an authoritative DNS-zone audit |
| Firewall | UFW binary exists; `sudo -n ufw status verbose` requires interactive operator authentication. Status/rules remain unverified |
| Proposed port | **127.0.0.1:8010** (free at inspection/final check); tunnel-origin nginx 127.0.0.1:8080 also free |

No production env/config values or DB were opened. Relevant directories under
`/etc`, `/opt` and `/var/lib` remained absent on final inspection; listener inventory
was unchanged. No unit/system package was installed, service enabled/restarted, route
exposed, firewall altered, tunnel started, DNS changed or certificate issued.

## Proposed deployment

For this private-LAN home host, prefer a **new dedicated named Cloudflare Tunnel**
after domain/account approval. Browser HTTPS → hostname/Cloudflare edge → dedicated
cloudflared → nginx 127.0.0.1:8080 → static `/demo/` or unchanged API prefix → single
ASGI worker 127.0.0.1:8010 → private DEMO SQLite/WAL. Cloudflare edge enforces HTTPS;
local origin HTTP stays on loopback. No router forwarding is proposed for this mode.
The supplied tunnel configuration is not validated against a live Cloudflare account.

An alternative nginx direct-HTTPS profile is prepared, but public reachability,
router/NAT/firewall/DNS and certificate provisioning must be established first.
Do not install both profiles. Neither profile changes a LIVE route. No CORS or
browser WebSocket is needed. `/` redirects to `/demo/`; the hash router works on
refresh without returning HTML for missing JS or unknown API versions.

Production paths: `/opt/polymarket-demo-v2/{app,venv,frontend}` point through one
atomic `current` release link. Private state is
`/var/lib/polymarket-demo-v2/demo_v2.sqlite3`, env is
`/etc/polymarket-demo-v2/demo.env`, journald unit is `polymarket-demo-v2.service`.
One worker owns the DB, WAL/FK/busy-timeout checks remain unchanged. The private
provisioning and online backup tools are explicit operator commands, not startup
jobs, timers or development fixtures.

## Private provisioning required

- DEMO_AUTH_USERNAME, DEMO_AUTH_PASSWORD_HASH (new Argon2id hash of operator-entered
  same password if desired), fresh independent DEMO_SESSION_SECRET (>=32 chars).
- All nonsecret data paths/production flags/TTL/queue/stability values in
  `demo.env.example`; PUBLIC_DATA_ENABLED and COOKIE_SECURE must be true.
- Tunnel choice: scoped named-tunnel credential and domain/hostname control,
  privately provisioned for cloudflared only. No Cloudflare secret in DEMO env/Git.
- Direct HTTPS choice instead: valid proxy certificate/private TLS key and ingress;
  no Cloudflare credential required unless independently selected for DNS automation.
- **No Polymarket trading key, Binance/Chainlink API key, wallet/private trading
  key, authenticated CLOB credential or LIVE env is required.**

The helper never reads LIVE credentials, accepts no password CLI argument, writes
0600 exclusively, generates separate hash/secret each time, and emits no values.
App cookie, secret, DB/session version and revocation state remain DEMO-only.

## Focused security/isolation review

- Runtime import allowlist and temp-DB/env/network guards remain covered by tests;
  approved book/exact-trigger primitives only, no trading SDK/real-order route.
- Production preflight requires non-root service, Argon2id, independent configured
  session secret, Secure cookies and public-data mode; it opens no DB/network.
- Existing host-only HttpOnly Secure SameSite=Strict `/demo/` cookie and CSRF writes
  are retained; confirmed resets still require session + CSRF + exact confirmation.
- Nginx serves only build output, preserves API/cookie paths, overwrites client
  forwarding headers, limits request bodies/rate and disables API/auth caching.
  Only loopback is trusted by ASGI; tunnel client IP is trusted only from loopback.
- Code/venv/static releases are readonly/root-owned. State/runtime permissions are
  0700, UMask 0077; env root:root 0600. Unit denies home and known LIVE resource
  paths and writes only its state dir. Sandbox **startup** still needs verification
  on the actual future unit activation; only offline unit validation was authorized.
- Application request logs are disabled; the proxy log format omits query strings,
  bodies, cookies and credentials. Config/validation errors do not echo auth values.
- Reasonable scoped history review: **4 revisions / 47 file versions** under the
  new frontend/deploy paths, plus current source/template review; no literal private
  settings, private-key blocks or tracked real env file found. This is a focused
  deployment check, not a claim of an exhaustive repository vulnerability scan.

Fee state remains **UNSUPPORTED** for
`CURRENT_TIE_ROUNDING_AND_FILL_AGGREGATION_UNVERIFIED`; no runtime/fee/engine guard
was changed. With no held positions and all new-entry demand fee-blocked, the
existing subscription policy can leave order-book cards unavailable even while
catalog/research providers work. It must not be disguised as fresh data or fixed
by bypassing fee eligibility. `/health` is internal shallow liveness, `/ready` may
legitimately remain 503; authenticated health explains the actual blockers.

## Capacity and restart

The extracted production dependency venv was 23 MiB, static build 156 KiB. A short
real ASGI/TLS test showed about **71 MiB resident Python RSS** with tiny fixtures,
not a public-provider/history-load benchmark. Plan an initial 256–512 MiB app
budget (unit high/max), plus proxy/optional tunnel overhead and at least 1 GiB total
headroom; measure on first approved start. Four CPU threads and current free RAM
are sufficient for a bounded first rollout, not a sustained-load guarantee.

One shared public Market WS handles deduplicated token demand across rules; no
per-rule socket. Gamma/Binance use two shared HTTP clients, bounded discovery
concurrency, per-symbol research locks/cache and per-instance workers. Normal
Gamma family polling is 21/10s plus metadata/fees/resolution, and cached research
trade requests roughly 7/2s plus startup/baselines/retries; actual rates/bandwidth
vary with provider latency and held instances. No long outbound soak was run here.
Open sockets/files are shared and capped by LimitNOFILE=4096. Read-model RAM and
SQLite/history/backups can grow; there is no automatic research pruning. Monitor
journal, RSS/CPU, FD count and DB/WAL/backup disk growth after launch.

Cold restart tests cover rules, open positions, quotas, new durable generation,
rediscovery, fresh-only decisions, clean workers and a new stability interval.
Old snapshots/missed SL or entry events are not replayed. Backup tests retain
committed WAL data and verify a separate consistent copy without modifying history.
Rollback is a tested-release switch preserving DB, conditional on schema/unit
compatibility; incompatible restoration requires separately reviewed backup restore.

## Validation actually performed

| Check | Result |
|---|---|
| Full backend suite | **206 passed**, zero failures/skips (200 baseline + 6 deployment/restart tests) |
| Frontend Node tests | **14 passed** |
| Typecheck/build | Strict API/model JSDoc check + UI syntax; production build passed; no existing lint task |
| Real Chrome E2E | **3 passed** against the newly installed isolated production dependency venv |
| Fresh production venv | Pinned closure installed on Python 3.14.4, `pip check` passed; no system/LIVE env used |
| Actual nginx | Ubuntu candidate 1.28.3-2ubuntu1.12 downloaded/extracted under `/tmp`, not installed |
| Proxy/TLS smoke | **16 passed**, real nginx + temporary trusted test TLS + ASGI, fake credentials/temp DB; both profiles syntax-checked |
| systemd unit | `systemd-analyze verify` passed on a workspace copy with executable/working paths substituted to existing temporary resources; no unit activation |
| Shutdown/scope | All test processes stopped; candidate/test ports closed; production directories absent; changes only under `demo_v2/` |

Reproduce the extra smoke after building frontend, using a user-extracted nginx and
mime.types (not an installed/active nginx config):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:polymarket-collector /path/to/demo-venv/bin/python demo_v2/tests/verify_deployment.py --nginx /path/to/extracted/usr/sbin/nginx --mime-types /path/to/extracted/etc/nginx/mime.types --output /tmp/demo-deployment-smoke.json
```

## Conditions before actual deployment

1. Separately approve installation/start and select Tunnel vs direct HTTPS.
2. Provision domain/exposure/TLS as selected; establish usable public hostname.
3. Obtain administrator inspection of UFW/routing, approve new packages/account/
   directories/units, and recheck service/port conflicts at execution time.
4. Provision independent private DEMO auth/env; never source/copy LIVE secrets.
5. Execute the runbook with a reviewed SHA/backup/rollback point, then verify actual
   systemd sandbox, public providers, cookie/browser flow and resource behavior.

The known fee-entry block is deliberately retained during deployment. No further
feature or Phase work was started; these conditions do not authorize execution.
