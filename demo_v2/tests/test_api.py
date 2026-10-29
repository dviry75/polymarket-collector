import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest

from demo_v2.app import create_app
from demo_v2.auth import DemoAuth
from demo_v2.config import DemoConfig
from demo_v2.domain.trading import ExitReason, RuleSpec
from test_app_isolation import running_client
from conftest import open_position


PREFIX="/demo/api/v1"
FAKE_PASSWORD="only-a-test-password"


def app_for(tmp_path):
    return create_app(DemoConfig(data_dir=tmp_path/"demo",login_username="demo-test",
        login_password_hash="sha256:"+hashlib.sha256(FAKE_PASSWORD.encode()).hexdigest(),session_secret="test-only-session-secret-32-characters"))


async def login(client):
    response=await client.post(PREFIX+"/auth/login",json={"username":"demo-test","password":FAKE_PASSWORD})
    assert response.status_code==200,response.text
    client.headers["X-Demo-CSRF"]=response.json()["csrf_token"]
    return response


def test_login_cookie_csrf_logout_and_revocation(tmp_path):
    app=app_for(tmp_path)
    async def run():
        async with running_client(app) as client:
            client.cookies.set("live_session","test-live-cookie")
            assert (await client.get(PREFIX+"/rules")).status_code==401
            assert (await client.post(PREFIX+"/auth/login",json={"username":"demo-test","password":"bad"})).status_code==401
            response=await login(client)
            header=response.headers["set-cookie"]
            assert "demo_v2_session=" in header and "HttpOnly" in header and "Secure" in header
            assert "Path=/demo/" in header and "SameSite=strict" in header and "Domain=" not in header
            token=client.cookies.get("demo_v2_session")
            assert (await client.get(PREFIX+"/auth/current")).json()["user"]=="demo-test"
            client.headers.pop("X-Demo-CSRF")
            assert (await client.post(PREFIX+"/auth/logout")).status_code==403
            client.headers["X-Demo-CSRF"]=app.state.auth.csrf(token)
            assert (await client.post(PREFIX+"/auth/logout")).status_code==200
            assert app.state.auth.verify(token) is None
            assert (await client.get(PREFIX+"/rules")).status_code==401
    asyncio.run(run())


def test_login_rate_limit_and_missing_config(tmp_path):
    app=app_for(tmp_path)
    async def run():
        async with running_client(app) as client:
            for _ in range(5):
                assert (await client.post(PREFIX+"/auth/login",json={"username":"demo-test","password":"bad"})).status_code==401
            assert (await client.post(PREFIX+"/auth/login",json={"username":"demo-test","password":FAKE_PASSWORD})).status_code==429
        async with running_client(create_app(DemoConfig(data_dir=tmp_path/"empty"))) as client:
            assert (await client.post(PREFIX+"/auth/login",json={"username":"demo-test","password":FAKE_PASSWORD})).status_code==503
    asyncio.run(run())


def test_session_expiry_future_tamper_rotation_and_restart(tmp_path):
    app=app_for(tmp_path)
    app.state.database.migrate()
    auth=app.state.auth
    auth.initialize()
    now=[1000]
    auth.clock=lambda:now[0]
    token=auth.create_session("demo-test")
    restarted=DemoAuth(auth.config,auth.database,clock=lambda:now[0])
    restarted.initialize()
    assert restarted.verify(token)
    assert auth.verify(token+"bad") is None
    now[0]=999
    assert auth.verify(token) is None
    now[0]=1000+auth.config.session_ttl_seconds+1
    assert auth.verify(token) is None
    now[0]=1000
    rotated=DemoAuth(replace(auth.config,login_password_hash="sha256:"+hashlib.sha256(b"other-test-password").hexdigest()),auth.database,clock=lambda:now[0])
    rotated.initialize()
    auth.initialize()  # Rotating back must not resurrect the old token.
    assert auth.verify(token) is None


def test_argon2_and_plaintext_rejection(tmp_path):
    from argon2 import PasswordHasher
    app=app_for(tmp_path)
    config=replace(app.state.config,login_password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash(FAKE_PASSWORD))
    auth=DemoAuth(config,app.state.database)
    assert asyncio.run(auth.validate("demo-test",FAKE_PASSWORD))
    assert asyncio.run(auth.validate("wrong-user",FAKE_PASSWORD)) is None
    assert not DemoAuth(replace(config,login_password_hash=FAKE_PASSWORD),app.state.database).configured()


def test_rule_crud_versions_cas_duplicate_archive_and_validation(tmp_path):
    app=app_for(tmp_path)
    spec=RuleSpec(name="API rule",markets=("BTC-5m","DOGE-15m","ETH-1h"),entry_trigger="0.74")
    async def run():
        async with running_client(app) as client:
            await login(client)
            invalid={**spec.model_dump(mode="json"),"gross_shares":"4.9"}
            assert (await client.post(PREFIX+"/rules",json=invalid)).status_code==422
            assert (await client.post(PREFIX+"/rules",json={**spec.model_dump(mode="json"),"entry_trigger":0.74})).status_code==422
            response=await client.post(PREFIX+"/rules",json=spec.model_dump(mode="json"))
            assert response.status_code==201,response.text
            rule=response.json()
            assert rule["spec"]["entry_frequency"]==1 and rule["spec"]["entry_trigger"]=="0.74"
            rule_id=rule["id"]
            path=PREFIX+"/rules/"+rule_id
            assert (await client.get(path)).json()["id"]==rule_id
            changed={**spec.model_dump(mode="json"),"entry_frequency":2}
            assert (await client.put(path,json={"spec":changed,"expected_version_id":rule["current_version_id"]})).status_code==200
            assert (await client.put(path,json={"spec":changed,"expected_version_id":rule["current_version_id"]})).status_code==409
            assert len((await client.get(path+"/versions")).json()["items"])==2
            assert (await client.post(path+"/disable")).json()["enabled"]==0
            assert (await client.post(path+"/enable")).json()["enabled"]==1
            duplicate=await client.post(path+"/duplicate")
            assert duplicate.status_code==201 and duplicate.json()["enabled"]==0
            assert (await client.delete(path)).json()["archived"]==1
            assert (await client.post(path+"/enable")).status_code==409
            assert len((await client.get(PREFIX+"/rules")).json()["items"])==2
            assert (await client.get(PREFIX+"/rules/missing")).status_code==404
    asyncio.run(run())


def test_trades_kpi_identical_filters_pagination_decimal_and_detail(tmp_path,market,rule,repository):
    # Seed a separate temp app from the repository fixture, never production.
    app=app_for(tmp_path)
    first=open_position(repository,rule,1)
    second=open_position(repository,rule,2,token="eth-down")
    third=open_position(repository,rule,3)
    repository.close_position(first["id"],price=Decimal("0.8"),sell_fee=Decimal("0.01"),
        reason=ExitReason.TAKE_PROFIT,occurred_at=datetime.now(timezone.utc),provenance={})
    repository.close_position(second["id"],price=Decimal("0.74"),sell_fee=Decimal(0),
        reason=ExitReason.TAKE_PROFIT,occurred_at=datetime.now(timezone.utc),provenance={})
    # Reuse fixture database via a configured app factory at exactly its owned path.
    app=create_app(replace(app.state.config,data_dir=repository.database.config.data_dir,db_path=repository.database.config.db_path))
    async def run():
        async with running_client(app) as client:
            await login(client)
            response=await client.get(PREFIX+"/trades",params={"limit":1,"offset":1,"asset":"ETH","timeframe":"1h"})
            assert response.status_code==200,response.text
            page=response.json()
            assert page["total"]==3 and len(page["items"])==1
            assert isinstance(page["items"][0]["entry_price"],str)
            filtered={"rule_id":rule["id"],"asset":"ETH","timeframe":"1h","side":"UP","status":"CLOSED"}
            trades=(await client.get(PREFIX+"/trades",params=filtered)).json()
            stats=(await client.get(PREFIX+"/kpi",params=filtered)).json()
            assert trades["total"]==stats["total_trades"]==1
            assert stats["wallet"]==trades["items"][0]["net_pnl"]=="0.29"
            assert stats["success_rate"]=="100" and stats["total_fees"]=="0.01"
            stats=(await client.get(PREFIX+"/kpi")).json()
            assert stats["positive"]==1 and stats["negative"]==0 and stats["break_even"]==1 and stats["open"]==1
            assert Decimal(stats["wallet"])==repository.wallet()
            detail=(await client.get(PREFIX+"/trades/"+first["id"])).json()
            assert detail["rule_snapshot"]["entry_trigger"]=="0.74" and len(detail["ledger"])==2
            assert (await client.get(PREFIX+"/trades",params={"asset":"BTC"})).json()["total"]==0
            assert (await client.get(PREFIX+"/trades",params={"from_date":"2026-01-01"})).status_code==422
            assert (await client.get(PREFIX+"/trades",params={"limit":201})).status_code==422
            assert (await client.get(PREFIX+"/trades",params={"unknown":"value"})).status_code==422
            assert (await client.get(PREFIX+"/trades/missing")).status_code==404
    asyncio.run(run())


def test_maintenance_confirmation_keeps_rules_then_factory_removes(tmp_path,repository,market,rule):
    open_position(repository,rule)
    settings=replace(app_for(tmp_path).state.config,data_dir=repository.database.config.data_dir,db_path=repository.database.config.db_path)
    app=create_app(settings)
    async def run():
        async with running_client(app) as client:
            assert (await client.post(PREFIX+"/maintenance/reset-trading-data",json={"confirmation":"RESET_TRADING_DATA"})).status_code==401
            await login(client)
            assert (await client.post(PREFIX+"/maintenance/reset-trading-data",json={"confirmation":"wrong"})).status_code==409
            assert len(repository.positions())==1
            assert (await client.post(PREFIX+"/maintenance/reset-trading-data",json={"confirmation":"RESET_TRADING_DATA"})).status_code==200
            assert not repository.positions() and repository.wallet()==0 and len(repository.rules())==1
            assert (await client.post(PREFIX+"/maintenance/factory-reset",json={"confirmation":"FACTORY_RESET_DEMO"})).status_code==200
            assert repository.rules()==[]
            assert len((await client.get(PREFIX+"/markets")).json()["definitions"])==21
            assert (await client.get(PREFIX+"/health")).status_code==200
    asyncio.run(run())


def test_openapi_lists_filters_and_cookie_auth_without_secret_values(tmp_path):
    app=app_for(tmp_path)
    schema=app.openapi()
    params={p["name"] for p in schema["paths"][PREFIX+"/trades"]["get"]["parameters"]}
    assert {"rule_id","asset","timeframe","status","side","from_date","to_date","movement_min","limit","offset"}<=params
    assert schema["components"]["securitySchemes"]["APIKeyCookie"]["in"]=="cookie"
    assert FAKE_PASSWORD not in str(schema) and app.state.config.session_secret not in str(schema)
