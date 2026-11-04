# Direct public HTTPS — verified 2026-11-04

User explicitly approved the final router save. One enabled rule `DEMO-HTTPS`:
TCP WAN443 → `192.168.33.24:443`, NAT loopback enabled. Existing DHCP reservation
already matched host Wi-Fi MAC/address; unchanged. DMZ remained disabled and no
other mapping was added. DNS A matched observed public IPv4 `95.86.124.98`; no
DNS/AAAA/provider changes were made.

Host ingress source: `9e6e190e7c2e96e9804d91fd8816dd6b8b9fc7d3`.
Installed signed official Caddy 2.11.8 via reviewed manifest
`f9f06ee72048dfa1b083b74e69826ffa8bebfa65731450feaadc55d2e7095ffd`.
`polymarket-demo-https.service` active, PID1523574, NRestarts0, about 18 MiB.
Only new listener: IPv4 `192.168.33.24:443/TCP`; no 80/UDP443/admin listener.
UFW was inactive and was left inactive; VM guard stateless rules were unchanged.
The package's generic units are masked; only the dedicated service is enabled.

Let's Encrypt completed external TLS-ALPN-01 validation and issued a publicly
trusted certificate (issuer YE2, expiry 2027-02-02 18:42:55 UTC). Python's normal
CA/hostname validation passed with TLS1.3; no insecure TLS flags or local CA.
Certificate issuance/renewal is managed by Caddy over TCP443. Early pre-forwarding
ACME failures recovered automatically after the rule was saved. A redundant
pending administrator restart was canceled; ingress/app were not restarted.

The public hostname via home NAT loopback returned frontend200 with the VM marker,
identical frontend bytes to existing local access, API401 without authentication
and 404 for internal/unrelated paths. Normal browser admin login over public HTTPS
succeeded; authenticated dashboard/API polling loaded 13,932 trades, matching KPI,
with no API error alerts. These counts continue changing with simulated trading.
Provider health remained scoped DEGRADED; no quote/entry guard was relaxed.

External ACME validation proves public443 reachability at issuance. Independent
web-fetch/check-host tools could not retrieve an external HTTP report; do not
describe the application HTTP tests as originating outside the home network.
For future routing changes, repeat an independent HTTP/cellular check as well.

Guest runtime stayed `ce91284e095ecf27677a00c009f713f689f73ca3`, PID15238,
NRestarts0; QEMU PID1295590 unchanged. Retired host writer stayed inactive/PID0.
Guest time remained synchronized. No guest DB/auth/settings/service restart or
LIVE change. Existing local URL remains available. No secrets in source/evidence.
The root installation JSON is a bootstrap record and retains its initial
verification-pending value; use current TLS/service/API checks as runtime truth.

Validation: six isolated ingress artifact tests; actual signed-binary config and
systemd-unit validation; disposable real-Caddy/fake-backend checks for redirects,
DEMO-only routing, API401 propagation and replacement of spoofed forwarding headers.
The ordinary VM deployment workflow remains unchanged; see
`../deploy/vm/PUBLIC_HTTPS.md` for ingress ownership/maintenance/closure.
