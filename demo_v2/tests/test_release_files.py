"""Public code access under umask 0077; private state stays outside the release."""
import os
from pathlib import Path

import pytest

from demo_v2.deploy.release_files import grant_release_access
from demo_v2.deploy.vm import guest


def test_restrictive_admin_umask_does_not_hide_code_or_static_files(tmp_path):
    root=tmp_path/'runtime';release=root/'releases'/'candidate'
    old=os.umask(0o077)
    try:
        (release/'app/pkg').mkdir(parents=True)
        (release/'frontend/demo').mkdir(parents=True)
        (release/'venv/bin').mkdir(parents=True)
        code=release/'app/pkg/app.py';code.write_text('pass\n')
        static=release/'frontend/demo/index.html';static.write_text('fake')
        executable=release/'venv/bin/launcher';executable.write_text('fake');executable.chmod(0o700)
        private=root/'deployment-state';private.mkdir();record=private/'private.json';record.write_text('fake-private')
        db=tmp_path/'private.sqlite3';db.write_text('fake-db')
    finally:os.umask(old)
    assert root.stat().st_mode&0o777==0o700
    grant_release_access(root,release)
    for path in (root,root/'releases',release,code.parent,static.parent,executable.parent):
        assert path.stat().st_mode&0o777==0o755
    assert code.stat().st_mode&0o777==static.stat().st_mode&0o777==0o644
    assert executable.stat().st_mode&0o777==0o755
    assert private.stat().st_mode&0o777==0o700
    assert record.stat().st_mode&0o777==db.stat().st_mode&0o777==0o600


def test_permission_walk_does_not_follow_symlinks_to_private_targets(tmp_path):
    root=tmp_path/'runtime';release=root/'releases'/'candidate';release.mkdir(parents=True)
    private=tmp_path/'private';private.mkdir(mode=0o700)
    secret=private/'fake.env';secret.write_text('fake-private');secret.chmod(0o600)
    (release/'linked-file').symlink_to(secret)
    (release/'linked-directory').symlink_to(private,target_is_directory=True)
    grant_release_access(root,release)
    assert secret.stat().st_mode&0o777==0o600
    assert private.stat().st_mode&0o777==0o700


def test_permission_helper_refuses_nonrelease_or_symlinked_release(tmp_path):
    root=tmp_path/'runtime';(root/'releases').mkdir(parents=True)
    private=tmp_path/'private';private.mkdir(mode=0o700)
    with pytest.raises(ValueError):grant_release_access(root,private)
    linked=root/'releases'/'linked';linked.symlink_to(private,target_is_directory=True)
    with pytest.raises(ValueError):grant_release_access(root,linked)
    assert private.stat().st_mode&0o777==0o700


def test_guest_release_check_uses_service_user_and_its_real_working_directory(tmp_path,monkeypatch):
    root=tmp_path/'runtime';release=root/'releases'/'candidate'
    target=release/'app/demo_v2/deploy/release_files.py';target.parent.mkdir(parents=True)
    source=Path(__file__).resolve().parents[1]/'deploy/release_files.py'
    target.write_text(source.read_text())
    monkeypatch.setattr(guest,'ROOT',root)
    calls=[]
    monkeypatch.setattr(guest,'run',lambda *args,**kwargs:calls.append((args,kwargs)))
    guest.check_release_access(release)
    args,kwargs=calls[0]
    assert args[:4]==('runuser','-u','polymarket-demo-v2','--')
    assert kwargs['cwd']==release/'app'
    assert not any(key.startswith(('LIVE_','DEMO_AUTH_','DEMO_SESSION_')) for key in kwargs['env'])
    assert all(args[:4]==('runuser','-u','www-data','--') for args,_ in calls[1:])
