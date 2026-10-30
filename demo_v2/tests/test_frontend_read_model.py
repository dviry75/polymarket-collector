"""The rule table needs lifetime realized P&L, never a paginated browser sum."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from conftest import open_position
from demo_v2.app import create_app
from demo_v2.domain.trading import ExitReason, RuleSpec
from test_api import app_for, login, PREFIX
from test_app_isolation import running_client


def test_rule_realized_pnl_is_exact_lifetime_and_excludes_open(tmp_path, repository, rule):
    first = open_position(repository, rule, 1)
    second = open_position(repository, rule, 2)
    open_position(repository, rule, 3)
    repository.close_position(first["id"], price=Decimal("0.8"), sell_fee=Decimal("0.00000001"),
        reason=ExitReason.TAKE_PROFIT, occurred_at=datetime.now(timezone.utc), provenance={})
    repository.close_position(second["id"], price=Decimal("0.7"), sell_fee=Decimal("0.01"),
        reason=ExitReason.STOP_LOSS, occurred_at=datetime.now(timezone.utc), provenance={})
    other=repository.create_rule(RuleSpec(name="Unrelated", markets=("ETH-1h",), entry_trigger="0.74"))
    config=app_for(tmp_path).state.config
    app=create_app(replace(config,data_dir=repository.database.config.data_dir,db_path=repository.database.config.db_path))
    async def run():
        async with running_client(app) as client:
            await login(client)
            # Fetching one trade must not narrow the rule-level totals.
            assert len((await client.get(PREFIX+"/trades?limit=1")).json()["items"])==1
            rows=(await client.get(PREFIX+"/rules")).json()["items"]
            view=next(row for row in rows if row["id"]==rule["id"])
            assert view["net_pnl"]=="0.08999999"
            assert view["trade_count"]==3
            assert Decimal(view["wallet"])==Decimal("0.08999999")-Decimal("3.7")
            assert next(row for row in rows if row["id"]==other["id"])["net_pnl"]=="0"
            assert (await client.get(PREFIX+"/rules/"+rule["id"])).json()["net_pnl"]=="0.08999999"
            assert "net_pnl" in (await client.get('/openapi.json')).json()['components']['schemas']['RuleView']['properties']
    asyncio.run(run())
