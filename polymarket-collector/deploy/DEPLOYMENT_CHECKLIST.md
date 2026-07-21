# Polymarket LIVE Future Deployment Checklist

No deployment was performed while creating this file.

## Target

- VM: `polymarket-btc-tst`
- User: `dvir`
- App path: `/opt/polymarket-btc-live`
- Venv: `/opt/polymarket-btc-live/.venv`
- DB: `/opt/polymarket-btc-live/poly_live.sqlite3`
- Backups: `/opt/polymarket-btc-live/backups`
- Service: `polymarket-live.service`
- Port: `8001`
- Domain: `live-poly.dvirtechnologies.com`
- Non-secret env: `/etc/polymarket-live/live.env`

## Steps

1. Create `/opt/polymarket-btc-live` owned by `dvir:dvir`.
2. Create `/etc/polymarket-live/live.env` from `deploy/live.env.example`; keep only non-secret values there.
3. Store `LIVE_LOGIN_PASSWORD_HASH`, `LIVE_SESSION_SECRET`, Polymarket keys and API credentials in Google Secret Manager.
4. Install Python dependencies into `/opt/polymarket-btc-live/.venv`.
5. Copy `deploy/polymarket-live.service` to systemd and run `systemctl daemon-reload`.
6. Configure nginx with `deploy/nginx-live-poly.conf`.
7. Run certbot for `live-poly.dvirtechnologies.com`.
8. Confirm firewall allows 80/443 and does not expose 8001 publicly.
9. Start LIVE only after DEMO is confirmed healthy.
10. Verify public `GET /health` returns only `{"status":"ok"}` or `{"status":"degraded"}`.
11. Login to `/live` and verify detailed health privately.
12. Run only dry-run and read-only checks. Do not create a Live Rule or enable real trading.

## Rollback

1. Keep DEMO service running throughout.
2. Stop `polymarket-live.service`.
3. Set `LIVE_MODULE_ENABLED=false` and `LIVE_KILL_SWITCH=true`.
4. Preserve `poly_live.sqlite3` and backups for audit.

## Journald

Use journald retention of 14 days or 500MB, whichever comes first. Do not log private keys, API secrets, passphrases, cookies, raw private WebSocket auth payloads, or session secrets.
