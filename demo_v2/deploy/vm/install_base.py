"""Explicit sudo entrypoint: create/test a base DEMO VM. No app or data migration.
No router, DNS, public forwarding, existing service restart or LIVE resource changes.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import uuid

BUNDLE=Path(__file__).resolve().parent
CONF=Path('/etc/polymarket-demo-vm')
LIB=Path('/usr/local/lib/polymarket-demo-vm')
STATE=Path('/var/lib/polymarket-demo-vm')
PRIVATE=Path('/home/dvir/Documents/Codex/2026-10-29/new-chat/work/demo-vm-private')
IMAGE='ubuntu-26.04-server-cloudimg-amd64.img'
IMAGE_SHA='5645d0318ad21f643ebb1ac4567514620e9893380b07b0201fda163baa5b7eb5'
UNITS=['polymarket-demo-vm-network.service','polymarket-demo-vm-dns.service','polymarket-demo-vm.service']


def run(*args, quiet=False):
    return subprocess.run(args,check=True,text=True,stdout=subprocess.PIPE if quiet else None,
                          stderr=subprocess.PIPE if quiet else None)


def digest(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def status():
    return {unit:run('systemctl','show',unit,'-p','MainPID','--value',quiet=True).stdout.strip()
            for unit in ['polymarket-demo-v2.service','nginx.service','cloudflared.service']}


def write(path,data,mode=0o644):
    path.write_text(data);path.chmod(mode)


def stop_incomplete_base(managed):
    # This retry is safe only before app provisioning/state transfer has begun.
    if any((CONF/name).exists() for name in ('migration-state.json','writer-in-vm','runtime-target.json')):
        raise SystemExit('Application migration state exists; inspect recovery instead of reinstalling the base VM')
    if managed:
        # Stop the DNS restart cycle and its dependent QEMU before replacing units.
        # Preserve the guest disk, SSH identity, private seed and network firewall.
        run('systemctl','stop','polymarket-demo-vm.service','polymarket-demo-vm-dns.service')
        if run('systemctl','show','polymarket-demo-vm.service','-p','MainPID','--value',quiet=True).stdout.strip()!='0':
            raise SystemExit('Managed VM did not stop; refusing to continue')


def main():
    if os.geteuid()!=0:raise SystemExit('Use sudo in your own terminal')
    if socket.gethostname()!='dvir-technologies':raise SystemExit('Unexpected host')
    if not Path('/dev/kvm').exists():raise SystemExit('KVM unavailable')
    if shutil.disk_usage('/var/lib').free<8*1024**3:raise SystemExit('Insufficient free disk space')
    before=status()
    manifest=json.loads((BUNDLE/'manifest.json').read_text())
    payload={name:expected for name,expected in manifest['files'].items() if '/' not in name and name not in (IMAGE,'SHA256SUMS','SHA256SUMS.gpg','source.tar','frontend.tar')}
    run('gpgv','--keyring','/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg',str(BUNDLE/'SHA256SUMS.gpg'),str(BUNDLE/'SHA256SUMS'))
    signed=[line.split()[0] for line in (BUNDLE/'SHA256SUMS').read_text().splitlines() if line.endswith('*'+IMAGE)]
    if signed!=[IMAGE_SHA]:raise SystemExit('Pinned Ubuntu image is not in the vendor-signed release')
    for name,expected in payload.items():
        if Path(name).name!=name or digest(BUNDLE/name)!=expected:raise SystemExit('Prepared payload changed')
    if digest(BUNDLE/IMAGE)!=IMAGE_SHA:raise SystemExit('Ubuntu cloud image checksum mismatch')
    pub=(PRIVATE/'id_ed25519.pub').read_text().strip()
    if not pub.startswith('ssh-ed25519 '):raise SystemExit('Missing dedicated guest management public key')
    operator=pwd.getpwnam('dvir')
    if PRIVATE.stat().st_uid!=operator.pw_uid or PRIVATE.stat().st_mode & 0o077:
        raise SystemExit('Private controller directory must be operator-owned mode 0700')
    marker=CONF/'stage3-managed.json'
    managed=marker.is_file() and not marker.is_symlink() and marker.stat().st_uid==0
    stop_incomplete_base(managed)
    # A managed retry must release its existing guest's RAM before this check.
    mem=next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    if mem<2048*1024:raise SystemExit('Less than 2 GiB RAM available; free resources before creating the 1.5 GiB guest')
    if not managed:
        for name in ('polymarket-demo-vm','polymarket-demo-dns'):
            try:pwd.getpwnam(name)
            except KeyError:pass
            else:raise SystemExit('Service account already exists; review ownership first')
        for path in [CONF,LIB,STATE,Path('/var/log/polymarket-demo-vm')]+[Path('/etc/systemd/system')/u for u in UNITS]:
            if path.exists() or path.is_symlink():raise SystemExit('Existing VM resource requires inspection: '+str(path))
        if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()!='0':
            raise SystemExit('Host already forwards traffic; review existing network ownership first')
        links=json.loads(run('ip','-j','link','show',quiet=True).stdout)
        if any(v['ifname'] in ('demo-br0','demo-tap0') for v in links):raise SystemExit('VM interface name already exists')
        if subprocess.run(['nft','list','table','inet','demo_vm_guard'],capture_output=True).returncode==0:
            raise SystemExit('Dedicated firewall table already exists; review ownership first')
        routes=json.loads(run('ip','-j','route','show','table','main',quiet=True).stdout)
        for route in routes:
            dst=route.get('dst','default')
            if dst!='default' and ipaddress.ip_network(dst).overlaps(ipaddress.ip_network('172.31.250.0/30')):
                raise SystemExit('VM subnet conflicts with an existing route')
        CONF.mkdir(mode=0o755)
        CONF.chmod(0o755)
        write(marker,json.dumps({'purpose':'dedicated DEMO VM stage 3','controller_uid':operator.pw_uid})+'\n')
    # Copy immutable checked payload before executing any new root-owned helper.
    stage=Path(tempfile.mkdtemp(prefix='demo-vm-install-',dir='/var/tmp'))
    stage.chmod(0o700)
    for name,expected in payload.items():
        shutil.copyfile(BUNDLE/name,stage/name)
        if digest(stage/name)!=expected:raise SystemExit('Payload copy integrity failure')
    shutil.copyfile(BUNDLE/IMAGE,stage/IMAGE)
    if digest(stage/IMAGE)!=IMAGE_SHA:raise SystemExit('Image copy integrity failure')
    # These packages add no general-purpose VM management daemon or router configuration.
    env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'}
    subprocess.run(['apt-get','update'],check=True,env=env)
    subprocess.run(['apt-get','install','--no-install-recommends','-y','qemu-system-x86','qemu-utils','cloud-image-utils','dnsmasq-base'],check=True,env=env)
    # Real packets, isolated namespace: stop before touching host networking if validation fails.
    run('unshare','--net',sys.executable,str(stage/'packet_checks.py'))
    run('unshare','--net',sys.executable,str(stage/'network_checks.py'))
    for name in ('polymarket-demo-vm','polymarket-demo-dns'):
        try:pwd.getpwnam(name)
        except KeyError:run('useradd','--system','--user-group','--home-dir','/nonexistent','--shell','/usr/sbin/nologin',name)
    vm=pwd.getpwnam('polymarket-demo-vm')
    LIB.mkdir(mode=0o755,exist_ok=True)
    LIB.chmod(0o755)
    for name in ('firewall.py','network.py','poweroff.py','verify_guest.py'):
        shutil.copyfile(stage/name,LIB/name);(LIB/name).chmod(0o644)
    STATE.mkdir(mode=0o750,exist_ok=True);os.chown(STATE,0,vm.pw_gid);STATE.chmod(0o750)
    logs=Path('/var/log/polymarket-demo-vm');logs.mkdir(mode=0o700,exist_ok=True);os.chown(logs,vm.pw_uid,vm.pw_gid)
    if not (STATE/'base.img').exists():
        shutil.copyfile(stage/IMAGE,STATE/'base.img');os.chown(STATE/'base.img',0,vm.pw_gid);(STATE/'base.img').chmod(0o440)
    if not (STATE/'disk.qcow2').exists():
        run('qemu-img','create','-f','qcow2','-F','qcow2','-b',str(STATE/'base.img'),str(STATE/'disk.qcow2'),'16G')
        os.chown(STATE/'disk.qcow2',vm.pw_uid,vm.pw_gid);(STATE/'disk.qcow2').chmod(0o600)
    provision=CONF/'provision';provision.mkdir(mode=0o700,exist_ok=True)
    hostkey=provision/'guest-host-key'
    if not hostkey.exists():run('ssh-keygen','-q','-t','ed25519','-N','','-C','demo-vm-host','-f',str(hostkey),quiet=True)
    write(CONF/'guest-host-key.pub',hostkey.with_suffix('.pub').read_text())
    if not (STATE/'seed.iso').exists():
        cloud={'hostname':'polymarket-demo-vm','manage_etc_hosts':True,'timezone':'UTC',
            'users':[{'name':'demoadmin','groups':['adm','sudo'],'shell':'/bin/bash',
                'sudo':['ALL=(ALL) NOPASSWD:ALL'],'lock_passwd':True,'ssh_authorized_keys':[pub]}],
            'disable_root':True,'ssh_pwauth':False,'ssh_deletekeys':True,'ssh_genkeytypes':['ed25519'],
            'ssh_keys':{'ed25519_private':hostkey.read_text(),'ed25519_public':hostkey.with_suffix('.pub').read_text()},
            'ssh':{'emit_keys_to_console':False},'no_ssh_fingerprints':True,
            'write_files':[{'path':'/etc/sysctl.d/90-demo-vm-ipv6.conf','permissions':'0644',
                'content':'net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n'}],
            'runcmd':[['sysctl','--system'],['systemctl','enable','--now','ssh.service']]}
        network={'version':2,'ethernets':{'demo':{'match':{'macaddress':'52:54:00:de:02:01'},
            'set-name':'demo0','dhcp4':False,'dhcp6':False,'accept-ra':False,
            'addresses':['172.31.250.2/30'],'routes':[{'to':'default','via':'172.31.250.1'}],
            'nameservers':{'addresses':['172.31.250.1']}}}}
        write(provision/'user-data','#cloud-config\n'+json.dumps(cloud,indent=2)+'\n',0o600)
        write(provision/'meta-data',json.dumps({'instance-id':'demo-vm-'+str(uuid.uuid4()),'local-hostname':'polymarket-demo-vm'})+'\n',0o600)
        write(provision/'network-config',json.dumps(network,indent=2)+'\n',0o600)
        run('cloud-localds','--network-config='+str(provision/'network-config'),str(STATE/'seed.iso'),str(provision/'user-data'),str(provision/'meta-data'))
        os.chown(STATE/'seed.iso',0,vm.pw_gid);(STATE/'seed.iso').chmod(0o440)
    default=json.loads(run('ip','-j','route','show','default',quiet=True).stdout)
    if len(default)!=1 or default[0]['dev']!='wlp3s0':raise SystemExit('Uplink changed; inspect before continuing')
    public=urllib.request.urlopen('https://api.ipify.org',timeout=15).read(128).decode().strip()
    if not ipaddress.IPv4Address(public).is_global:raise SystemExit('Invalid observed public address')
    write(CONF/'network.json',json.dumps({'uplink':'wlp3s0','home_public_ipv4':public},indent=2)+'\n')
    for unit in UNITS:shutil.copyfile(stage/unit,Path('/etc/systemd/system')/unit);(Path('/etc/systemd/system')/unit).chmod(0o644)
    run('systemd-analyze','verify',*[str(Path('/etc/systemd/system')/u) for u in UNITS])
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now',*UNITS)
    report=PRIVATE/'stage3-result.json'
    # Controller runs as the operator and has no administrator rights on the physical host.
    run('runuser','-u','dvir','--','python3',str(LIB/'verify_guest.py'),'--wait','360','--report',str(report))
    if status()!=before:raise SystemExit('An existing service PID changed externally; inspect before further deployment')
    print('VM_BASE_ISOLATION_PASS. Base guest ready; existing DEMO/data unchanged.')


if __name__=='__main__':
    os.umask(0o077)
    main()
