"""Promote a reviewed offline DEMO-only bundle; preserve state and roll back code.

Run explicitly as administrator. Does not alter env, units, nginx, firewall, DNS,
credentials or LIVE. Checksummed artifacts are copied to private staging first.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path('/opt/polymarket-demo-v2')
UNIT = 'polymarket-demo-v2.service'
DATABASE = Path('/var/lib/polymarket-demo-v2/demo_v2.sqlite3')
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'PYTHONDONTWRITEBYTECODE':'1',
       'PIP_CONFIG_FILE':'/dev/null'}


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def read_manifest(bundle, expected):
    path = bundle/'manifest.json'
    if not re.fullmatch(r'[a-f0-9]{64}', expected) or digest(path) != expected:
        raise ValueError('Deployment manifest checksum mismatch')
    manifest = json.loads(path.read_text())
    if manifest.get('version') != 1:
        raise ValueError('Unknown bundle format')
    for field in ('release_sha','previous_sha'):
        if not re.fullmatch(r'[a-f0-9]{40}', manifest.get(field,'')):
            raise ValueError('Invalid release identity')
    files = manifest.get('files',{})
    if not {'source.tar','frontend.tar'} <= files.keys() or not any(n.startswith('wheels/') for n in files):
        raise ValueError('Incomplete offline bundle')
    for name, checksum in files.items():
        if not (name in ('source.tar','frontend.tar') or re.fullmatch(r'wheels/[A-Za-z0-9_.+-]+\.whl',name)):
            raise ValueError('Unexpected artifact path')
        if not re.fullmatch(r'[a-f0-9]{64}',checksum):
            raise ValueError('Invalid artifact checksum')
    return manifest


def extract(archive, destination):
    with tarfile.open(archive) as source:
        source.extractall(destination, filter='data')


def run(*args, capture=False, cwd=None, env=None):
    return subprocess.run(args, check=True, text=True, cwd=cwd, env=env or ENV,
                          stdout=subprocess.PIPE if capture else None).stdout


def property_value(name):
    return run('systemctl','show',UNIT,'--property='+name,'--value',capture=True).strip()


def wait_alive(timeout=25):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            with urlopen('http://127.0.0.1:8010/health',timeout=2) as response:
                payload = json.load(response)
            if payload == {'status':'alive','mode':'SIMULATION','trading_enabled':False}:
                return
        except (OSError, ValueError, URLError):
            pass
        time.sleep(1)
    raise RuntimeError('DEMO did not pass liveness verification')


def state_ids():
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True) as connection:
        return {table:{row[0] for row in connection.execute('SELECT id FROM '+table)}
                for table in ('demo_v2_rules','demo_v2_positions','demo_v2_ledger')}



def preserved_settings():
    # Compare private fingerprints in memory only; never log setting values,
    # recipients, auth fingerprints, or their digests in deployment evidence.
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True) as connection:
        rows=connection.execute("""SELECT key,value FROM demo_v2_meta WHERE
            key IN ('session_version','auth_fingerprint','notification_settings','trade_serial_counter')
            OR key GLOB 'trade_serial:*'""").fetchall()
        return {key:int(value) if key=='trade_serial_counter' else hashlib.sha256(value.encode()).digest()
                for key,value in rows}


def verify_settings(before):
    after=preserved_settings()
    if any((after.get(key,-1)<value if key=='trade_serial_counter' else after.get(key)!=value)
           for key,value in before.items()):
        raise RuntimeError('Existing DEMO session, notification preferences or trade serials were changed')


def promote(release):
    pending = ROOT/'current.next'
    if pending.exists() or pending.is_symlink():
        raise RuntimeError('An unfinished deployment link requires inspection')
    pending.symlink_to(release)
    pending.replace(ROOT/'current')


def update(bundle, expected):
    if os.geteuid() != 0:
        raise PermissionError('Administrator authentication is required; run the prepared command with sudo')
    manifest = read_manifest(bundle, expected)
    previous = ROOT/'releases'/manifest['previous_sha']
    release = ROOT/'releases'/manifest['release_sha']
    if release.exists():
        raise RuntimeError('Release already exists; do not overwrite immutable releases')
    if (ROOT/'current').resolve() != previous or not (previous/'app').is_dir():
        raise RuntimeError('Installed release changed; deployment review must be refreshed')
    if property_value('ActiveState') != 'active' or property_value('User') != 'polymarket-demo-v2':
        raise RuntimeError('Unexpected DEMO service identity/state')
    if property_value('WorkingDirectory') != str(ROOT/'app'):
        raise RuntimeError('Unexpected DEMO working directory')
    if 'demo_v2.app:create_app' not in property_value('ExecStart'):
        raise RuntimeError('Unexpected service entrypoint')
    wait_alive()
    private = Path(tempfile.mkdtemp(prefix='polymarket-demo-update-',dir='/var/tmp'))
    os.chmod(private,0o700)
    for name, checksum in manifest['files'].items():
        target = private/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(bundle/name,target)
        if digest(target) != checksum:
            raise ValueError('Artifact checksum mismatch: '+name)
    release.mkdir(mode=0o755)
    (release/'app').mkdir()
    (release/'frontend/demo').mkdir(parents=True)
    extract(private/'source.tar',release/'app')
    extract(private/'frontend.tar',release/'frontend/demo')
    # This update must be schema-identical, permitting rollback without DB restore.
    migration = Path('app/demo_v2/persistence/migrations.py')
    if digest(release/migration) != digest(previous/migration):
        raise RuntimeError('Schema changes are outside this reviewed code-only update')
    run('/usr/bin/python3','-m','venv',str(release/'venv'))
    python = str(release/'venv/bin/python')
    run(python,'-m','pip','--isolated','install','--disable-pip-version-check','--no-cache-dir',
        '--no-index','--no-deps','--find-links='+str(private/'wheels'),'-r',
        str(release/'app/demo_v2/deploy/requirements.lock'))
    run(python,'-m','pip','check')
    # Private staging keeps umask 0077. Public release parents/files must also be
    # traversable/readable by the app and nginx workers, without becoming writable.
    spec=importlib.util.spec_from_file_location('demo_release_files',release/'app/demo_v2/deploy/release_files.py')
    permissions=importlib.util.module_from_spec(spec);spec.loader.exec_module(permissions)
    permissions.grant_release_access(ROOT,release)
    # Verify the actual service identity, not root, with disposable state only.
    test_env = {**ENV,'PYTHONPATH':str(release/'app')+':'+str(release/'app/polymarket-collector')}
    run('runuser','-u','polymarket-demo-v2','--',python,'-m','demo_v2.deploy.release_check',env=test_env,cwd=release/'app')
    # Nothing in the immutable release contains an env/credential/DB copy.
    backup_dir = Path('/var/backups/polymarket-demo-v2')
    backup_dir.mkdir(mode=0o700,exist_ok=True)
    backup = backup_dir/('before-'+manifest['release_sha']+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite3')
    run(python,str(release/'app/demo_v2/deploy/backup_sqlite.py'),
        '--source',str(DATABASE),'--destination',str(backup))
    if (ROOT/'current').resolve() != previous:
        raise RuntimeError('Concurrent release change; refusing promotion')
    previous_ids = state_ids()
    switched = False
    try:
        run('systemctl','stop',UNIT)
        # Quiescent final backup makes the actual promotion point recoverable.
        final_backup = backup.with_name(backup.stem+'-stopped.sqlite3')
        run(python,str(release/'app/demo_v2/deploy/backup_sqlite.py'),
            '--source',str(DATABASE),'--destination',str(final_backup))
        previous_ids = state_ids()
        previous_settings=preserved_settings()
        promote(release)
        switched = True
        run('systemctl','start',UNIT)
        wait_alive()
        if property_value('ActiveState') != 'active':
            raise RuntimeError('DEMO unit is not active')
        pid = property_value('MainPID')
        if Path('/proc/'+pid+'/cwd').resolve() != release/'app':
            raise RuntimeError('Running process does not use the reviewed release')
        for name in ('index.html','src/app.js','src/resources.js','src/pipeline.js','src/typography.css'):
            with urlopen('http://127.0.0.1:8080/demo/'+name,timeout=5) as response:
                actual = hashlib.sha256(response.read()).hexdigest()
            if actual != digest(release/'frontend/demo'/name):
                raise RuntimeError('Served frontend does not match release: '+name)
        current_ids = state_ids()
        if any(not ids <= current_ids[table] for table,ids in previous_ids.items()):
            raise RuntimeError('Existing DEMO records were not preserved')
        verify_settings(previous_settings)
    except BaseException:
        if switched:
            run('systemctl','stop',UNIT)
            promote(previous)
        run('systemctl','start',UNIT)
        wait_alive()
        raise
    record = dict(release_sha=manifest['release_sha'],previous_sha=manifest['previous_sha'],
                  deployed_at=datetime.now(timezone.utc).isoformat(),backup=str(final_backup),
                  records_preserved={table:len(ids) for table,ids in previous_ids.items()},
                  env_changed=False,service_config_changed=False,proxy_changed=False,
                  private_settings_and_trade_serials_preserved=True)
    records=ROOT/'deployment-state'
    records.mkdir(mode=0o700,exist_ok=True)
    with (records/(manifest['release_sha']+'.json')).open('x') as output:
        json.dump(record,output,indent=2)
    print('DEMO deployment verified. Release: '+manifest['release_sha'])
    print('Previous release and verified DB backups retained. Existing records preserved.')
    print('No env/unit/nginx/DNS/firewall/LIVE changes. Inspect authenticated health for provider readiness.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    args=parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Administrator authentication required: use sudo with this prepared command')
    with Path('/run/lock/polymarket-demo-v2-deploy.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        update(args.bundle.resolve(),args.manifest_sha256)


if __name__=='__main__':
    main()
