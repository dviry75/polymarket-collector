import asyncio
import base64
from dataclasses import replace
import hashlib
import hmac
import json
import stat

from argon2 import PasswordHasher
import pytest

from demo_v2.auth import DemoAuth
from demo_v2.config import DemoConfig
from demo_v2.deploy.preflight import validate
from demo_v2.deploy.provision_env import add_user, write_environment
from test_api import app_for, login, PREFIX, FAKE_PASSWORD
from test_app_isolation import running_client


def fake_hash(value):return 'sha256:'+hashlib.sha256(value.encode()).hexdigest()


def test_additional_config_is_demo_only_immutable_private_and_fail_closed(tmp_path):
    encoded=json.dumps({'admin':fake_hash('fake-extra-password')})
    config=DemoConfig.from_env({'DEMO_DATA_DIR':str(tmp_path),'DEMO_AUTH_USERNAME':'primary',
        'DEMO_AUTH_USERS_JSON':encoded})
    assert config.additional_users==(('admin',fake_hash('fake-extra-password')),)
    assert encoded not in repr(config) and 'admin' not in repr(config)
    for value in ('[]','null','{"admin":"plaintext"}',
                  '{"admin":"sha256:'+'0'*64+'","admin":"sha256:'+'1'*64+'"}',
                  json.dumps({'primary':fake_hash('fake')})):
        with pytest.raises(ValueError):
            DemoConfig.from_env({'DEMO_DATA_DIR':str(tmp_path),'DEMO_AUTH_USERNAME':'primary','DEMO_AUTH_USERS_JSON':value})


def test_add_account_preserves_legacy_primary_sessions_and_binds_new_tokens_to_account(tmp_path):
    app=app_for(tmp_path);app.state.database.migrate();primary=app.state.auth;primary.initialize()
    # Construct the previously shipped signed payload, without a per-user credential field.
    payload={'sub':'demo-test','iat':int(primary.clock()),'version':primary.version(),'nonce':'fake','aud':'DEMO_V2'}
    encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    legacy=encoded+'.'+hmac.new(primary.config.session_secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    config=replace(primary.config,additional_users=(('admin',fake_hash('fake-extra-password')),))
    auth=DemoAuth(config,primary.database);auth.initialize()
    assert auth.verify(legacy).subject=='demo-test'
    assert asyncio.run(auth.validate('admin','fake-extra-password')).subject=='admin'
    assert asyncio.run(auth.validate('admin',FAKE_PASSWORD)) is None
    assert asyncio.run(auth.validate('unknown',FAKE_PASSWORD)) is None
    token=auth.create_session('admin')
    assert auth.verify(token).subject=='admin'
    with pytest.raises(ValueError):auth.create_session('unknown')
    for users in ((),(('admin',fake_hash('fake-rotated-password')),)):
        changed=DemoAuth(replace(config,additional_users=users),primary.database);changed.initialize()
        assert changed.verify(token) is None
        assert changed.verify(legacy).subject=='demo-test'
    payload['sub']='admin'
    encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    unsigned_credential=encoded+'.'+hmac.new(config.session_secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    assert auth.verify(unsigned_credential) is None


def test_additional_user_normal_login_current_csrf_and_shared_workspace(tmp_path):
    from demo_v2.app import create_app
    from demo_v2.domain.trading import RuleSpec
    app=create_app(replace(app_for(tmp_path).state.config,additional_users=(('admin',fake_hash('fake-extra-password')),)))
    async def run():
        async with running_client(app) as client:
            await login(client)
            primary_token=client.cookies.get('demo_v2_session')
            response=await client.post(PREFIX+'/auth/login',json={'username':'admin','password':'fake-extra-password'})
            assert response.status_code==200 and response.json()['user']=='admin'
            assert (await client.get(PREFIX+'/auth/current')).json()['user']=='admin'
            client.headers.pop('X-Demo-CSRF')
            spec=RuleSpec(name='Shared rule',markets=('ETH-1h',),entry_trigger='0.74').model_dump(mode='json')
            assert (await client.post(PREFIX+'/rules',json=spec)).status_code==403
            client.headers['X-Demo-CSRF']=response.json()['csrf_token']
            assert (await client.post(PREFIX+'/rules',json=spec)).status_code==201
            assert app.state.auth.verify(primary_token).subject=='demo-test'
            client.cookies.set('demo_v2_session',primary_token,path='/demo/')
            assert len((await client.get(PREFIX+'/rules')).json()['items'])==1
    asyncio.run(run())


def test_private_env_add_preserves_original_values_backups_and_rejects_replacement(tmp_path,capsys):
    path=tmp_path/'demo.env'
    write_environment(path,'primary','fake-original-password')
    original=path.read_text()
    password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash('fake-extra-password')
    add_user(path,'admin',password_hash)
    assert path.read_text().startswith(original)
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    saved=next(tmp_path.glob('demo.env.before-user-*'))
    assert saved.read_text()==original and stat.S_IMODE(saved.stat().st_mode)==0o600
    values={key:json.loads(value) for key,value in (line.split('=',1) for line in path.read_text().splitlines())}
    config=DemoConfig.from_env(values)
    validate(config)
    assert dict(config.additional_users)['admin']==password_hash
    assert PasswordHasher().verify(dict(config.additional_users)['admin'],'fake-extra-password')
    before=path.read_bytes()
    for username,value in (('primary',password_hash),('admin',password_hash),('other','plaintext')):
        with pytest.raises(ValueError):add_user(path,username,value)
        assert path.read_bytes()==before
    assert 'fake-extra-password' not in path.read_text()
    assert capsys.readouterr().out==''
    with pytest.raises(ValueError):validate(replace(config,additional_users=(('other',fake_hash('fake')),)))
