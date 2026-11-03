# DEMO email notifications

Implemented in `notifications.py`, authenticated `/demo/api/v1/notifications`
routes and the Hebrew **התראות במייל** panel in maintenance. This is a separate
DEMO mechanism, with no LIVE imports, environment fallback, state or SMTP reuse.
The existing LIVE reporting sender was inspected as a reference only; its
trading-runtime imports make direct reuse inappropriate.

## Choices

The shared workspace can configure up to 10 bare email recipients, an enable
switch, a Jerusalem daily time and a 1/3/6-hour detailed report interval.
Each recipient receives a separate envelope. The sender is operator-controlled,
shown read-only in the UI, and defaults to `demo@dvirtechnologies.com`.

| Category | Code | Initially selected |
| --- | --- | --- |
| Sustained system fault | SYSTEM_FAULT | Yes |
| Stable recovery | SYSTEM_RECOVERY | Yes |
| Daily report | DAILY_SUMMARY | Yes |
| Detailed report every 1, 3 or 6 hours | PERIODIC_SUMMARY | Yes; default 3 hours |
| Host network throughput | NETWORK_HIGH | No |
| Host CPU usage | CPU_HIGH | No |
| Host RAM usage | RAM_HIGH | No |
| Disk usage | DISK_HIGH | No |
| Simulated entry | POSITION_OPENED | No |
| Take-profit exit | TAKE_PROFIT | No |
| Stop-loss exit | STOP_LOSS | No |
| Official market resolution | MARKET_RESOLUTION | No |

Settings and server delivery are both **disabled by default**. No recipients
are pre-populated. Changes start from the save point: previous trades/reports
are not backfilled. A daily report covers the Jerusalem calendar day ending at
the selected time. Periodic reports use Jerusalem wall-clock boundaries (e.g.
00:00/03:00/06:00 for a 3-hour interval). Only the latest completed period is
reported after a missed schedule; missed reports are not replayed. DST offsets
are included in report timestamps. Each report includes period entries/exits,
closed-position net P&L, action fees, current open count, up to 30 detailed trades
and current aggregate component health. A longer report points to the UI for the
remaining rows. Reports are research summaries, not live-money statements.

Faults must persist for 30 seconds; recovery must remain ready for 10 seconds.
Only aggregate components are monitored, avoiding an email for each individual
token/family. Repeated identical fault samples do not create repeated alerts.
The in-process worker cannot email while the complete app/host/network is down;
that requires a separately configured external uptime monitor. This feature
reports faults it can still observe while the DEMO process is running.

## Delivery and persistence

The worker observes committed ledger rows, not price telemetry. Cursor and queue
insertion are committed together in the existing `demo_v2_meta` table under
`notification_settings`, `notification_state`, `notification_queue`. No schema
migration is needed. SQL values use bound parameters. The queue is bounded at
500 per-recipient deliveries; terminal history is evicted first. Overflow is
counted and visible in the UI. Pending mail expires after 24 hours.

SMTP runs in a background thread, outside the trading decision path. TLS uses
certificate verification. Settings updates cancel pending old-preference mail.
Resetting trading data removes queued/history research notification payloads in
the same reset transaction, retaining settings and system/test notification data.
An already in-flight or externally delivered message cannot be recalled.

`SENT` means the SMTP server accepted the message, **not** that it reached the
inbox. Known connection refusal can retry at most 3 times. Rejected credentials
or recipients fail without looping. An uncertain SMTP result, or an interrupted
in-flight send found after restart, is `UNKNOWN` and is **not automatically
resent**. A deterministic Message-ID helps diagnosis but does not guarantee
exactly-once inbox delivery. No raw SMTP exception or credentials enter the API.

## API

- `GET /demo/api/v1/notifications`: preferences, revision, sender, transport gate,
  selectable catalog, queue counts and latest 30 delivery status records.
- `PUT /demo/api/v1/notifications`: `{settings, expected_revision}`. Authenticated
  cookie and CSRF are required; stale revisions fail. No SMTP secret/host is
  accepted through this endpoint.
- `POST /demo/api/v1/notifications/test`: explicit test to saved recipients only,
  requiring enabled settings, enabled server transport and CSRF; at most one test
  request per minute. The message is queued, not sent during the request.

The UI preserves unsaved inputs during background health refreshes. SMTP
configuration belongs in a private DEMO-only environment, never browser storage.

## Google Workspace relay setup

User confirmed that `demo@dvirtechnologies.com` exists and the sending IP has
already been configured in Workspace. DNS and an EHLO/STARTTLS-only probe were
checked on 2026-11-03 (Jerusalem); no email or MAIL/RCPT command was sent.

Observed public DNS:

- MX: `1 smtp.google.com.`
- SPF: `v=spf1 include:_spf.google.com ~all`
- A DKIM RSA key exists at `google._domainkey.dvirtechnologies.com`. This does
  not by itself prove outbound signing is enabled or alignment succeeds.
- `_dmarc.dvirtechnologies.com` returned NXDOMAIN. A DMARC record was not found.
- NS records use `ns-cloud-a*.googledomains.com`; no DNS provider move is needed.

The server reached `smtp-relay.gmail.com:587`; EHLO returned 250, STARTTLS 220,
and EHLO over TLS 250. This proves network/TLS reachability, **not** relay
authorization or final delivery. Those require a test to an explicitly chosen
recipient after deployment.

Proposed private DEMO settings for the already IP-authorized relay:

```dotenv
DEMO_EMAIL_ENABLED=true
DEMO_EMAIL_FROM=demo@dvirtechnologies.com
DEMO_SMTP_HOST=smtp-relay.gmail.com
DEMO_SMTP_PORT=587
DEMO_SMTP_SECURITY=STARTTLS
```

Do not copy LIVE SMTP environment. With IP authorization, username/password
should remain absent unless the Workspace administrator specifically requires
SMTP AUTH. Optional `DEMO_SMTP_USERNAME` and `DEMO_SMTP_PASSWORD` must be provided
together via the private DEMO configuration. No plaintext credentials belong in
source, chat, screenshots or frontend settings.

Remaining operational checks:

1. Verify that the allowlisted address is the server's actual outbound public IP,
   the Workspace relay allows the intended sender/domain, and TLS is required.
2. Verify DKIM signing is active in Workspace; an existing public key is only
   half the check.
3. Add DMARC at the current authoritative DNS provider. A staged initial policy
   can be `v=DMARC1; p=none;`, then review real headers/reports before tightening.
   Do not overwrite the current SPF or create a second SPF record.
4. Deploy the reviewed code and enable the private DEMO settings through a
   separately authorized service update. Save recipient preferences in the UI.
5. Send a requested test and inspect inbox/spam plus Authentication-Results for
   SPF/DKIM/DMARC alignment. Acceptance by SMTP alone is insufficient.

References:

- [Google Workspace application SMTP and relay options](https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app)
- [Google sender requirements](https://support.google.com/mail/answer/81126)
- [Google DMARC configuration](https://knowledge.workspace.google.com/admin/security/set-up-dmarc)

No DNS, mailbox, relay, service, firewall or production data was modified during
implementation. No real email has been sent by this task.

## Optional resource alerts

Reuses the same ResourceMonitor snapshot displayed in system details; no new
network probes or collectors. Each metric is independently opt-in. Defaults are
90% host CPU/RAM/disk usage, and 10 MiB/s combined incoming + outgoing host
network traffic. These are editable absolute thresholds, not learned anomalies
or a measurement of the Internet subscription speed. All thresholds and the
sustained duration (default 60 seconds, configurable 10–3600) are validated.

One alert per metric per incident is queued. Rearming requires 120 seconds
continuously below 80% of that metric's threshold. Missing, invalid or samples
older than 15 seconds break continuity; repeated snapshots do not advance it.
Restart or preference updates restart observation. No resource alert changes
trading readiness. CPU/RAM refer to the host, disk to the DEMO data filesystem;
network includes other programs and virtual interfaces, excluding loopback.

### SMTP test on 2026-11-03

The user authorized one test to dvir@dvirtechnologies.com. Google rejected MAIL
FROM with 550 5.7.1 Invalid credentials for relay, identifying outbound IP
164.138.114.14. No message DATA was submitted. A second attempt with the sender
domain as EHLO was rejected at the same stage. Verify that this exact outbound
IP is authorized in the correct Workspace tenant's SMTP relay service, that
dvirtechnologies.com is registered there, and that the intended sender is
allowed. No SMTP credentials were requested or read; no private config or
services were changed. SMTP reachability alone does not prove relay permission.

After the user updated the IP allowlist, a fresh authorized test from
`demo@dvirtechnologies.com` to `dvir@dvirtechnologies.com` was accepted by Google
SMTP on 2026-11-03, using STARTTLS and EHLO `dvirtechnologies.com`. This resolves
the relay rejection for this test; inbox receipt and DKIM/DMARC results are not
yet verified. The sender now identifies its domain in EHLO and adds a Date header.
No deployment, service restart or private environment changes were performed.
