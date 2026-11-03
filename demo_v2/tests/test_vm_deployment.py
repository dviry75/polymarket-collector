"""Migration safety using temp state and fake SSH; never host/guest operations."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from demo_v2.deploy.vm import controller,guest,migrate


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
