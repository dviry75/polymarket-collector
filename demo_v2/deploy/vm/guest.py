"""Root helper for this dedicated KVM guest only; never load private env values."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path('/opt/polymarket-demo-v2')
DATA=Path('/var/lib/polymarket-demo-v2')
DB=DATA/'demo_v2.sqlite3'
ENV=Path('/etc/polymarket-demo-v2/demo.env')
UNIT='polymarket-demo-v2.service'
SAFE_ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','PYTHONDONTWRITEBYTECODE':'1',
          'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l','PIP_CONFIG_FILE':'/dev/null'}


def run(*args,capture=False,env=None):
    return subprocess.run(args,check=True,text=True,env=env or SAFE_ENV,
        stdout=subprocess.PIPE if capture else None).stdout


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def snapshot(path=DB):
    with sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('DEMO database verification failed')
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if not tables or any(not name.startswith(('demo_v2_','sqlite_')) for name in tables):
            raise RuntimeError('Not a DEMO-only database')
        ids={table:[r[0] for r in conn.execute('SELECT id FROM '+table)]
             for table in ('demo_v2_rules','demo_v2_positions','demo_v2_ledger')}
        meta={key:(int(value) if key=='trade_serial_counter' else hashlib.sha256(value.encode()).hexdigest())
              for key,value in conn.execute("""SELECT key,value FROM demo_v2_meta WHERE
                key IN ('session_version','auth_fingerprint','notification_settings','trade_serial_counter')
                OR key GLOB 'trade_serial:*'""")}
    return {'ids':ids,'meta':meta}


def preserves(before,after):
    return (all(set(rows)<=set(after['ids'].get(table,[])) for table,rows in before['ids'].items())
        and all((after['meta'].get(key,-1)>=value if key=='trade_serial_counter'
                 else after['meta'].get(key)!=None and after['meta'][key]==value)
                for key,value in before['meta'].items()))


def backup(destination):
    destination=Path(destination)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as reader:
        with sqlite3.connect(destination) as writer:
            reader.backup(writer,pages=256,sleep=.05)
            writer.execute('PRAGMA journal_mode=DELETE')
    snapshot(destination)


def verify():
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            with urlopen('http://127.0.0.1:8010/health',timeout=3) as response:health=json.load(response)
            with urlopen('http://127.0.0.1:8010/ready',timeout=3) as response:ready=json.load(response)
            if health=={'status':'alive','mode':'SIMULATION','trading_enabled':False} and ready['entries_allowed']:
                break
        except (OSError,ValueError):pass
        time.sleep(2)
    else:raise RuntimeError('Guest DEMO did not become ready')
    if run('systemctl','is-active',UNIT,capture=True).strip()!='active':raise RuntimeError('Guest DEMO inactive')
    pid=int(run('systemctl','show',UNIT,'--property=MainPID','--value',capture=True))
    release=(ROOT/'current').resolve()
    if Path(f'/proc/{pid}/cwd').resolve()!=release/'app':raise RuntimeError('Guest release identity mismatch')
    children=Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    if len(children)<2:raise RuntimeError('Guest decision child not present')
    if run('systemctl','show',UNIT,'--property=NRestarts','--value',capture=True).strip()!='0':
        raise RuntimeError('Guest app restarted during migration')
    for name in ('index.html','src/app.js','src/resources.js','src/pipeline.js'):
        with urlopen('http://127.0.0.1:8080/demo/'+name,timeout=5) as response:
            if hashlib.sha256(response.read()).hexdigest()!=digest(release/'frontend/demo'/name):
                raise RuntimeError('Guest frontend mismatch')
    try:urlopen('http://127.0.0.1:8080/demo/api/v1/health',timeout=5)
    except HTTPError as exc:
        if exc.code!=401:raise
    else:raise RuntimeError('Guest authenticated health is not protected')
    return {'ready':ready['state'],'simulation_only':True,'pid':pid,
            'release':release.name,'database':snapshot(),'env_digest':digest(ENV)}


def provision(bundle,sha):
    manifest=json.loads((bundle/'manifest.json').read_text())
    if manifest['release_sha']!=sha:raise ValueError('Guest release identity mismatch')
    for name,expected in manifest['files'].items():
        if digest(bundle/name)!=expected:raise ValueError('Guest artifact changed')
    try:account=pwd.getpwnam('polymarket-demo-v2')
    except KeyError:
        run('useradd','--system','--user-group','--home-dir','/nonexistent','--shell','/usr/sbin/nologin','polymarket-demo-v2')
        account=pwd.getpwnam('polymarket-demo-v2')
    DATA.mkdir(mode=0o700,exist_ok=True);DATA.chmod(0o700);os.chown(DATA,account.pw_uid,account.pw_gid)
    ENV.parent.mkdir(mode=0o700,exist_ok=True);ENV.parent.chmod(0o700)
    release=ROOT/'releases'/sha
    if release.exists():raise RuntimeError('Guest release already exists; inspect interrupted provisioning')
    (release/'app').mkdir(parents=True)
    (release/'frontend/demo').mkdir(parents=True)
    for name,target in (('source.tar',release/'app'),('frontend.tar',release/'frontend/demo')):
        with tarfile.open(bundle/name) as archive:archive.extractall(target,filter='data')
    run('/usr/bin/python3','-m','venv',str(release/'venv'))
    python=str(release/'venv/bin/python')
    run(python,'-m','pip','--isolated','install','--disable-pip-version-check','--no-index','--no-deps',
        '--find-links='+str(bundle/'wheels'),'-r',str(release/'app/demo_v2/deploy/requirements.lock'))
    run(python,'-m','pip','check')
    run(python,'-m','demo_v2.deploy.release_check',env={**SAFE_ENV,
        'PYTHONPATH':str(release/'app')+':'+str(release/'app/polymarket-collector')})
    for name in ('app','venv','frontend'):
        (ROOT/name).symlink_to('current/'+name)
    (ROOT/'current').symlink_to('releases/'+sha)
    for root,dirs,files in os.walk(release):
        for name in dirs+files:
            path=Path(root)/name
            if not path.is_symlink():path.chmod(path.stat().st_mode & ~0o022)
    shutil.copyfile(bundle/'app.service',Path('/etc/systemd/system')/UNIT)
    Path('/etc/systemd/system/'+UNIT).chmod(0o644)
    snippet=Path('/etc/nginx/snippets/polymarket-demo-v2-api.conf')
    shutil.copyfile(bundle/'nginx-api.conf',snippet);snippet.chmod(0o644)
    site=Path('/etc/nginx/sites-available/polymarket-demo-v2.conf')
    shutil.copyfile(bundle/'nginx-guest.conf',site);site.chmod(0o644)
    enabled=Path('/etc/nginx/sites-enabled/polymarket-demo-v2.conf');enabled.symlink_to(site)
    default=Path('/etc/nginx/sites-enabled/default')
    if default.is_symlink():default.unlink()  # Stock site inside this new guest only.
    run('nginx','-t');run('systemctl','daemon-reload')
    run('systemctl','disable',UNIT)
    run('systemctl','enable','--now','nginx.service');run('systemctl','reload','nginx.service')
    # Nothing starts the real app before the old writer is stopped and final state arrives.
    if subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0:
        raise RuntimeError('Guest app must remain stopped before cutover')
    print('Guest code, interpreter and proxy provisioned; application still stopped.')


def import_state(bundle):
    if subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0:
        raise RuntimeError('Cannot import state while guest writer is active')
    snapshot(bundle/'demo_v2.sqlite3')
    if DB.exists():raise RuntimeError('Existing guest database must not be overwritten')
    account=pwd.getpwnam('polymarket-demo-v2')
    shutil.copyfile(bundle/'demo_v2.sqlite3',DB);DB.chmod(0o600);os.chown(DB,account.pw_uid,account.pw_gid)
    shutil.copyfile(bundle/'demo.env',ENV);ENV.chmod(0o600)
    for path in (bundle/'demo.env',bundle/'demo_v2.sqlite3'):path.unlink()
    print('Guest private state imported without displaying values.')


def main():
    if os.geteuid()!=0 or socket.gethostname()!='polymarket-demo-vm':raise SystemExit('Dedicated guest root only')
    if run('systemd-detect-virt',capture=True).strip()!='kvm':raise SystemExit('Expected KVM guest')
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=('prepare-os','provision','import-state','verify','export-state','stop','start'))
    parser.add_argument('--bundle',type=Path);parser.add_argument('--sha');args=parser.parse_args()
    os.umask(0o077)
    if args.operation=='prepare-os':
        if DB.exists():raise RuntimeError('OS bootstrap is only for the empty new guest')
        run('apt-get','update')
        run('apt-get','-o','DPkg::Lock::Timeout=300','upgrade','--with-new-pkgs','-y')
        run('apt-get','-o','DPkg::Lock::Timeout=300','install','--no-install-recommends','-y',
            'python3-venv','nginx','ca-certificates','libatomic1')
    elif args.operation=='provision':provision(args.bundle.resolve(),args.sha)
    elif args.operation=='import-state':import_state(args.bundle.resolve())
    elif args.operation=='verify':print(json.dumps(verify()))  # Controller captures; never echo private fingerprints.
    elif args.operation=='export-state':
        run('systemctl','disable',UNIT)
        run('systemctl','stop',UNIT)
        if subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode==0:raise RuntimeError('Guest writer still active')
        args.bundle.mkdir(mode=0o700,exist_ok=True);backup(args.bundle/'demo_v2.sqlite3')
        admin=pwd.getpwnam('demoadmin');os.chown(args.bundle/'demo_v2.sqlite3',admin.pw_uid,admin.pw_gid)
    elif args.operation=='stop':run('systemctl','stop',UNIT)
    elif args.operation=='start':run('systemctl','enable','--now',UNIT)


if __name__=='__main__':main()
