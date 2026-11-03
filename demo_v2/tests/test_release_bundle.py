"""Pure artifact validation only: never invoke the deployment updater or sudo."""
import hashlib
import io
import json
import tarfile

import pytest

from demo_v2.deploy.update_release import digest, read_manifest, extract


def manifest(tmp_path, **changes):
    data={'version':1,'release_sha':'a'*40,'previous_sha':'b'*40,
          'files':{'source.tar':'1'*64,'frontend.tar':'2'*64,'wheels/fake-1.0-py3-none-any.whl':'3'*64}}
    data.update(changes)
    path=tmp_path/'manifest.json';path.write_text(json.dumps(data))
    return digest(path)


def test_manifest_is_bound_to_exact_checksums_and_release_identity(tmp_path):
    sha=manifest(tmp_path)
    assert read_manifest(tmp_path,sha)['release_sha']=='a'*40
    with pytest.raises(ValueError,match='checksum'):
        read_manifest(tmp_path,'0'*64)
    sha=manifest(tmp_path,release_sha='../other')
    with pytest.raises(ValueError,match='identity'):
        read_manifest(tmp_path,sha)


def test_bundle_rejects_path_escape_unknown_files_and_missing_wheels(tmp_path):
    for extra in ('../outside','wheels/../../secret','/etc/demo.env','other.tar'):
        files={'source.tar':'1'*64,'frontend.tar':'2'*64,'wheels/fake.whl':'3'*64,extra:'4'*64}
        sha=manifest(tmp_path,files=files)
        with pytest.raises(ValueError,match='path'):
            read_manifest(tmp_path,sha)
    sha=manifest(tmp_path,files={'source.tar':'1'*64,'frontend.tar':'2'*64})
    with pytest.raises(ValueError,match='Incomplete'):
        read_manifest(tmp_path,sha)


def test_tar_extraction_cannot_write_outside_release(tmp_path):
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as source:
        entry=tarfile.TarInfo('../escape')
        entry.size=1
        source.addfile(entry,io.BytesIO(b'x'))
    destination=tmp_path/'release';destination.mkdir()
    with pytest.raises(tarfile.FilterError):
        extract(archive,destination)
    assert not (tmp_path/'escape').exists()


def test_atomic_promotion_and_rollback_preserve_releases_and_guard_incomplete_link(tmp_path,monkeypatch):
    import demo_v2.deploy.update_release as updater
    monkeypatch.setattr(updater,'ROOT',tmp_path)
    old=tmp_path/'old';new=tmp_path/'new';old.mkdir();new.mkdir()
    (tmp_path/'current').symlink_to(old)
    updater.promote(new)
    assert (tmp_path/'current').resolve()==new and old.is_dir()
    updater.promote(old)
    assert (tmp_path/'current').resolve()==old and new.is_dir()
    (tmp_path/'current.next').symlink_to(new)
    with pytest.raises(RuntimeError,match='unfinished'):
        updater.promote(new)
    assert (tmp_path/'current').resolve()==old


def test_deployment_preserves_private_preferences_sessions_and_trade_serials(repository,rule,monkeypatch):
    import demo_v2.deploy.update_release as updater
    from conftest import open_position
    monkeypatch.setattr(updater,'DATABASE',repository.database.config.db_path)
    open_position(repository,rule)
    with repository.database.transaction() as conn:
        conn.execute("INSERT INTO demo_v2_meta VALUES ('notification_settings','fake-test-preferences')")
    before=updater.preserved_settings()
    open_position(repository,rule,2)
    updater.verify_settings(before)  # a new legitimate trade may advance the high-water mark
    with repository.database.transaction() as conn:
        conn.execute("UPDATE demo_v2_meta SET value='changed-fake-preferences' WHERE key='notification_settings'")
    with pytest.raises(RuntimeError,match='preferences'):
        updater.verify_settings(before)
