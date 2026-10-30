"""Explicit loopback-only RC smoke with a disposable DB and fake credentials."""

import argparse
import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import secrets
import socket
import tempfile

from argon2 import PasswordHasher
import httpx
import uvicorn

from demo_v2.app import create_app
from demo_v2.config import DemoConfig
from demo_v2.domain.models import MarketInstance,MarketObservation,market_definitions
from demo_v2.domain.trading import EntryAmounts
from demo_v2.api.schemas import SessionView,RuleView,TradePage,TradeDetail,KPIView,MarketList,HealthView


async def verify(output):
    prefix="/demo/api/v1"
    report={"mode":"LOOPBACK_TEMP_DB_FAKE_CREDENTIALS","checks":{}}
    with tempfile.TemporaryDirectory(prefix="demo-v2-api-rc-") as folder:
        password=secrets.token_urlsafe(24)
        config=DemoConfig(data_dir=Path(folder),login_username="phase9-test",
            login_password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash(password),
            session_secret=secrets.token_urlsafe(40),session_cookie_secure=False)
        app=create_app(config)
        listener=socket.socket()
        listener.bind(("127.0.0.1",0))
        listener.listen(128)
        server=uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False,lifespan="on"))
        task=asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(1000):
                if server.started: break
                if task.done(): await task
                await asyncio.sleep(0.005)
            assert server.started
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",trust_env=False,timeout=5) as client:
                async def request(method,path,body=None,status=200):
                    response=await client.request(method,prefix+path,json=body)
                    assert response.status_code==status,(method,path,response.status_code)
                    assert response.headers["Cache-Control"]=="no-store"
                    report["checks"][method+" "+path]=status
                    return response.json()
                await request("GET","/rules",status=401)
                session=SessionView.model_validate(await request("POST","/auth/login",{"username":"phase9-test","password":password}))
                client.headers["X-Demo-CSRF"]=session.csrf_token
                await request("GET","/auth/current")
                assert (await request("GET","/rules"))["items"]==[]
                spec={"name":"RC_FAKE_FIXTURE","markets":["ETH-1h"],"entry_trigger":"0.74","gross_shares":"5"}
                rule=RuleView.model_validate(await request("POST","/rules",spec,status=201))
                await request("GET","/rules/"+rule.id)
                changed={**spec,"entry_frequency":2}
                await request("PUT","/rules/"+rule.id,{"spec":changed,"expected_version_id":rule.current_version_id})
                await request("POST","/rules/"+rule.id+"/disable")
                await request("POST","/rules/"+rule.id+"/enable")
                await request("POST","/rules/"+rule.id+"/duplicate",status=201)
                await request("GET","/rules/"+rule.id+"/versions")
                repo=app.state.repository
                now=datetime.now(timezone.utc)
                definition=next(d for d in market_definitions() if d.key=="ETH-1h")
                market=MarketInstance("FAKE_RC_MARKET",definition,"FAKE_RC_CONDITION","FAKE_UP","FAKE_DOWN",now-timedelta(minutes=1),
                    now+timedelta(minutes=59),external_id="FAKE_RC_EXTERNAL",slug="fake-rc",discovered_at=now,
                    metadata_source="PHASE9_FAKE_FIXTURE",verified=True)
                repo.upsert_instance(market)
                app.state.runtime.instances[market.instance_id]=market
                current=repo.rule(rule.id)
                observation=MarketObservation(market.instance_id,market.up_token_id,1,1,now,now,"PHASE9_FAKE_FIXTURE",Decimal("0.73"),Decimal("0.74"))
                position=repo.open_position(rule_id=rule.id,version_id=current["current_version_id"],observation=observation,
                    amounts=EntryAmounts(Decimal(5),Decimal("3.7"),Decimal(0),Decimal(0),Decimal(5),Decimal("3.7")),
                    fee_evidence={"state":"VALID","source":"PHASE9_FAKE_FIXTURE"},provenance={"source":"PHASE9_FAKE_FIXTURE"},
                    idempotency_key="PHASE9_FAKE_POSITION")
                page=TradePage.model_validate(await request("GET","/trades?limit=1&offset=0"))
                detail=TradeDetail.model_validate(await request("GET","/trades/"+position["id"]))
                stats=KPIView.model_validate(await request("GET","/kpi"))
                assert page.total==1 and detail.entry_price=="0.74" and stats.wallet=="-3.7"
                assert isinstance(detail.buy_fee,str) and isinstance(detail.net_shares,str)
                MarketList.model_validate(await request("GET","/markets"))
                HealthView.model_validate(await request("GET","/health"))
                await request("DELETE","/rules/"+rule.id)
                assert repo.positions()[0]["status"]=="OPEN"
                await request("POST","/maintenance/reset-trading-data",{"confirmation":"RESET_TRADING_DATA"})
                assert not repo.positions() and len(repo.rules())==2
                assert (await request("GET","/trades"))["total"]==0
                assert (await request("GET","/kpi"))["wallet"]=="0"
                await request("POST","/maintenance/factory-reset",{"confirmation":"FACTORY_RESET_DEMO"})
                assert not repo.rules()
                assert (await request("GET","/rules"))["items"]==[]
                schema=(await client.get("/openapi.json")).json()
                assert all(path in ("/health","/ready") or path.startswith(prefix+"/") for path in schema["paths"])
                assert not any(word in path for path in schema["paths"] for word in ("/order","/cancel","/redeem","/wallet-key"))
                assert {"SessionView","TradePage","TradeDetail","RuleView","KPIView"}<=schema["components"]["schemas"].keys()
                report["openapi_routes"]=list(schema["paths"])
                await request("POST","/auth/logout")
                await request("GET","/auth/current",status=401)
            report.update(result="PASS",decimal_strings=True,real_trading_routes=False,public_provider_calls=0,
                fake_position_only=True)
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,10)
            listener.close()
            report["server_stopped"]=task.done()
            Path(output).write_text(json.dumps(report,sort_keys=True,indent=2)+"\n")
    print(json.dumps({"result":report["result"],"checks":len(report["checks"]),"server_stopped":report["server_stopped"]}))
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    asyncio.run(verify(parser.parse_args().output))
