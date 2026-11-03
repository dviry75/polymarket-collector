"""Browser tests only: loopback, disposable DB, fake credentials/quotes/positions.
Never reads dotenv/production settings and never contacts a public provider.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets
import tempfile

import uvicorn
from fastapi import Depends

from demo_v2.app import create_app
from demo_v2.config import DemoConfig
from demo_v2.api.dependencies import require_demo_session
from demo_v2.domain.models import MarketInstance, MarketObservation, market_definitions
from demo_v2.domain.trading import RuleSpec, EntryAmounts, ExitReason
from demo_v2.health import ComponentHealth, HealthState
from demo_v2.providers.contracts import Providers


class FakeBrowserBooks:
    status="CONNECTED"
    """Static test snapshots intentionally become stale during the browser test."""
    def __init__(self):
        self.latest={}
        self.token_map={}

    def token_ready(self, token):
        quote=self.latest.get(token)
        return bool(quote and (datetime.now(timezone.utc)-quote.observed_at).total_seconds()<5)

    def health(self):
        return {"status":"TEST_FIXTURE","generation":1,"reason":"FAKE_BROWSER_TEST","tokens":list(self.token_map)}

    async def resync(self, reason):
        self.latest.clear()

    async def set_subscriptions(self, subscriptions):
        self.token_map={}

    async def close(self):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix="demo-frontend-browser-") as folder:
        config=DemoConfig(data_dir=Path(folder),login_username="frontend-e2e",
            login_password_hash="sha256:"+hashlib.sha256(b"frontend-test-only").hexdigest(),
            session_secret=secrets.token_urlsafe(48),session_cookie_secure=False,public_data_enabled=False,recovery_stability_seconds=0)
        books=FakeBrowserBooks()
        app=create_app(config,providers=Providers(market_data=books))
        repo=app.state.repository
        repo.database.migrate()
        now=datetime.now(timezone.utc)
        definition=next(d for d in market_definitions() if d.key=="ETH-1h")
        market=MarketInstance("BROWSER_FAKE_ETH",definition,"BROWSER_FAKE_CONDITION","BROWSER_FAKE_UP","BROWSER_FAKE_DOWN",
            now-timedelta(minutes=1),now+timedelta(minutes=59),external_id="BROWSER_FAKE_EXTERNAL",slug="browser-fake",
            discovered_at=now,metadata_source="BROWSER_TEST_FIXTURE",verified=True)
        repo.upsert_instance(market,{"feesEnabled":True,"feeSchedule":{"rate":"0.07","rounding":"UNKNOWN"}})
        rule=repo.create_rule(RuleSpec(name="בדיקת מקור Backend",markets=("ETH-1h",),entry_trigger="0.74",entry_frequency=50))
        for i in range(12):
            observation=MarketObservation(market.instance_id,market.up_token_id if i%2 else market.down_token_id,1,i+1,
                now-timedelta(seconds=i),now,"BROWSER_TEST_FIXTURE",Decimal("0.73"),Decimal("0.74"))
            position=repo.open_position(rule_id=rule["id"],version_id=rule["current_version_id"],observation=observation,
                amounts=EntryAmounts(Decimal(5),Decimal("3.7"),Decimal(0),Decimal(0),Decimal(5),Decimal("3.7")),
                fee_evidence={"state":"VALID","source":"BROWSER_TEST_FIXTURE"},provenance={"source":"BROWSER_TEST_FIXTURE"},idempotency_key="BROWSER_FAKE_"+str(i))
            if i<8:
                repo.close_position(position["id"],price=Decimal("0.8") if i%2 else Decimal("0.6"),sell_fee=Decimal("0.01"),
                    reason=ExitReason.TAKE_PROFIT if i%2 else ExitReason.STOP_LOSS,occurred_at=now,provenance={"source":"BROWSER_TEST_FIXTURE"})
        for token,bid,ask in [(market.up_token_id,"0.73","0.74"),(market.down_token_id,"0.25","0.26")]:
            books.latest[token]=MarketObservation(market.instance_id,token,1,100,now,now,"BROWSER_TEST_FIXTURE",Decimal(bid),Decimal(ask))
            books.token_map[token]=market.instance_id
        original=app.router.lifespan_context
        @asynccontextmanager
        async def seeded(application):
            async with original(application):
                app.state.runtime.current[definition.key]=market.instance_id
                yield
        app.router.lifespan_context=seeded
        # Test-only ingress. It is defined in this disposable fixture, never in
        # the production app/router. Feed the real FIFO/runtime consumer.
        @app.post("/demo/api/v1/__test__/observation",dependencies=[Depends(require_demo_session)])
        async def fresh_observation(body: dict):
            runtime=app.state.runtime
            stamp=datetime.now(timezone.utc)
            runtime.discovery_at=stamp
            runtime.current[definition.key]=market.instance_id
            generation=await runtime.command("next_generation")
            app.state.decisions.begin_generation(generation)
            samples=[]
            for sequence,(token,bid,ask) in enumerate([
                (market.down_token_id,"0.25","0.26"),
                (market.up_token_id,body.get("bid","0.73"),body["ask"])],1):
                observation=MarketObservation(market.instance_id,token,generation,sequence,
                    stamp,stamp,"BROWSER_FAKE_FEE_ENABLED_FEED",Decimal(bid),Decimal(ask))
                books.latest[token]=observation
                books.token_map[token]=market.instance_id
                samples.append(observation)
            for observation in samples:
                app.state.decisions.publish(observation)
            for _ in range(100):
                if runtime.engine.generation==generation and runtime.engine.last_sequence==2:
                    break
                await asyncio.sleep(0.01)
            return {"generation":generation,"processed":runtime.engine.last_sequence,
                "engine_reason":runtime.engine.reason,"entry_gates":[runtime.entry_gate(r,market) for r in runtime.projection["rules"]],
                "critical_health":[c.reason for c in runtime.health.snapshot().components if c.critical and c.reason],
                "fees_enabled":json.loads(repo.instance(market.instance_id)["metadata_json"])["feesEnabled"],
                "exact_fee_provider_configured":runtime.providers.fees is not None}

        uvicorn.run(app,host="127.0.0.1",port=52971,log_level="error",access_log=False)


if __name__=="__main__":
    main()
