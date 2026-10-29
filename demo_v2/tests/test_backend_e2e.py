import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
import json

from demo_v2.app import create_app
from demo_v2.providers.contracts import Providers
from demo_v2.providers.market_ws import PublicMarketWS
from demo_v2.api.schemas import RuleView,TradePage,KPIView,HealthView,MarketList
import demo_v2.runtime as runtime_module
from test_api import app_for,login,PREFIX
from test_app_isolation import running_client
from test_fee_underlying import verified_fee,context


def test_authenticated_backend_with_native_fake_ws_and_ordered_decisions(tmp_path,market,monkeypatch):
    class Discovery:
        metadata={market.instance_id:{}}
        issues={}
        async def discover(self,definitions): return [market]
    class Fees:
        async def parameters(self,instance): return verified_fee(instance.instance_id)
    class Underlying:
        async def context(self,instance,metadata): return context(instance)
    class Resolution:
        async def resolve(self,instance): return None
    class Socket:
        def __init__(self):
            self.queue=asyncio.Queue();self.sent=[]
            self.stamp=int(datetime.now(timezone.utc).timestamp()*1000)-50
        async def send(self,raw):
            self.sent.append(raw)
            if raw=="PING":
                self.queue.put_nowait("PONG")
                return
            message=json.loads(raw)
            if message.get("type")=="market":
                self.snapshot(market.up_token_id,"0.73","0.75")
                self.snapshot(market.down_token_id,"0.24","0.26")
        def snapshot(self,token,bid,ask):
            self.stamp+=1
            self.queue.put_nowait(json.dumps({"event_type":"book","asset_id":token,"market":market.condition_id,
                "timestamp":str(self.stamp),
                "bids":[{"price":bid,"size":"100"}],"asks":[{"price":ask,"size":"100"}]}))
        async def recv(self):
            message=await self.queue.get()
            if message is None: raise ConnectionError("fake disconnect")
            return message
        async def close(self): self.queue.put_nowait(None)
    async def run():
        socket=Socket()
        class Connection:
            async def __aenter__(self): return socket
            async def __aexit__(self,*args): await socket.close()
        monkeypatch.setattr(runtime_module,"PublicMarketWS",lambda *args,**kwargs:PublicMarketWS(
            *args,**kwargs,connector=lambda *a,**k:Connection()))
        config=replace(app_for(tmp_path).state.config,recovery_stability_seconds=0,telemetry_capacity=1)
        app=create_app(config,providers=Providers(discovery=Discovery(),fees=Fees(),underlying=Underlying(),resolution=Resolution()))
        async with running_client(app) as client:
            await login(client)
            response=await client.post(PREFIX+"/rules",json={"name":"end-to-end","markets":[market.definition.key],
                "entry_trigger":"0.74","gross_shares":"5","entry_frequency":2,"take_profit":"0.96","stop_loss":"0.66"})
            assert response.status_code==201
            RuleView.model_validate(response.json())
            for _ in range(1000):
                data=app.state.runtime.providers.market_data
                if data.token_ready(market.down_token_id): break
                await asyncio.sleep(0.001)
            assert data.token_ready(market.up_token_id)
            # Telemetry capacity one cannot select which of these decisions survives.
            for ask in ("0.74","0.73","0.74"):
                socket.snapshot(market.up_token_id,"0.72",ask)
            for _ in range(1000):
                if len(app.state.repository.positions())==2: break
                await asyncio.sleep(0.001)
            assert len(app.state.repository.positions(status="OPEN"))==2
            socket.snapshot(market.up_token_id,"0.97","0.98")
            for _ in range(1000):
                if not app.state.repository.positions(status="OPEN"): break
                await asyncio.sleep(0.001)
            trades=TradePage.model_validate((await client.get(PREFIX+"/trades")).json())
            stats=KPIView.model_validate((await client.get(PREFIX+"/kpi")).json())
            assert trades.total==2 and all(row.exit_price=="0.97" for row in trades.items)
            assert stats.positive==2 and stats.open==0
            assert Decimal(stats.wallet)==sum(Decimal(row.net_pnl) for row in trades.items)
            HealthView.model_validate((await client.get(PREFIX+"/health")).json())
            MarketList.model_validate((await client.get(PREFIX+"/markets")).json())
            assert (await client.post(PREFIX+"/maintenance/reset-trading-data",json={"confirmation":"RESET_TRADING_DATA"})).status_code==200
            assert (await client.get(PREFIX+"/trades")).json()["total"]==0
            assert len((await client.get(PREFIX+"/rules")).json()["items"])==1
        assert all(task.done() for task in app.state.runtime.all_tasks())
    asyncio.run(run())
