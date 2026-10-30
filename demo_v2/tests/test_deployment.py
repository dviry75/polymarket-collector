"""Deployment artifacts and operator helpers, tested only with fake/temp resources."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import stat

from argon2 import PasswordHasher
import pytest

from demo_v2.config import DemoConfig
from demo_v2.deploy.preflight import validate
from demo_v2.deploy.provision_env import write_environment
from demo_v2.deploy.backup_sqlite import backup

DEPLOY=Path(__file__).resolve().parents[1]/'deploy'


def test_private_provisioning_new_hash_and_secret_no_echo_or_overwrite(tmp_path,capsys):
    first,second=tmp_path/'first.env',tmp_path/'second.env'
    write_environment(first,'same-user','only-fake-test-password')
    write_environment(second,'same-user','only-fake-test-password')
    def read(path):
        return {key:json.loads(value) for key,value in (line.split('=',1) for line in path.read_text().splitlines())}
    a,b=read(first),read(second)
    assert stat.S_IMODE(first.stat().st_mode)==0o600
    assert a['DEMO_AUTH_PASSWORD_HASH']!=b['DEMO_AUTH_PASSWORD_HASH']
    assert PasswordHasher().verify(a['DEMO_AUTH_PASSWORD_HASH'],'only-fake-test-password')
    assert len(a['DEMO_SESSION_SECRET'])>=32 and a['DEMO_SESSION_SECRET']!=b['DEMO_SESSION_SECRET']
    assert 'only-fake-test-password' not in first.read_text()
    assert capsys.readouterr().out==''
    with pytest.raises(FileExistsError):write_environment(first,'same-user','fake')
    assert first.read_text()==''.join(key+'='+json.dumps(value)+'\n' for key,value in a.items())


def test_preflight_validates_without_db_or_network(tmp_path):
    config=DemoConfig(data_dir=tmp_path/'uncreated',login_username='fake-user',
        login_password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash('fake-password'),
        session_secret='fake-session-secret-at-least-32-characters',public_data_enabled=True,session_cookie_secure=True)
    validate(config)
    for changes in ({'public_data_enabled':False},{'session_cookie_secure':False},{'session_secret':'short'},
                    {'login_password_hash':'$argon2id$invalid'},{'login_password_hash':'sha256:'+'0'*64},{'login_username':''}):
        with pytest.raises(ValueError):validate(replace(config,**changes))
    assert not config.data_dir.exists()


def test_online_backup_includes_wal_commits_never_overwrites_or_changes_source(repository,rule,tmp_path):
    source=repository.database.config.db_path
    target=tmp_path/'backup.sqlite3'
    # Keep a connection open so committed WAL contents need not have checkpointed.
    with repository.database.connect() as writer:
        before=writer.execute('SELECT COUNT(*) FROM demo_v2_rules').fetchone()[0]
        assert writer.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        backup(source,target)
        with sqlite3.connect(target) as saved:
            assert saved.execute('SELECT COUNT(*) FROM demo_v2_rules').fetchone()[0]==before==1
            assert saved.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert saved.execute('PRAGMA foreign_key_check').fetchall()==[]
        with pytest.raises(FileExistsError):backup(source,target)
        assert writer.execute('SELECT COUNT(*) FROM demo_v2_rules').fetchone()[0]==before
    assert stat.S_IMODE(target.stat().st_mode)==0o600


def test_backup_refuses_non_demo_source(tmp_path):
    wrong=tmp_path/'demo_v2.sqlite3'
    with sqlite3.connect(wrong) as conn:conn.execute('CREATE TABLE unrelated(value TEXT)')
    with pytest.raises(ValueError):backup(wrong,tmp_path/'should-not-exist.sqlite3')
    assert not (tmp_path/'should-not-exist.sqlite3').exists()


def test_unit_and_proxy_keep_single_owner_cookie_prefix_and_isolation():
    unit=(DEPLOY/'polymarket-demo-v2.service').read_text()
    assert '--host 127.0.0.1 --port 8010 --workers 1' in unit and '--reload' not in unit
    assert 'User=polymarket-demo-v2' in unit and 'ProtectHome=true' in unit
    assert 'EnvironmentFile=/etc/polymarket-demo-v2/demo.env' in unit
    assert 'ReadWritePaths=/var/lib/polymarket-demo-v2' in unit and 'UMask=0077' in unit
    api=(DEPLOY/'nginx-demo-api.conf').read_text()
    assert 'proxy_pass http://127.0.0.1:8010;' in api
    assert 'X-Forwarded-For $remote_addr' in api
    assert 'proxy_cache off;' in api and 'proxy_set_header Cookie' not in api
    assert 'proxy_cookie_path' not in api and 'Access-Control-Allow' not in api
    proxy=(DEPLOY/'nginx-demo-poly.conf').read_text()
    assert 'location = / { return 302 /demo/; }' in proxy
    assert 'location ^~ /demo/api/v1/' in proxy and 'try_files $uri $uri/ =404;' in proxy
    assert 'root /opt/polymarket-demo-v2/frontend;' in proxy
    assert 'Upgrade' not in api


def test_cold_service_restart_restores_positions_and_requires_fresh_generation_and_stability(tmp_path,repository,market,rule):
    import asyncio
    from datetime import datetime,timezone
    from demo_v2.app import create_app
    from demo_v2.providers.contracts import Providers
    from conftest import open_position,quote
    from test_api import app_for,login,PREFIX
    from test_app_isolation import running_client
    from test_fee_underlying import verified_fee,context
    open_position(repository,rule,1)
    previous=repository.next_generation()
    class Discovery:
        metadata={}
        issues={}
        async def discover(self,definitions):return [market]
    class Fees:
        async def parameters(self,instance):return verified_fee(instance.instance_id)
    class Underlying:
        async def context(self,instance,metadata):return context(instance)
    class Resolution:
        async def resolve(self,instance):return None
    class Data:
        status='CONNECTED'
        _ws=None
        def __init__(self):self.latest={};self.wanted=();self.stop=asyncio.Event();self.generation=0
        def token_ready(self,token):return token in self.latest
        async def set_subscriptions(self,instances):self.wanted=instances
        async def run(self):
            self.generation=repository.next_generation()
            self.decisions.begin_generation(self.generation)
            await self.stop.wait()
        async def close(self):self.stop.set()
        async def resync(self,reason):self.latest.clear()
    async def run():
        nonlocal previous
        for _ in range(2):
            data=Data();gate_time=[0]
            config=replace(app_for(tmp_path).state.config,data_dir=repository.database.config.data_dir,
                db_path=repository.database.config.db_path,recovery_stability_seconds=2)
            app=create_app(config,providers=Providers(discovery=Discovery(),fees=Fees(),
                underlying=Underlying(),resolution=Resolution(),market_data=data))
            data.decisions=app.state.decisions
            app.state.runtime.stability.clock=lambda:gate_time[0]
            async with running_client(app) as client:
                await login(client)
                for attempt in range(500):
                    if data.wanted and app.state.runtime.discovery_at:break
                    await asyncio.sleep(.002)
                assert data.wanted==(market,) and data.generation>previous
                previous=data.generation
                assert (await client.get(PREFIX+'/rules')).json()['items'][0]['id']==rule['id']
                assert (await client.get(PREFIX+'/trades')).json()['items'][0]['status']=='OPEN'
                assert len(repository.positions())==1 and repository.entry_count(rule['id'],market.instance_id)==1
                # No historical prices or cached generation can grant entry readiness.
                assert not app.state.runtime.entry_gate(rule,market)
                data.latest={market.up_token_id:True,market.down_token_id:True}
                assert not app.state.runtime.entry_gate(rule,market)
                # A fresh post-restart recovered bid does not reconstruct a missed SL.
                app.state.decisions.publish(quote(generation=data.generation,bid='0.72',ask='0.75'))
                for attempt in range(50):
                    if app.state.runtime.engine.generation==data.generation:break
                    await asyncio.sleep(.002)
                assert repository.positions()[0]['status']=='OPEN'
                assert len(repository.positions())==1
                app.state.runtime.entry_gate(rule,market)  # begin a fresh stable interval
                gate_time[0]=2
                assert app.state.runtime.entry_gate(rule,market)
            assert all(task.done() for task in app.state.runtime.all_tasks())
    asyncio.run(run())
