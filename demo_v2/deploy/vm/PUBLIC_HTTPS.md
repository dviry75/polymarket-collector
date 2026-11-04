# Direct HTTPS ingress, explicitly authorized 2026-11-04

Initial activation and trusted TLS/login were verified on 2026-11-04; see
`../../docs/PUBLIC_HTTPS_VERIFICATION.md` for evidence and external-check limits.
Repeat actual runtime checks instead of relying on that historical record.

Target: `https://demo-poly.dvirtechnologies.com/demo/`. DNS remains at its existing
provider; no Cloudflare delegation or DNS API key is needed. This runbook records
the prepared design, not evidence that router forwarding/certificate issuance or
external access has succeeded. Check current service, TLS and actual external
responses before claiming completion.

## Traffic and ownership

Router TCP 443 → host `192.168.33.24:443` → dedicated
`polymarket-demo-https.service` (Caddy) → existing guest nginx
`172.31.250.2:8080` → unchanged isolated DEMO app. The existing host nginx
`127.0.0.1:8080` continues to provide local access independently. Only `/demo/*`
is proxied; `/` and `/demo` redirect to `/demo/`, other paths return 404.

Caddy listens on that one IPv4 TCP socket. HTTP/3/UDP, HTTP redirects/port 80 and
its admin listener are disabled. Let's Encrypt TLS-ALPN-01 performs issuance and
automatic renewal on TCP 443; no DNS-01 secrets, self-signed browser certificates
or TLS validation bypass. Certificates/account keys live privately under its own
systemd state directory `/var/lib/polymarket-demo-https`, separate from DEMO auth.
Forwarded client-IP headers are replaced before the guest's trusted-proxy hop.

The service uses a dedicated dynamic user, readonly system/home isolation, only
CAP_NET_BIND_SERVICE, bounded CPU/RAM and its own writable state. It cannot read
the VM disk, guest DB/env or operator home/SSH key. No guest network policy,
bridge, incoming VM forwarding, LIVE or app/service/financial state changes.
Internet exposure is still public access to the authenticated DEMO; this is not
a claim of immunity to exploits or shared ISP bandwidth exhaustion.

## Prepare, inspect, authenticate, install

`public_https.py prepare --output <new-folder>` creates a manifest-checked bundle
from the current source. It downloads the pinned Caddy 2.11.8 official package,
checks its signed repository/index and package hashes, and exports the exact
config/unit/installer. `--cache <verified-previous-preparation>` avoids downloading
again; all cached artifacts are verified again. Never add package/private-key
bundles, real certificates, env or DB to Git.

Test the actual extracted Caddy binary's adapted config before install: only
`192.168.33.24:443`, `h1/h2`, admin disabled, HTTP challenge disabled, fixed guest
upstream. Run `test_public_https.py` and systemd unit validation with fake/temp
dependencies. Commit/push the intended source before applying its bundle.

After explicit user authorization for exposure, run the bundle's `apply.py apply
--bundle <folder> --manifest-sha256 <reviewed-digest>` as host root (sudo or pkexec
with normal Ubuntu administrator authentication). It verifies cutover, active VM,
stopped host writer, the unchanged VM guard and existing local API auth denial,
then recopies/reverifies artifacts into root-private staging before installation.
The package's generic default units are masked to prevent an unintended default
site or port 80 listener. Only the dedicated unit is enabled. Its signed APT source
is scoped and pinned to Caddy for future normal package upgrades. After upgrading
Caddy, restart only `polymarket-demo-https.service`, then recheck TLS/auth.

If UFW is already active, only an inbound TCP443 rule on `wlp3s0` to the host IP
is added. An inactive UFW is left inactive; the script never flushes/replaces
firewalls. Existing guest isolation must be byte-identical excluding counters.
Startup failure disables only the new HTTPS unit and removes its added UFW rule;
installed package/masked generic units/config are retained for inspection. Reruns
refuse existing installation paths; do not overwrite a partially completed setup.
The root-owned `installation.json` initially records certificate/external checks
as **pending** even when the HTTPS daemon is running.

## Router and completion gates

Reserve `192.168.33.24` for host Wi-Fi MAC `54:13:79:36:6b:cf` in DHCP. Add one
TCP-only WAN 443 → `192.168.33.24:443` rule. No DMZ, UPnP, WAN router management,
80, 22, 8010, 8080, 8097 or guest management forwarding. Inspect existing mappings
and conflicts before saving. Router UI changes that materially expose access
require confirmation at the final action under the computer-use policy.

Check current A record against observed router/public IPv4 and guest home-public-IP
deny set; never publish an AAAA record without a separately reviewed IPv6 boundary.
Verify a trusted, unexpired public certificate for the exact hostname, frontend
bytes/guest marker, root redirect, unknown-path rejection and unauthenticated API
401. Verify normal HTTPS login and authenticated reads without printing cookies,
CSRF, password or TLS keys. Check the source IP reaching auth cannot be spoofed
through forwarding headers. Check only expected listeners and guest service/PIDs,
host writer stopped, unchanged local access and VM containment.

NAT hairpin tests from the home network do not establish external reachability.
Use an independent public request or a user cellular check. ACME successful
TLS-ALPN validation provides external TCP443 evidence for issuance, but still
verify the actual application over HTTPS. Report ISP/router/hairpin limitations
plainly; never claim completion from a running daemon or DNS record alone.

For ordinary future DEMO fixes, continue the existing `controller.py deploy`
workflow into the guest. Host HTTPS stays in place and needs no app redeployment.
To close this public ingress, remove its single router NAT rule and stop/disable
only `polymarket-demo-https.service`; local DEMO/VM writer remain untouched.

References: [Caddy official install](https://caddyserver.com/docs/install),
[Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls),
[Let's Encrypt challenges](https://letsencrypt.org/docs/challenge-types/).
