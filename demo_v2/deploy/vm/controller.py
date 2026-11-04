"""Host development controller: prepare migration or deploy a committed VM release.

No private env/DB reads. Initial migration needs host sudo; subsequent releases
use the existing operator SSH key with admin authority inside the DEMO guest only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
PRIVATE=Path('/home/dvir/Documents/Codex/2026-10-29/new-chat/work/demo-vm-private')
DEFAULT_NODE=Path('/home/dvir/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
SAFE_ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','PYTHONDONTWRITEBYTECODE':'1',
          'DEMO_API_BASE':'/demo/api/v1','PIP_CONFIG_FILE':'/dev/null'}
SSH_OPTIONS=['-i',str(PRIVATE/'id_ed25519'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
    '-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=polymarket-demo-vm',
    '-o','ConnectTimeout=5','-o','UserKnownHostsFile='+str(PRIVATE/'known_hosts')]
TARGET='demoadmin@172.31.250.2'


def run(*args,capture=False,cwd=None):
    return subprocess.run([str(a) for a in args],check=True,text=True,cwd=cwd,env=SAFE_ENV,
        stdout=subprocess.PIPE if capture else None).stdout


def ssh(*args,capture=False):
    return run('ssh',*SSH_OPTIONS,TARGET,shlex.join([str(a) for a in args]),capture=capture)


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def write_manifest(folder,fields):
    fields['files']={str(path.relative_to(folder)):digest(path) for path in sorted(folder.rglob('*'))
                     if path.is_file() and path.name not in ('manifest.json','migrate-demo.sh')}
    path=folder/'manifest.json';path.write_text(json.dumps(fields,sort_keys=True,indent=2)+'\n')
    return digest(path)


def migration(args):
    source=args.release_bundle.resolve();image=args.image_dir.resolve()
    release=json.loads((source/'manifest.json').read_text());sha=release['release_sha']
    previous=release['previous_sha']
    if not all(re.fullmatch('[a-f0-9]{40}',value) for value in (sha,previous)):raise ValueError('Invalid release identity')
    if Path('/opt/polymarket-demo-v2/current').resolve().name!=previous:raise RuntimeError('Host release changed; refresh migration bundle')
    args.output.mkdir(parents=True)
    output=args.output.resolve()
    for name,expected in release['files'].items():
        if not re.fullmatch(r'source\.tar|frontend\.tar|wheels/[A-Za-z0-9_.+-]+\.whl',name):raise ValueError('Unexpected release artifact')
        if digest(source/name)!=expected:raise ValueError('Original release artifact changed')
        target=output/name;target.parent.mkdir(exist_ok=True);shutil.copyfile(source/name,target)
    shutil.copyfile(source/'manifest.json',output/'release-manifest.json')
    for name in ('ubuntu-26.04-server-cloudimg-amd64.img','SHA256SUMS','SHA256SUMS.gpg'):
        shutil.copyfile(image/name,output/name)
    for path in HERE.iterdir():
        if path.suffix in ('.py','.service'):shutil.copyfile(path,output/path.name)
    shutil.copyfile(REPO/'demo_v2/deploy/polymarket-demo-v2.service',output/'app.service')
    shutil.copyfile('/etc/nginx/snippets/polymarket-demo-v2-api.conf',output/'nginx-api.conf')
    site=Path('/etc/nginx/sites-available/polymarket-demo-v2.conf')
    original=site.read_text()
    if original.count('listen 127.0.0.1:8080;')!=1 or 'real_ip_header CF-Connecting-IP;' not in original:
        raise RuntimeError('Unexpected installed DEMO nginx profile; inspect before migration')
    guest=original.replace('listen 127.0.0.1:8080;',
        'listen 127.0.0.1:8080;\n    listen 172.31.250.2:8080;').replace(
        'set_real_ip_from 127.0.0.1;', 'set_real_ip_from 127.0.0.1;\n    set_real_ip_from 172.31.250.1;')
    (output/'nginx-guest.conf').write_text(guest)
    (output/'nginx-host-proxy.conf').write_text('''# DEMO VM ingress relay only; no app/state/static root on host.
server {
    listen 127.0.0.1:8080;
    server_name demo-poly.dvirtechnologies.com;
    absolute_redirect off;
    server_tokens off;
    add_header X-Demo-Runtime-Target "polymarket-demo-vm" always;
    set_real_ip_from 127.0.0.1;
    real_ip_header CF-Connecting-IP;
    client_max_body_size 32k;
    client_header_timeout 15s;
    client_body_timeout 15s;
    access_log off;
    error_log /var/log/nginx/polymarket-demo-v2.error.log warn;
    location / {
        proxy_pass http://172.31.250.2:8080;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header CF-Connecting-IP $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_hide_header X-Demo-Runtime-Target;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        proxy_buffering off;
        proxy_cache off;
    }
}
''')
    commit=run('git','rev-parse','HEAD',capture=True,cwd=REPO).strip()
    fields={'version':1,'purpose':'DEMO_VM_MIGRATION_V1','release_sha':sha,
        'previous_sha':previous,'tool_commit':commit,'host_nginx_sha256':digest(site)}
    if args.resume_provisioning:
        fields['host_vm_unit_sha256']=digest(Path('/etc/systemd/system/polymarket-demo-vm.service'))
    if args.retry_rolled_back:
        current=ssh('readlink','-f','/opt/polymarket-demo-v2/current',capture=True).strip()
        if not re.fullmatch('/opt/polymarket-demo-v2/releases/[a-f0-9]{40}',current):raise RuntimeError('Unexpected guest release')
        fields['retry_guest_sha']=Path(current).name
    checksum=write_manifest(output,fields)
    script=output/'migrate-demo.sh'
    command=['/usr/bin/python3',str(output/'migrate.py'),'--bundle',str(output),'--manifest-sha256',checksum]
    if args.resume_provisioning:command.append('--resume-provisioning')
    if args.retry_rolled_back:command.append('--retry-rolled-back')
    script.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'+shlex.join(command)+'\n')
    script.chmod(0o755)
    print('Migration bundle: '+str(output))
    print('Manifest SHA256: '+checksum)
    print('Operator command: sudo bash '+shlex.quote(str(script)))


def release(args):
    # No host sudo or private config access: normal host -> pinned guest SSH.
    marker=Path('/etc/polymarket-demo-vm/runtime-target.json')
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_uid!=0 or marker.stat().st_mode & 0o022:
        raise RuntimeError('Verified VM migration marker is required before releases')
    target=json.loads(marker.read_text())
    if target!={'status':'COMPLETE','target':'polymarket-demo-vm','guest_ipv4':'172.31.250.2','host_writer_disabled':True}:
        raise RuntimeError('VM target is not ready for ordinary releases')
    if ssh('hostname',capture=True).strip()!='polymarket-demo-vm' or ssh('systemd-detect-virt',capture=True).strip()!='kvm':
        raise RuntimeError('Deployment target is not the dedicated KVM guest')
    previous=Path(ssh('readlink','-f','/opt/polymarket-demo-v2/current',capture=True).strip()).name
    sha=run('git','rev-parse','--verify','--end-of-options',args.sha+'^{commit}',capture=True,cwd=REPO).strip()
    if not re.fullmatch('[a-f0-9]{40}',previous) or not re.fullmatch('[a-f0-9]{40}',sha):raise ValueError('Invalid release identity')
    if previous==sha:raise RuntimeError('That release is already installed')
    # Only immutable committed source is exported; dirty files never enter a release.
    output=args.output.resolve();output.mkdir(parents=True)
    run('git','archive','--format=tar','--output='+str(output/'source.tar'),sha,cwd=REPO)
    with tempfile.TemporaryDirectory(prefix='demo-vm-release-') as folder:
        root=Path(folder)
        with tarfile.open(output/'source.tar') as archive:archive.extractall(root,filter='data')
        node=args.node or shutil.which('node') or DEFAULT_NODE
        run(node,'scripts/build.mjs',cwd=root/'demo_v2/frontend')
        with tarfile.open(output/'frontend.tar','w') as archive:
            for path in sorted((root/'demo_v2/frontend/build').iterdir()):archive.add(path,arcname=path.name)
        wheels=output/'wheels';wheels.mkdir()
        if args.wheels:
            for path in args.wheels.glob('*.whl'):shutil.copyfile(path,wheels/path.name)
        else:
            run(sys.executable,'-m','venv',root/'build-venv')
            run(root/'build-venv/bin/python','-m','pip','--isolated','download','--only-binary=:all:',
                '--no-deps','--dest',wheels,'-r',root/'demo_v2/deploy/requirements.lock')
        shutil.copyfile(root/'demo_v2/deploy/update_release.py',output/'update_release.py')
    # The existing updater's manifest intentionally covers source/frontend/wheels.
    fields={'version':1,'release_sha':sha,'previous_sha':previous,'files':{str(p.relative_to(output)):digest(p)
        for p in sorted(output.rglob('*')) if p.is_file() and p.name!='update_release.py'}}
    (output/'manifest.json').write_text(json.dumps(fields,sort_keys=True,indent=2)+'\n')
    checksum=digest(output/'manifest.json')
    remote=ssh('mktemp','-d','/home/demoadmin/demo-release-XXXXXX',capture=True).strip()
    if not re.fullmatch('/home/demoadmin/demo-release-[A-Za-z0-9]+',remote):raise RuntimeError('Unexpected guest staging path')
    run('scp',*SSH_OPTIONS,'-r',str(output)+'/.',TARGET+':'+remote+'/')
    ssh('sudo','-n','/usr/bin/python3',remote+'/update_release.py','--bundle',remote,'--manifest-sha256',checksum)
    print('VM release deployed: '+sha)


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='action',required=True)
    migrate=commands.add_parser('prepare-migration')
    migrate.add_argument('--release-bundle',type=Path,required=True);migrate.add_argument('--image-dir',type=Path,required=True)
    migrate.add_argument('--output',type=Path,required=True)
    recovery=migrate.add_mutually_exclusive_group()
    recovery.add_argument('--resume-provisioning',action='store_true',help='Reviewed recovery of PROVISIONING with an empty guest only')
    recovery.add_argument('--retry-rolled-back',action='store_true',help='Reviewed retry after a completed state-preserving rollback')
    deploy=commands.add_parser('deploy');deploy.add_argument('--sha',default='HEAD')
    deploy.add_argument('--output',type=Path,required=True);deploy.add_argument('--node',type=Path)
    deploy.add_argument('--wheels',type=Path)
    args=parser.parse_args()
    if args.action=='prepare-migration':migration(args)
    else:release(args)


if __name__=='__main__':main()
