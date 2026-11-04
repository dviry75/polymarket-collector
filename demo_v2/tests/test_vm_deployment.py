"""Migration safety using temp state and fake SSH; never host/guest operations."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from demo_v2.deploy.vm import controller,guest,migrate,install_base,verify_guest


def baseline():
    return {'ids':{'demo_v2_rules':['r'],'demo_v2_positions':['p'],'demo_v2_ledger':['l']},
            'meta':{'session_version':'hashed-v1','auth_fingerprint':'hashed-auth',
                    'notification_settings':'hashed-settings','trade_serial_counter':5,'trade_serial:p':'hashed-serial'}}


def test_existing_records_and_private_settings_survive_with_legitimate_new_trades():
    before=baseline();after=copy.deepcopy(before)
    after['ids']['demo_v2_positions'].append('new')
    after['ids']['demo_v2_ledger'].append('new-l')
    after['meta'].update(trade_serial_counter=6,**{'trade_serial:new':'new-hash'})
    assert guest.preserves(before,after)


@pytest.mark.parametrize('field',('session_version','auth_fingerprint','notification_settings','trade_serial_counter','trade_serial:p'))
def test_replaced_private_settings_or_serial_regression_are_rejected(field):
    before=baseline();after=copy.deepcopy(before);after['meta'][field]=4 if field=='trade_serial_counter' else 'changed'
    assert not guest.preserves(before,after)


@pytest.mark.parametrize('table',('demo_v2_rules','demo_v2_positions','demo_v2_ledger'))
def test_missing_existing_ids_fail_verification(table):
    before=baseline();after=copy.deepcopy(before);after['ids'][table]=[]
    assert not guest.preserves(before,after)


def test_snapshot_reads_only_demo_schema_and_hashes_private_metadata(repository,market,rule):
    with repository.database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO demo_v2_meta VALUES ('notification_settings','fake-private-value')")
    result=guest.snapshot(repository.database.config.db_path)
    assert result['ids']['demo_v2_rules']==[rule['id']]
    assert 'fake-private-value' not in json.dumps(result)
    assert result['meta']['notification_settings']==hashlib.sha256(b'fake-private-value').hexdigest()


def test_unreachable_guest_during_rollback_never_starts_stale_host(tmp_path,monkeypatch):
    guard=tmp_path/'writer-in-vm';guard.write_text('guarded')
    monkeypatch.setattr(migrate,'GUARD',guard)
    def unreachable(*args,**kwargs):raise ConnectionError('fake guest unreachable')
    monkeypatch.setattr(migrate,'ssh',unreachable)
    calls=[];monkeypatch.setattr(migrate,'run',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(ConnectionError):
        migrate.rollback(tmp_path,'/home/demoadmin/staging',None,{'phase':'GUEST_STARTING'})
    assert guard.exists() and not calls


def test_rollback_restores_committed_guest_state_before_restarting_host(tmp_path,monkeypatch):
    stage=tmp_path/'stage';stage.mkdir();(stage/'final.sqlite3').write_bytes(b'initial')
    (stage/'host-nginx-before.conf').write_text('original nginx')
    db=tmp_path/'demo_v2.sqlite3';db.write_bytes(b'stale host')
    (tmp_path/'demo_v2.sqlite3-wal').write_bytes(b'old WAL')
    site=tmp_path/'nginx.conf';site.write_text('guest proxy')
    guard=tmp_path/'writer-in-vm';guard.write_text('guarded')
    for name,value in (('DB',db),('SITE',site),('GUARD',guard),('STATE',tmp_path/'migration.json')):
        monkeypatch.setattr(migrate,name,value)
    events=[]
    def ssh(*args,**kwargs):
        events.append(('ssh',args))
        return '/home/demoadmin/demo-rollback-ABC123\n' if args[0]=='mktemp' else ''
    def run(*args,**kwargs):
        events.append(('host',args))
        if args[0]=='scp':Path(args[-1]).write_bytes(b'guest committed state')
        if args[:2]==('systemctl','start'):
            assert db.read_bytes()==b'guest committed state'
            assert not guard.exists() and site.read_text()=='original nginx'
    monkeypatch.setattr(migrate,'ssh',ssh);monkeypatch.setattr(migrate,'run',run)
    helper=SimpleNamespace(snapshot=lambda path:baseline(),preserves=guest.preserves)
    state={'phase':'PROXY_CHANGING','host_was_enabled':True}
    migrate.rollback(stage,'/home/demoadmin/staging',helper,state)
    assert state['phase']=='ROLLED_BACK' and not (tmp_path/'demo_v2.sqlite3-wal').exists()
    export=next(i for i,event in enumerate(events) if 'export-state' in event[1])
    start=next(i for i,event in enumerate(events) if event==('host',('systemctl','start',migrate.UNIT)))
    assert export<start


def test_prepare_manifest_digests_all_artifacts_except_generated_wrapper(tmp_path):
    (tmp_path/'source.tar').write_bytes(b'committed source')
    checksum=controller.write_manifest(tmp_path,{'purpose':'DEMO_VM_MIGRATION_V1'})
    assert checksum==controller.digest(tmp_path/'manifest.json')
    manifest=json.loads((tmp_path/'manifest.json').read_text())
    assert manifest['files']=={'source.tar':hashlib.sha256(b'committed source').hexdigest()}


def test_migration_manifest_checksum_failure_precedes_staging(tmp_path):
    (tmp_path/'manifest.json').write_text('{}')
    with pytest.raises(ValueError,match='checksum'):
        migrate.stage_bundle(tmp_path,'0'*64)


def test_managed_base_retry_stops_only_dedicated_vm_and_dns(tmp_path,monkeypatch):
    monkeypatch.setattr(install_base,'CONF',tmp_path)
    calls=[]
    def run(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='0\n')
    monkeypatch.setattr(install_base,'run',run)
    install_base.stop_incomplete_base(False)
    assert not calls
    install_base.stop_incomplete_base(True)
    assert calls==[
        ('systemctl','stop','polymarket-demo-vm.service','polymarket-demo-vm-dns.service'),
        ('systemctl','show','polymarket-demo-vm.service','-p','MainPID','--value')]


@pytest.mark.parametrize('marker',('migration-state.json','writer-in-vm','runtime-target.json'))
def test_base_retry_refuses_application_migration_state_before_stopping_any_service(tmp_path,monkeypatch,marker):
    monkeypatch.setattr(install_base,'CONF',tmp_path)
    (tmp_path/marker).write_text('fake state')
    calls=[];monkeypatch.setattr(install_base,'run',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(SystemExit,match='migration state exists'):
        install_base.stop_incomplete_base(True)
    assert not calls


def test_base_retry_fails_if_vm_keeps_running(tmp_path,monkeypatch):
    monkeypatch.setattr(install_base,'CONF',tmp_path)
    monkeypatch.setattr(install_base,'run',lambda *args,**kwargs:SimpleNamespace(stdout='123\n'))
    with pytest.raises(SystemExit,match='did not stop'):
        install_base.stop_incomplete_base(True)


def test_public_probe_distinguishes_dns_failure_without_exposing_exception_text(monkeypatch):
    def failed(*args):raise verify_guest.socket.gaierror('private diagnostic details')
    monkeypatch.setattr(verify_guest.socket,'getaddrinfo',failed)
    assert verify_guest.public_probe('fake.test')=={
        'dns':False,'tcp':False,'tls':False,'failed_stage':'dns','error_type':'gaierror'}


def test_public_probe_tries_another_address_and_verifies_tls(monkeypatch):
    addresses=[('192.0.2.1',443),('192.0.2.2',443)]
    monkeypatch.setattr(verify_guest.socket,'getaddrinfo',lambda *args:[(2,1,6,'',a) for a in addresses])
    attempted=[]
    class Socket:
        def settimeout(self,value):pass
        def connect(self,address):
            attempted.append(address)
            if address==addresses[0]:raise ConnectionRefusedError('fake failure')
        def close(self):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(verify_guest.socket,'socket',lambda *args:Socket())
    def wrap(raw,server_hostname):
        assert server_hostname=='fake.test'
        return raw
    monkeypatch.setattr(verify_guest.ssl,'create_default_context',lambda:SimpleNamespace(wrap_socket=wrap))
    assert verify_guest.public_probe('fake.test')=={'dns':True,'tcp':True,'tls':True}
    assert attempted==addresses


@pytest.mark.parametrize('phase',('CUTOVER_LOCKED','GUEST_STARTING','GUEST_RUNNING','PROXY_CHANGING','PROXY_SWITCHED','COMPLETE','ROLLED_BACK'))
def test_provisioning_resume_never_reenters_a_cutover_or_rollback(phase):
    with pytest.raises(RuntimeError,match='pre-cutover'):
        migrate.validate_provisioning_resume({'phase':phase,'previous_sha':'old'},{'previous_sha':'old'})


def test_provisioning_resume_rechecks_host_release():
    migrate.validate_provisioning_resume({'phase':'PROVISIONING','previous_sha':'old'},{'previous_sha':'old'})
    with pytest.raises(RuntimeError,match='matching'):
        migrate.validate_provisioning_resume({'phase':'PROVISIONING','previous_sha':'other'},{'previous_sha':'old'})


@pytest.mark.parametrize('path',('database','env','current','releases'))
def test_bootstrap_recovery_refuses_existing_guest_app_or_private_state(tmp_path,monkeypatch,path):
    root=tmp_path/'app';root.mkdir()
    db=tmp_path/'data.sqlite3';env=tmp_path/'demo.env'
    for name,value in (('ROOT',root),('DB',db),('ENV',env)):monkeypatch.setattr(guest,name,value)
    target={'database':db,'env':env,'current':root/'current','releases':root/'releases'}[path]
    target.write_text('fake')
    calls=[];monkeypatch.setattr(guest,'run',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(RuntimeError,match='empty guest'):guest.prepare_os()
    assert not calls


def test_bootstrap_finishes_interrupted_package_configuration_before_installing(tmp_path,monkeypatch):
    for name,value in (('ROOT',tmp_path/'app'),('DB',tmp_path/'data.sqlite3'),('ENV',tmp_path/'demo.env')):
        monkeypatch.setattr(guest,name,value)
    monkeypatch.setattr(guest.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=3))
    calls=[]
    monkeypatch.setattr(guest,'run',lambda *args,**kwargs:calls.append(args) or '')
    guest.prepare_os()
    assert calls[0]==('dpkg','--configure','--pending')
    assert calls[-1]==('dpkg','--audit')
    assert any('python3-venv' in call and 'nginx' in call for call in calls)


@pytest.mark.parametrize('result,exit_status',(('exit-code','1'),('success','1'),('timeout','0')))
def test_bootstrap_requires_persistent_job_success_not_just_reachable_ssh(monkeypatch,result,exit_status):
    monkeypatch.setattr(migrate,'run',lambda *args,**kwargs:'100\n')
    monkeypatch.setattr(migrate,'ssh',lambda *args,**kwargs:
        f'LoadState=loaded\nActiveState=active\nSubState=exited\nResult={result}\nExecMainStatus={exit_status}\n')
    with pytest.raises(RuntimeError,match='exit successfully'):migrate.wait_bootstrap('fake-bootstrap.service')


def test_bootstrap_rejects_qemu_restart_instead_of_accepting_a_new_guest(monkeypatch):
    pids=iter(('100\n','200\n'))
    monkeypatch.setattr(migrate,'run',lambda *args,**kwargs:next(pids))
    calls=[];monkeypatch.setattr(migrate,'ssh',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(RuntimeError,match='QEMU restarted'):migrate.wait_bootstrap('fake-bootstrap.service')
    assert not calls


def test_bootstrap_can_recover_ssh_disconnect_but_still_waits_for_job_exit(monkeypatch):
    monkeypatch.setattr(migrate,'run',lambda *args,**kwargs:'100\n')
    monkeypatch.setattr(migrate.time,'sleep',lambda _:None)
    responses=iter((migrate.subprocess.CalledProcessError(255,'fake ssh'),
        'LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n'))
    def ssh(*args,**kwargs):
        result=next(responses)
        if isinstance(result,Exception):raise result
        return result
    monkeypatch.setattr(migrate,'ssh',ssh)
    migrate.wait_bootstrap('fake-bootstrap.service')


@pytest.mark.parametrize('phase',('PROVISIONING','RETRY_PREPARING','CUTOVER_LOCKED','GUEST_STARTING','GUEST_RUNNING','PROXY_SWITCHED','COMPLETE'))
def test_rolled_back_retry_requires_completed_single_writer_recovery(phase):
    with pytest.raises(RuntimeError,match='completed rollback'):
        migrate.validate_rolled_back_retry({'phase':phase,'previous_sha':'old','release_sha':'guest'},
            {'previous_sha':'old','retry_guest_sha':'guest'})


def test_rolled_back_retry_matches_both_host_and_retired_guest_releases():
    previous={'phase':'ROLLED_BACK','previous_sha':'host','release_sha':'guest'}
    manifest={'previous_sha':'host','retry_guest_sha':'guest'}
    migrate.validate_rolled_back_retry(previous,manifest)
    for change in ({'previous_sha':'changed-host'},{'retry_guest_sha':'changed-guest'}):
        with pytest.raises(RuntimeError):migrate.validate_rolled_back_retry(previous,{**manifest,**change})


def test_guest_state_archive_preserves_all_private_files_without_overwriting_them(tmp_path,monkeypatch,capsys):
    root=tmp_path/'runtime';release=root/'releases'/'old';release.mkdir(parents=True)
    (root/'current').symlink_to(release)
    data=tmp_path/'state';data.mkdir();db=data/'demo_v2.sqlite3';env=tmp_path/'demo.env'
    files=(db,Path(str(db)+'-wal'),Path(str(db)+'-shm'),env)
    for index,path in enumerate(files):path.write_text('fake-private-'+str(index));path.chmod(0o600)
    for name,value in (('ROOT',root),('DB',db),('ENV',env),('ARCHIVES',tmp_path/'archive')):
        monkeypatch.setattr(guest,name,value)
    monkeypatch.setattr(guest,'require_stopped_guest',lambda:None)
    monkeypatch.setattr(guest.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1))
    monkeypatch.setattr(guest,'snapshot',lambda path:baseline())
    guest.park_rolled_back_state('old')
    saved=next((tmp_path/'archive').iterdir())
    assert saved.stat().st_mode&0o777==0o700
    for index,path in enumerate(files):
        assert not path.exists()
        assert (saved/path.name).read_text()=='fake-private-'+str(index)
    assert 'fake-private' not in capsys.readouterr().out


def test_active_guest_cannot_be_archived_even_when_host_wants_a_retry(monkeypatch):
    monkeypatch.setattr(guest.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError,match='writer must be stopped'):guest.park_rolled_back_state('old')


def test_retry_only_reuses_the_exact_root_owned_retired_host_condition(tmp_path,monkeypatch):
    guard=tmp_path/'writer-in-vm';monkeypatch.setattr(migrate,'GUARD',guard)
    content='[Unit]\nConditionPathExists=!'+str(guard)+'\n'
    dropin=SimpleNamespace(exists=lambda:True,is_symlink=lambda:False,
        stat=lambda:SimpleNamespace(st_uid=0),read_text=lambda:content)
    monkeypatch.setattr(migrate,'DROPIN',dropin)
    migrate.check_host_guard(True)
    with pytest.raises(RuntimeError):migrate.check_host_guard(False)
    content='[Unit]\nUnreviewed=change\n'
    with pytest.raises(RuntimeError):migrate.check_host_guard(True)
    guard.write_text('active guard')
    with pytest.raises(RuntimeError,match='writer guard'):migrate.check_host_guard(True)
