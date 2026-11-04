"""Explicit host HTTPS ingress preparation/application; never touches guest state.

Only run apply after authorization to expose the DEMO hostname publicly. The
router must forward TCP 443 to 192.168.33.24:443 separately. No router, DNS,
VM firewall, application, DB, credential or existing nginx changes are made here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request


HOST = 'demo-poly.dvirtechnologies.com'
LAN = '192.168.33.24'
VERSION = '2.11.8'
OFFICIAL = 'https://dl.cloudsmith.io/public/caddy/stable/'
SERVICE = 'polymarket-demo-https.service'
FILES = {'Caddyfile', SERVICE, 'apply.py', 'caddy.deb', 'keyring.gpg', 'InRelease', 'Packages'}
SAFE_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, env=SAFE_ENV).stdout.strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def package_record(index):
    for block in index.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in block.splitlines()
                      if ': ' in line and not line.startswith(' '))
        if fields.get('Package') == 'caddy' and fields.get('Version') == VERSION:
            return fields
    raise ValueError('Reviewed official Caddy version missing')


def verify_package(folder):
    run('gpgv', '--keyring', str(folder / 'keyring.gpg'), str(folder / 'InRelease'))
    index = (folder / 'Packages').read_bytes()
    expected = [sha(index), str(len(index)), 'main/binary-amd64/Packages']
    if not any(line.split() == expected for line in (folder / 'InRelease').read_text().splitlines()):
        raise ValueError('Signed repository index mismatch')
    package = package_record(index.decode())
    if sha((folder / 'caddy.deb').read_bytes()) != package['SHA256']:
        raise ValueError('Official package digest mismatch')
    for key, value in [('Package', 'caddy'), ('Version', VERSION), ('Architecture', 'amd64')]:
        if run('dpkg-deb', '-f', str(folder / 'caddy.deb'), key) != value:
            raise ValueError('Unexpected package identity')


def prepare(output, cache=None):
    output.mkdir(parents=True)
    def download(name, relative):
        if cache and (cache / name).is_file():
            shutil.copyfile(cache / name, output / name)
        else:
            with urllib.request.urlopen(OFFICIAL + relative, timeout=60) as response:
                (output / name).write_bytes(response.read())
    download('InRelease', 'deb/debian/dists/any-version/InRelease')
    download('Packages', 'deb/debian/dists/any-version/main/binary-amd64/Packages')
    download('repo-key.asc', 'gpg.key')
    run('gpg', '--batch', '--yes', '--dearmor', '--output', str(output / 'keyring.gpg'), str(output / 'repo-key.asc'))
    (output / 'repo-key.asc').unlink()
    record = package_record((output / 'Packages').read_text())
    download('caddy.deb', 'deb/debian/' + record['Filename'])
    verify_package(output)
    here = Path(__file__).resolve().parent
    shutil.copyfile(here / 'https.Caddyfile', output / 'Caddyfile')
    shutil.copyfile(here / SERVICE, output / SERVICE)
    shutil.copyfile(Path(__file__), output / 'apply.py')
    manifest = {'purpose': 'DEMO_HTTPS_443_ONLY', 'domain': HOST, 'lan_ipv4': LAN,
                'caddy_version': VERSION, 'files': {name: sha((output / name).read_bytes()) for name in sorted(FILES)}}
    (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    print('Prepared signed official Caddy and dedicated TCP-443-only ingress.')
    print('Manifest SHA256: ' + sha((output / 'manifest.json').read_bytes()))


def verify_bundle(folder, checksum):
    data = (folder / 'manifest.json').read_bytes()
    if sha(data) != checksum:
        raise ValueError('Reviewed manifest changed')
    manifest = json.loads(data)
    if (manifest.get('purpose') != 'DEMO_HTTPS_443_ONLY' or manifest.get('domain') != HOST
            or manifest.get('lan_ipv4') != LAN or manifest.get('caddy_version') != VERSION
            or set(manifest.get('files', {})) != FILES):
        raise ValueError('Unexpected ingress manifest')
    for name, expected in manifest['files'].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != expected:
            raise ValueError('Reviewed ingress artifact changed')
    verify_package(folder)


def require_vm_target():
    marker = Path('/etc/polymarket-demo-vm/runtime-target.json')
    if marker.is_symlink() or marker.stat().st_uid != 0 or marker.stat().st_mode & 0o022:
        raise ValueError('Untrusted VM target marker')
    if json.loads(marker.read_text()) != dict(status='COMPLETE', target='polymarket-demo-vm',
            guest_ipv4='172.31.250.2', host_writer_disabled=True):
        raise ValueError('Verified VM cutover required')
    if run('systemctl', 'is-active', 'polymarket-demo-vm.service') != 'active':
        raise ValueError('Dedicated VM is inactive')
    if run('systemctl', 'show', 'polymarket-demo-v2.service', '-p', 'MainPID', '--value') != '0':
        raise ValueError('Retired host writer must remain stopped')
    # Read the boundary, never rewrite/flush it or add incoming VM forwarding.
    run('nft', 'list', 'table', 'inet', 'demo_vm_guard')
    with urllib.request.urlopen('http://127.0.0.1:8080/demo/', timeout=10) as response:
        if response.status != 200:
            raise ValueError('Existing DEMO relay unhealthy')
    try:
        urllib.request.urlopen('http://127.0.0.1:8080/demo/api/v1/rules', timeout=10)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return
    raise ValueError('Existing DEMO authentication boundary failed')


def apply(folder, checksum):
    if os.geteuid() != 0:
        raise PermissionError('Ubuntu administrator authentication required')
    verify_bundle(folder, checksum)
    require_vm_target()
    if any(Path(path).exists() or Path(path).is_symlink() for path in (
            '/etc/polymarket-demo-https', '/etc/systemd/system/' + SERVICE,
            '/etc/systemd/system/caddy.service', '/etc/systemd/system/caddy-api.service',
            '/usr/share/keyrings/polymarket-demo-caddy.gpg',
            '/etc/apt/sources.list.d/polymarket-demo-caddy.list',
            '/etc/apt/preferences.d/polymarket-demo-caddy')):
        raise ValueError('Ingress/default Caddy configuration already exists; inspect instead of overwriting')
    if shutil.which('caddy'):
        raise ValueError('Existing Caddy installation requires a separate review')
    with socket.socket() as probe:
        probe.bind((LAN, 443))  # Refuse absent/changed host IP or an occupied port.
    ufw_active = run('ufw', 'status').startswith('Status: active')
    # Copy the reviewed package/config into root-private staging and verify again
    # before executing a package installer; a user-writable bundle is not trusted.
    stage = Path(tempfile.mkdtemp(prefix='demo-https-install-', dir='/var/tmp'))
    stage.chmod(0o700)
    for name in FILES | {'manifest.json'}:
        (stage / name).write_bytes((folder / name).read_bytes())
    verify_bundle(stage, checksum)
    before_guard = run('nft', '--stateless', 'list', 'table', 'inet', 'demo_vm_guard')
    # Prevent the package's generic default site from starting on TCP 80/443.
    run('systemctl', 'mask', 'caddy.service', 'caddy-api.service')
    run('dpkg', '--install', str(stage / 'caddy.deb'))
    # Keep future normal host package upgrades on the official signed repository.
    # Trust is scoped to this source; only the caddy package gets positive priority.
    keyring = Path('/usr/share/keyrings/polymarket-demo-caddy.gpg')
    if keyring.exists():
        raise ValueError('Dedicated Caddy keyring already exists')
    shutil.copyfile(stage / 'keyring.gpg', keyring)
    keyring.chmod(0o644)
    source = Path('/etc/apt/sources.list.d/polymarket-demo-caddy.list')
    preferences = Path('/etc/apt/preferences.d/polymarket-demo-caddy')
    if source.exists() or preferences.exists():
        raise ValueError('Dedicated Caddy APT configuration already exists')
    source.write_text('deb [signed-by=' + str(keyring) + '] ' + OFFICIAL + 'deb/debian any-version main\n')
    source.chmod(0o644)
    preferences.write_text('Package: caddy\nPin: origin "dl.cloudsmith.io"\nPin-Priority: 600\n\n'
                           'Package: *\nPin: origin "dl.cloudsmith.io"\nPin-Priority: -1\n')
    preferences.chmod(0o644)
    conf = Path('/etc/polymarket-demo-https')
    conf.mkdir(mode=0o755)
    shutil.copyfile(stage / 'Caddyfile', conf / 'Caddyfile')
    (conf / 'Caddyfile').chmod(0o644)
    unit = Path('/etc/systemd/system') / SERVICE
    shutil.copyfile(stage / SERVICE, unit)
    unit.chmod(0o644)
    run('systemd-analyze', 'verify', str(unit))
    run('systemctl', 'daemon-reload')
    firewall_added = False
    try:
        if ufw_active:
            run('ufw', 'allow', 'in', 'on', 'wlp3s0', 'proto', 'tcp', 'to', LAN,
                'port', '443', 'comment', 'DEMO HTTPS only')
            firewall_added = True
        run('systemctl', 'enable', '--now', SERVICE)
        if run('systemctl', 'is-active', SERVICE) != 'active':
            raise ValueError('Dedicated HTTPS ingress failed to start')
        if run('nft', '--stateless', 'list', 'table', 'inet', 'demo_vm_guard') != before_guard:
            raise ValueError('VM boundary changed unexpectedly')
        require_vm_target()
    except BaseException:
        subprocess.run(['systemctl', 'disable', '--now', SERVICE], capture_output=True)
        if firewall_added:
            subprocess.run(['ufw', 'delete', 'allow', 'in', 'on', 'wlp3s0', 'proto', 'tcp', 'to', LAN,
                            'port', '443'], capture_output=True)
        raise
    (conf / 'installation.json').write_text(json.dumps({'domain': HOST, 'lan_ipv4': LAN,
        'port': 443, 'protocol': 'TCP', 'caddy_version': VERSION,
        'status': 'INGRESS_STARTED_CERTIFICATE_AND_EXTERNAL_VERIFICATION_PENDING',
        'manifest_sha256': checksum, 'ufw_rule_added': firewall_added}, indent=2) + '\n')
    (conf / 'installation.json').chmod(0o644)
    print('DEMO_HTTPS_INGRESS_STARTED; TCP 443 only; public certificate/reachability still require verification.')
    print('Existing nginx/local access, VM boundary/writer, DB and credentials unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--cache', type=Path)
    install = commands.add_parser('apply')
    install.add_argument('--bundle', type=Path, required=True)
    install.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.output.resolve(), args.cache.resolve() if args.cache else None)
    else:
        apply(args.bundle.resolve(), args.manifest_sha256)


if __name__ == '__main__':
    main()
