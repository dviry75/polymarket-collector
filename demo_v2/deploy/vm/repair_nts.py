"""Apply the reviewed public NTS egress repair without restarting the DEMO VM.

Host administrator authentication is required. Only the dedicated firewall
renderer/table is changed; the existing initializer applies an atomic nft batch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


LIB = Path('/usr/local/lib/polymarket-demo-vm')
CONF = Path('/etc/polymarket-demo-vm')
OLD_PORTS = 'tcp dport {{ 80, 443, 465, 587 }}'
NEW_PORTS = 'tcp dport {{ 80, 443, 465, 587, 4460 }}'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_change(old, new):
    if old.count(OLD_PORTS) != 1:
        raise ValueError('Unexpected installed egress policy')
    # Permit only this port-list edit and explanatory full-line comments.
    meaningful = lambda value: [line for line in value.splitlines() if not line.lstrip().startswith('#')]
    if meaningful(old.replace(OLD_PORTS, NEW_PORTS)) != meaningful(new):
        raise ValueError('Repair contains unrelated firewall changes')


def replace_public_file(path, data):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_policy(target, initializer, old, new, run):
    replace_public_file(target, new)
    try:
        run('/usr/bin/python3', '-B', str(initializer))
    except BaseException:
        replace_public_file(target, old)
        run('/usr/bin/python3', '-B', str(initializer))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--installed-sha256', required=True)
    parser.add_argument('--initializer-sha256', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Host administrator authentication required')
    marker = CONF / 'runtime-target.json'
    if marker.is_symlink() or marker.stat().st_uid != 0 or marker.stat().st_mode & 0o022:
        raise RuntimeError('Untrusted VM target marker')
    if json.loads(marker.read_text()) != dict(status='COMPLETE', target='polymarket-demo-vm',
            guest_ipv4='172.31.250.2', host_writer_disabled=True):
        raise RuntimeError('Verified VM cutover is required')
    run = lambda *command: subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
    if run('systemctl', 'is-active', 'polymarket-demo-vm.service') != 'active':
        raise RuntimeError('Dedicated VM is not active')
    if run('systemctl', 'show', 'polymarket-demo-v2.service', '-p', 'MainPID', '--value') != '0':
        raise RuntimeError('Retired host writer must remain stopped')
    target, initializer = LIB / 'firewall.py', LIB / 'network.py'
    old, new = target.read_bytes(), args.source.read_bytes()
    if (digest(old) != args.installed_sha256 or digest(new) != args.source_sha256
            or digest(initializer.read_bytes()) != args.initializer_sha256):
        raise ValueError('Reviewed artifact or installed policy changed')
    validate_change(old.decode(), new.decode())
    backup_dir = Path(tempfile.mkdtemp(prefix='demo-nts-policy-', dir='/var/tmp'))
    backup_dir.chmod(0o700)
    (backup_dir / 'firewall-before.py').write_bytes(old)
    apply_policy(target, initializer, old, new, run)
    print('DEMO_PUBLIC_NTS_EGRESS_APPLIED; VM/app not restarted; private-network guards retained')


if __name__ == '__main__':
    main()
