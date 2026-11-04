"""Reviewed sudo migration: base KVM guest, final backup, single-writer cutover.

No LIVE state, public ports, DNS, router, sudoers or tunnel changes. The host
nginx remains a loopback-only ingress relay. Private state travels over pinned SSH.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path('/opt/polymarket-demo-v2')
DB=Path('/var/lib/polymarket-demo-v2/demo_v2.sqlite3')
ENV=Path('/etc/polymarket-demo-v2/demo.env')
CONF=Path('/etc/polymarket-demo-vm')
PRIVATE=Path('/home/dvir/Documents/Codex/2026-10-29/new-chat/work/demo-vm-private')
UNIT='polymarket-demo-v2.service'
SITE=Path('/etc/nginx/sites-available/polymarket-demo-v2.conf')
STATE=CONF/'migration-state.json'
GUARD=CONF/'writer-in-vm'
SSH_OPTIONS=['-i',str(PRIVATE/'id_ed25519'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
    '-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=polymarket-demo-vm',
    '-o','UserKnownHostsFile='+str(PRIVATE/'known_hosts')]
TARGET='demoadmin@172.31.250.2'
SAFE_ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','PYTHONDONTWRITEBYTECODE':'1',
          'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'}


def run(*args,capture=False,timeout=1200):
    return subprocess.run(args,check=True,text=True,env=SAFE_ENV,timeout=timeout,
        stdout=subprocess.PIPE if capture else None).stdout


def ssh(*args,capture=False):
    return run('ssh',*SSH_OPTIONS,TARGET,shlex.join([str(a) for a in args]),capture=capture)


def upload(source,destination):
    run('scp',*SSH_OPTIONS,'-r',str(source),TARGET+':'+shlex.quote(str(destination)))


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def load_helper(path):
    spec=importlib.util.spec_from_file_location('demo_vm_guest_helpers',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def stage_bundle(bundle,checksum):
    if not re.fullmatch('[a-f0-9]{64}',checksum) or digest(bundle/'manifest.json')!=checksum:
        raise ValueError('Migration manifest checksum mismatch')
    manifest=json.loads((bundle/'manifest.json').read_text())
    if manifest.get('purpose')!='DEMO_VM_MIGRATION_V1':raise ValueError('Unexpected migration bundle')
    if not all(re.fullmatch('[a-f0-9]{40}',manifest.get(key,'')) for key in ('release_sha','previous_sha')):
        raise ValueError('Invalid release')
    private=Path(tempfile.mkdtemp(prefix='demo-vm-migration-',dir='/var/tmp'));private.chmod(0o700)
    for name,expected in manifest['files'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+|wheels/[A-Za-z0-9_.+-]+\.whl',name):
            raise ValueError('Unexpected migration artifact path')
        if not re.fullmatch('[a-f0-9]{64}',expected):raise ValueError('Invalid artifact digest')
        target=private/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(bundle/name,target)
        if digest(target)!=expected:raise ValueError('Migration artifact changed')
    shutil.copyfile(bundle/'manifest.json',private/'manifest.json')
    if digest(private/'manifest.json')!=checksum:raise ValueError('Manifest changed during staging')
    return private,manifest


def property_value(name):
    return run('systemctl','show',UNIT,'--property='+name,'--value',capture=True).strip()


def record(state):
    state['updated_at']=datetime.now(timezone.utc).isoformat()
    target=STATE.with_suffix('.next');target.write_text(json.dumps(state,indent=2)+'\n');target.chmod(0o600)
    target.replace(STATE)
    public=STATE.parent/'runtime-target.json'
    if state['phase']=='COMPLETE':
        pending=public.with_suffix('.next')
        pending.write_text(json.dumps({'status':'COMPLETE','target':'polymarket-demo-vm',
            'guest_ipv4':'172.31.250.2','host_writer_disabled':True})+'\n')
        pending.chmod(0o644);pending.replace(public)
    elif state['phase']=='ROLLED_BACK':public.unlink(missing_ok=True)


def proxy_verify(stage):
    for name in ('index.html','src/app.js','src/pipeline.js'):
        expected=stage/'served-check'/name
        with urlopen('http://127.0.0.1:8080/demo/'+name,timeout=5) as response:
            if hashlib.sha256(response.read()).hexdigest()!=digest(expected):raise RuntimeError('Host proxy serves another frontend')
    try:urlopen('http://127.0.0.1:8080/demo/api/v1/health',timeout=5)
    except HTTPError as exc:
        if exc.code!=401:raise
    else:raise RuntimeError('Host proxy bypassed authentication')


def validate_provisioning_resume(previous,manifest):
    if previous.get('phase')!='PROVISIONING' or previous.get('previous_sha')!=manifest['previous_sha']:
        raise RuntimeError('Only matching pre-cutover PROVISIONING may resume; inspect recovery state')


def resume_empty_base(stage,manifest):
    # The caller already checked the active host release/proxy and absence of guards.
    # Independently verify guest identity/emptiness BEFORE restarting QEMU.
    if ssh('hostname',capture=True).strip()!='polymarket-demo-vm' or ssh('systemd-detect-virt',capture=True).strip()!='kvm':
        raise RuntimeError('Recovery target is not the dedicated KVM guest')
    jobs=json.loads(ssh('systemctl','list-units','--all','--output=json','polymarket-demo-vm-bootstrap-*',capture=True))
    if any(job['sub'] not in ('dead','failed','exited') for job in jobs):
        raise RuntimeError('A guest bootstrap job is still running; do not interrupt it')
    remote=ssh('mktemp','-d','/home/demoadmin/demo-migration-XXXXXX',capture=True).strip()
    if not re.fullmatch('/home/demoadmin/demo-migration-[A-Za-z0-9]+',remote):raise RuntimeError('Unexpected guest staging path')
    upload(stage/'guest.py',remote+'/guest.py')
    ssh('sudo','-n','python3',remote+'/guest.py','empty-check')
    unit=Path('/etc/systemd/system/polymarket-demo-vm.service')
    if digest(unit)!=manifest.get('host_vm_unit_sha256'):raise RuntimeError('Installed VM unit changed; review before recovery')
    shutil.copyfile(unit,stage/'host-vm-unit-before.service')
    run('systemd-analyze','verify',str(stage/unit.name))
    run('systemctl','stop',unit.name)
    if run('systemctl','show',unit.name,'--property=MainPID','--value',capture=True).strip()!='0':
        raise RuntimeError('VM did not stop; refusing unit replacement')
    shutil.copyfile(stage/unit.name,unit);unit.chmod(0o644)
    run('systemctl','daemon-reload');run('systemctl','start',unit.name)
    run('runuser','-u','dvir','--','python3','/usr/local/lib/polymarket-demo-vm/verify_guest.py',
        '--wait','180','--report',str(PRIVATE/'resumed-base-boundary-result.json'),timeout=260)
    ssh('sudo','-n','python3',remote+'/guest.py','empty-check')
    return remote


def wait_bootstrap(unit,timeout=1900):
    deadline=time.monotonic()+timeout
    vm_pid=run('systemctl','show','polymarket-demo-vm.service','--property=MainPID','--value',capture=True).strip()
    disconnects=0;next_message=0
    while time.monotonic()<deadline:
        if run('systemctl','show','polymarket-demo-vm.service','--property=MainPID','--value',capture=True).strip()!=vm_pid:
            raise RuntimeError('QEMU restarted during guest bootstrap; package success is unverified')
        try:
            output=ssh('systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState','-p','Result','-p','ExecMainStatus',capture=True)
            values=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
            disconnects=0
        except subprocess.CalledProcessError as exc:
            if exc.returncode!=255 or disconnects>=3:raise
            disconnects+=1;time.sleep(5);continue
        if values.get('LoadState')!='loaded' or values.get('ActiveState')=='failed':
            raise RuntimeError('Guest bootstrap job failed or disappeared: '+unit)
        if values.get('ActiveState')=='active' and values.get('SubState')=='exited':
            if values.get('Result')!='success' or values.get('ExecMainStatus')!='0':
                raise RuntimeError('Guest bootstrap did not exit successfully: '+unit)
            return
        if time.monotonic()>=next_message:
            print('Guest OS bootstrap running; host DEMO remains active.',flush=True)
            next_message=time.monotonic()+30
        time.sleep(5)
    raise RuntimeError('Guest bootstrap timed out; inspect its job before resuming: '+unit)


def bootstrap_os(remote,state):
    # Package installation survives SSH/session replacement, but never assume a
    # disconnect means success: inspect the persistent guest unit's exit status.
    unit='polymarket-demo-vm-bootstrap-'+remote.rsplit('-',1)[-1]+'.service'
    state['bootstrap_unit']=unit;record(state)
    try:
        ssh('sudo','-n','systemd-run','--no-block','--unit='+unit,'--property=Type=oneshot',
            '--property=RemainAfterExit=yes','--property=TimeoutStartSec=1800',
            '/usr/bin/python3',remote+'/guest.py','prepare-os')
    except subprocess.CalledProcessError as exc:
        if exc.returncode!=255:raise
    wait_bootstrap(unit)
    before=ssh('cat','/proc/sys/kernel/random/boot_id',capture=True).strip()
    try:ssh('sudo','-n','systemctl','reboot')
    except subprocess.CalledProcessError as exc:
        if exc.returncode!=255:raise
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        time.sleep(3)
        try:after=ssh('cat','/proc/sys/kernel/random/boot_id',capture=True).strip()
        except subprocess.CalledProcessError as exc:
            if exc.returncode!=255:raise
            continue
        if after and after!=before:break
    else:raise RuntimeError('Updated guest did not complete a verified reboot')


def rollback(stage,remote,helper,state):
    # Once the guest may have written, never start the stale host DB. Recover the
    # guest's final committed state first; unreachable guest means leave host off.
    phase=state['phase']
    if phase in ('GUEST_STARTING','GUEST_RUNNING','PROXY_CHANGING','PROXY_SWITCHED'):
        export=ssh('mktemp','-d','/home/demoadmin/demo-rollback-XXXXXX',capture=True).strip()
        if not re.fullmatch('/home/demoadmin/demo-rollback-[A-Za-z0-9]+',export):raise RuntimeError('Unexpected rollback path')
        ssh('sudo','-n','python3',remote+'/guest.py','export-state','--bundle',export)
        recovered=stage/'recovered.sqlite3'
        run('scp',*SSH_OPTIONS,TARGET+':'+export+'/demo_v2.sqlite3',str(recovered))
        new=helper.snapshot(recovered)
        if not helper.preserves(helper.snapshot(stage/'final.sqlite3'),new):raise RuntimeError('Guest recovery lost pre-migration records')
        # Both writers are stopped. Keep old DB, WAL and SHM in the private backup.
        for suffix in ('','-wal','-shm'):
            source=Path(str(DB)+suffix)
            if source.exists():shutil.copyfile(source,stage/('old-host.sqlite3'+suffix))
        for suffix in ('-wal','-shm'):
            source=Path(str(DB)+suffix)
            if source.exists():source.unlink()
        account=DB.stat();replacement=DB.with_suffix('.vm-recovery')
        shutil.copyfile(recovered,replacement);replacement.chmod(0o600);os.chown(replacement,account.st_uid,account.st_gid)
        replacement.replace(DB)
    ssh('sudo','-n','systemctl','disable',UNIT)
    if phase in ('PROXY_CHANGING','PROXY_SWITCHED'):
        shutil.copyfile(stage/'host-nginx-before.conf',SITE);run('nginx','-t');run('systemctl','reload','nginx.service')
    GUARD.unlink(missing_ok=True)
    run('systemctl','daemon-reload')
    if state['host_was_enabled']:run('systemctl','enable',UNIT)
    run('systemctl','start',UNIT)
    state['phase']='ROLLED_BACK';record(state)
    print('Migration rolled back with committed state preserved; inspect failure before retrying.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--resume-provisioning',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname()!='dvir-technologies':raise SystemExit('Run with sudo on the dedicated development host')
    os.umask(0o077)
    import fcntl
    with Path('/run/lock/polymarket-demo-v2-deploy.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=None
        if STATE.exists():
            previous=json.loads(STATE.read_text())
            if previous.get('phase')=='COMPLETE':
                print('DEMO migration already complete; use the VM release workflow.');return
            if not args.resume_provisioning:
                raise RuntimeError('Existing migration journal requires inspection; do not overwrite state or start a second writer')
        elif args.resume_provisioning:raise RuntimeError('No provisioning journal exists to resume')
        if GUARD.exists() or Path('/etc/systemd/system/'+UNIT+'.d/00-vm-cutover.conf').exists():
            raise RuntimeError('Existing host writer guard requires inspection')
        stage,manifest=stage_bundle(args.bundle.resolve(),args.manifest_sha256)
        if previous:
            validate_provisioning_resume(previous,manifest)
            shutil.copyfile(STATE,stage/'previous-migration-state.json')
        sha=manifest['release_sha'];previous_sha=manifest['previous_sha']
        if (ROOT/'current').resolve()!=ROOT/'releases'/previous_sha:raise RuntimeError('Installed release changed; rebuild migration bundle')
        import tarfile
        with tarfile.open(stage/'source.tar') as archive:
            migration_source=archive.extractfile('demo_v2/persistence/migrations.py').read()
        if hashlib.sha256(migration_source).hexdigest()!=digest(ROOT/'app/demo_v2/persistence/migrations.py'):
            raise RuntimeError('Schema changes require a separate migration review')
        if property_value('ActiveState')!='active' or property_value('User')!='polymarket-demo-v2':raise RuntimeError('Unexpected host DEMO service')
        if digest(SITE)!=manifest['host_nginx_sha256']:raise RuntimeError('Host nginx changed; review before migration')
        # Read only these non-credential settings; never source/print the env.
        mail={}
        for line in ENV.read_text().splitlines():
            if line.startswith(('DEMO_EMAIL_ENABLED=','DEMO_SMTP_PORT=')):
                key,value=line.split('=',1);mail[key]=value.strip().strip('"\'')
        if mail.get('DEMO_EMAIL_ENABLED','false').lower()=='true' and int(mail.get('DEMO_SMTP_PORT','587')) not in (465,587):
            raise RuntimeError('Configured SMTP port needs a reviewed firewall exception before migration')
        helper=load_helper(stage/'guest.py')
        # Reuse the verified base network/QEMU preparation. No existing app restart.
        if previous:remote=resume_empty_base(stage,manifest)
        else:run('/usr/bin/python3',str(stage/'install_base.py'),timeout=1800)
        state={'phase':'PROVISIONING','release_sha':sha,'previous_sha':previous_sha,'host_was_enabled':
            subprocess.run(['systemctl','is-enabled','--quiet',UNIT]).returncode==0,'backup_directory':str(stage)}
        record(state)
        if not previous:remote=ssh('mktemp','-d','/home/demoadmin/demo-migration-XXXXXX',capture=True).strip()
        if not re.fullmatch('/home/demoadmin/demo-migration-[A-Za-z0-9]+',remote):raise RuntimeError('Unexpected guest staging path')
        state['guest_staging']=remote;record(state)
        app=stage/'guest-bundle';app.mkdir(mode=0o700)
        for name in ('source.tar','frontend.tar','guest.py','app.service','nginx-api.conf','nginx-guest.conf'):
            shutil.copyfile(stage/name,app/name)
        shutil.copyfile(stage/'release-manifest.json',app/'manifest.json');shutil.copytree(stage/'wheels',app/'wheels')
        upload(str(app)+'/.',remote+'/')
        bootstrap_os(remote,state)
        run('runuser','-u','dvir','--','python3','/usr/local/lib/polymarket-demo-vm/verify_guest.py',
            '--wait','180','--report',str(PRIVATE/'updated-guest-boundary-result.json'),timeout=260)
        ssh('sudo','-n','python3',remote+'/guest.py','provision','--bundle',remote,'--sha',sha)
        # Preparing the guest may take minutes; recheck concurrent host changes.
        if (ROOT/'current').resolve()!=ROOT/'releases'/previous_sha or digest(SITE)!=manifest['host_nginx_sha256']:
            raise RuntimeError('Host changed during provisioning; leave the original writer active')
        backup_script=ROOT/'app/demo_v2/deploy/backup_sqlite.py'
        python=ROOT/'venv/bin/python'
        run(str(python),str(backup_script),'--source',str(DB),'--destination',str(stage/'online.sqlite3'))
        shutil.copyfile(SITE,stage/'host-nginx-before.conf')
        state['phase']='CUTOVER_LOCKED';record(state)
        try:
            # Persist a boot/manual-start guard before stopping the old writer.
            dropin=Path('/etc/systemd/system')/(UNIT+'.d');dropin.mkdir(exist_ok=True)
            guard_conf=dropin/'00-vm-cutover.conf'
            if guard_conf.exists():raise RuntimeError('Existing host cutover guard requires inspection')
            guard_conf.write_text('[Unit]\nConditionPathExists=!'+str(GUARD)+'\n');guard_conf.chmod(0o644)
            GUARD.write_text('DEMO financial writer belongs to the isolated VM.\n');GUARD.chmod(0o600)
            run('systemctl','daemon-reload');run('systemctl','stop',UNIT);run('systemctl','disable',UNIT)
            if property_value('ActiveState')=='active':raise RuntimeError('Old DEMO writer still active')
            run('sync')  # Persist the host boot guard before enabling a guest writer.
            run(str(python),str(backup_script),'--source',str(DB),'--destination',str(stage/'final.sqlite3'))
            before=helper.snapshot(stage/'final.sqlite3')
            # Copy opaque DEMO-only private files. Never source, log or put them in Git.
            upload(stage/'final.sqlite3',remote+'/demo_v2.sqlite3');upload(ENV,remote+'/demo.env')
            ssh('sudo','-n','python3',remote+'/guest.py','import-state','--bundle',remote)
            state['phase']='GUEST_STARTING';record(state)
            ssh('sudo','-n','python3',remote+'/guest.py','start')
            result=json.loads(ssh('sudo','-n','python3',remote+'/guest.py','verify',capture=True))
            if result['release']!=sha or result['env_digest']!=digest(ENV) or not helper.preserves(before,result['database']):
                raise RuntimeError('Guest did not preserve release/config/records/settings/serials')
            state['phase']='GUEST_RUNNING';record(state)
            # Stage exact frontend byte checks without starting another application.
            with tarfile.open(stage/'frontend.tar') as archive:archive.extractall(stage/'served-check',filter='data')
            state['phase']='PROXY_CHANGING';record(state)
            shutil.copyfile(stage/'nginx-host-proxy.conf',SITE)
            run('nginx','-t');run('systemctl','reload','nginx.service')
            state['phase']='PROXY_SWITCHED';record(state)
            proxy_verify(stage)
            # Guest-root containment probes run again after actual app deployment.
            run('runuser','-u','dvir','--','python3','/usr/local/lib/polymarket-demo-vm/verify_guest.py',
                '--wait','60','--report',str(PRIVATE/'migration-boundary-result.json'),timeout=120)
            if property_value('ActiveState')=='active':raise RuntimeError('Second host writer detected')
            state.update(phase='COMPLETE',guest_pid=result['pid'],records_preserved={k:len(v) for k,v in before['ids'].items()},
                private_settings_preserved=True,host_app_disabled=True,host_proxy_loopback_only=True)
            record(state)
            print('DEMO_VM_MIGRATION_PASS: runtime and private state are in KVM; host is development plus loopback ingress.')
            print('Existing login/settings/history/serials preserved. No LIVE/router/DNS/tunnel changes.')
        except BaseException:
            print('Cutover failed; attempting single-writer state-preserving recovery.')
            try:rollback(stage,remote,helper,state)
            except BaseException:
                print('RECOVERY_REQUIRES_INSPECTION: keep host writer guarded; never start it against its stale database.')
            raise


if __name__=='__main__':main()
