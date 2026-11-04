# DEMO: develop on host, run in isolated KVM

Status: **prepared; actual installation/migration must be verified**, not inferred
from this document. The installed source before migration is release
`14c96ac7c6ef40e74e58492a6f2a207d9b69e445`. Future runtime truth is the actual
guest plus root-owned host migration journal, not this historical SHA.

## Boundaries

- Physical host `dvir-technologies`: development checkout, tests with fake/temp
  dependencies, source builds and pinned-key SSH management. Existing nginx only
  relays `127.0.0.1:8080` to guest `172.31.250.2:8080` after cutover. Existing tunnel,
  public DNS/router exposure and LIVE are unchanged.
- Dedicated QEMU/KVM guest `polymarket-demo-vm`: 2 vCPU, 1536 MiB RAM, 16 GiB virtual
  disk, Ubuntu 26.04 official cloud image verified against Canonical's signed sums.
  No shared folders, physical bridge, USB passthrough, display server or host keys
  inside the VM. QEMU itself runs as an unprivileged dedicated host service user.
- Guest app: same dedicated `polymarket-demo-v2.service`, paths `/opt/polymarket-demo-v2`,
  `/etc/polymarket-demo-v2/demo.env`, `/var/lib/polymarket-demo-v2/demo_v2.sqlite3`.
  API binds guest loopback 8010; guest nginx serves frontend/API at loopback and
  private guest IP 8080. Host is the only initiator allowed to reach guest services.
- Host-enforced nftables: private bridge/TAP only; deny guest initiation toward
  host, private/LAN/link-local/multicast networks, observed home public IPv4,
  spoofed source addresses and IPv6. Deny unsolicited incoming forwarding. Allow
  public TCP 80/443 for packages/data, 465/587 for existing TLS mail, UDP 123 for
  clock, and DNS only through the restricted host relay. Guest root cannot change
  host enforcement. This is containment, not immunity to hypervisor vulnerabilities.
- Guest DNS uses the host resolver, preserving the existing ISP configuration.
  The saved home-public-IP deny value must be refreshed/reviewed if the ISP address
  changes; never remove that block to fix guest connectivity.
- VM resource cards measure the guest's CPU/RAM/disk/NIC. Monitor the physical
  host separately. VM/network containment does not isolate the shared ISP bandwidth.

## Initial migration (explicit task authorization required)

Use the prepared, checksummed bundle emitted by:

```sh
python3 demo_v2/deploy/vm/controller.py prepare-migration \
  --release-bundle /path/to/reviewed/current-release-bundle \
  --image-dir /path/to/verified/ubuntu-image-and-signed-sums \
  --output /path/to/new/migration-bundle
```

Then run its printed `sudo bash /path/to/migration-bundle/migrate-demo.sh` in the
operator's Ubuntu terminal. This is Ubuntu administrator authentication, not the
DEMO password. The command installs only QEMU/cloud-image prerequisites, repeats
real packet tests in disposable namespaces, creates the dedicated units/network,
boots the guest and verifies containment from guest root using pinned SSH before
the application moves. It installs official guest OS updates, reboots the empty
guest and rechecks pinned SSH/containment before copying production state. No host
sudoers exception is installed.

The root orchestrator then provisions guest code/interpreter/nginx while the host
writer continues. It rechecks the expected release/proxy, makes online and stopped
WAL-aware backups, installs a persistent host writer boot guard and disables/stops
the old app. Only then does it transfer the final DB and opaque private DEMO env
over pinned SSH and start the guest writer. Root-private migration staging/journal
is retained for recovery; no secret-bearing bundle enters Git or operator output.

Verification compares original IDs, session/auth fingerprints, notification
preferences and serial mappings/high-water in memory; fingerprints are never
printed. After guest liveness/readiness/static hashes/auth denial pass, host nginx
switches to the guest. Root containment checks run again. Success marker:
`DEMO_VM_MIGRATION_PASS`, journal phase `COMPLETE`. Verify normal login, rules,
history, fresh observations and a rollover through the existing local URL too.
An additional root-owned mode-0644 `runtime-target.json` publishes only the target
identity/completion flag, allowing the unprivileged release controller to reject
deployments before cutover. It contains no private state or fingerprints.

If failure occurs after guest start, rollback must stop/disable the guest and
recover its latest committed DB before starting the old host writer. If guest
recovery cannot be verified, the host remains guarded/stopped. Never blindly
rerun an interrupted migration, delete its journal or start against the stale DB.
Inspect the journal and complete recovery first. Automatic code rollback for
ordinary releases is separate and never restores history.

A base-only failure before `migration-state.json` exists can be retried with a
newly checksummed, reviewed bundle. The installer stops only the managed QEMU/DNS
units before updating them, preserves the guest disk/seed/SSH identity and repeats
containment checks before transferring state. It refuses base reinstall once any
application migration journal or writer/target marker exists. DNS needs AF_NETLINK
for dnsmasq's interface enumeration; it still runs without CAP_NET_ADMIN, with the
same IP filters and firewall. Connectivity reports distinguish DNS, TCP and TLS.

For an inspected OS-bootstrap failure in phase `PROVISIONING`, prepare a new
bundle with `prepare-migration --resume-provisioning` and run its printed command.
It preserves the journal chain, requires the original host writer/release/proxy
and no cutover guards, and independently verifies a dedicated, empty guest before
restarting only QEMU with the checksummed unit. Existing guest app code, config,
DB, a running bootstrap job or any later migration phase blocks this recovery.
Never delete the journal to force a fresh migration.

The QEMU main-loop disk worker pool is explicitly capped at 16, below the total
`TasksMax=64` budget; the default worker pool alone can grow to 64. A 2026-11-04
bootstrap failed with `qemu_thread_create: Resource temporarily unavailable`
during initramfs generation (no memory-limit OOM), consistent with that conflict.
CPU/RAM/task limits and all network containment remain unchanged. Guest package
repair starts with `dpkg --configure --pending`, checks a clean package audit and
runs as a persistent, time-bounded guest systemd job. The controller verifies its
exit status and QEMU identity, then requires a changed guest boot ID and repeats
containment before provisioning code or moving any financial state. SSH loss
alone is never evidence of a successful update/reboot.

After a verified `ROLLED_BACK`, the host is again the current writer. A reviewed
`prepare-migration --retry-rolled-back` bundle must match both the restored host
release and the retired guest release. It preserves the journal chain, requires
the guest writer disabled/stopped, archives its old DB/WAL/SHM/config privately,
and provisions a fresh committed release without rerunning OS bootstrap. It
reuses only the exact root-owned host cutover condition created earlier. Fresh
host backups/state are taken at the new cutover; the stale guest copy is never
reused as current truth. Other journal phases still require separate recovery.

Public release directories are explicitly traversable (0755), and code/static
files readable (0644, existing executable files 0755), regardless of root's
umask 0077. The shared release-permissions helper covers initial guest install and
future code updates. It does not follow symlinks or change private env/DB/backups.
Before stopping the host writer, startup/decision-child checks use disposable
state as the actual app user from the release app directory; nginx file access
is checked as `www-data`. This catches the 2026-11-04 `200/CHDIR` failure that the
previous root-only check missed.

Relay cutover is verified from actual HTTP responses, not `systemctl reload`
returning: nginx reload signals its master asynchronously. The new host relay
adds `X-Demo-Runtime-Target: polymarket-demo-vm` to every response; an identical
old frontend cannot satisfy the gate. Check the private guest frontend/API from
the host before switching, then allow at most 30 seconds for the new relay plus
matching frontend and unauthenticated API 401. Only old-route/connection and
502/503/504 failures are retried. Unexpected auth success or wrong artifact bytes
fail immediately; persistent transport errors still trigger state-preserving
rollback. Repeat guest readiness/PID/release/private-state checks after routing
and containment pass before writing COMPLETE.

## Every future authorized fix/release

1. Read `demo_v2/AGENTS.md`; inspect Git/actual guest state. Preserve other chats'
   changes. Implement only in source, and run relevant temp/fake tests on the host.
2. Commit and push the exact intended revision. Deploy committed source; uncommitted
   working files, host credentials and host databases never enter release bundles.
3. From that source checkout, run (unique output folder; no host sudo):

   ```sh
   python3 demo_v2/deploy/vm/controller.py deploy \
     --sha <full-commit-SHA> --output /tmp/demo-release-<short-SHA>
   ```

   Node is found on PATH or the Codex bundled runtime; `--node /path/to/node` is
   available. Production wheels are downloaded from the pinned dependency closure
   using a disposable build venv, or `--wheels /verified/wheel-cache` can supply
   them. Guest offline install and `pip check` still validate compatibility.
4. The controller verifies guest identity and pinned host key, reads the guest's
   current release, builds source/frontend from the requested commit, uploads via
   SSH and runs `update_release.py` inside the guest. The current schema-compatible
   updater performs disposable child startup, backup, promotion, preservation checks
   and rollback. Schema changes require separate reviewed migration work.
5. Check guest service/owner PIDs and restarts, expected release, `/health`, `/ready`,
   unauthenticated API rejection, then normal authenticated UI/market queues. The
   root journal describes initial migration; the actual guest `current` symlink
   describes later deployed revisions. READY/DEGRADED are scoped, not a guarantee
   of an executed trade in each of the 21 definitions.

Host old wrappers under `work/demo-deployment-*` are historical artifacts after
cutover. Never use them to update or restart a host financial writer.

## Read-only operational commands

```sh
systemctl show polymarket-demo-vm.service -p ActiveState -p MainPID -p NRestarts
systemctl show polymarket-demo-v2.service -p ActiveState -p UnitFileState
curl --fail --silent http://127.0.0.1:8080/demo/ >/dev/null
```

The management SSH options are centralized in `controller.py`; use that exact
identity, HostKeyAlias and strict known-hosts file for guest operations. Do not
dump env files, cookie/session values, SSH keys, SMTP credentials, private DB
metadata or raw controller `guest.py verify` output. That internal output contains
comparison fingerprints and is intended only for captured migration verification.

## Evidence and sources

Preparation: 16 packet tests plus 3 network-initializer checks in disposable
network namespaces. Temp/fake migration tests cover state/settings/serial
preservation, manifest validation and refusing stale-host recovery when SSH fails.
Actual VM boot, live containment and migration checks remain required.

This adapts the already-prepared `work/demo-vm-stage3` network/QEMU implementation
rather than introducing an unrelated hypervisor or bridging into the home LAN.

- [Ubuntu official cloud-image/libvirt guidance](https://ubuntu.com/docs/public-images/public-images-how-to/launch-with-libvirt/)
- [libvirt NAT semantics](https://libvirt.org/formatnetwork.html): NAT alone permits
  outbound access to the LAN/host; additional host-enforced rules are intentional.
- [systemd service sandbox controls](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [QEMU event-loop worker pool defaults](https://www.qemu.org/docs/master/interop/qemu-storage-daemon-qmp-ref.html#object-EventLoopBaseProperties)
