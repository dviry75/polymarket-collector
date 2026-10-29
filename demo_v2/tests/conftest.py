from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from demo_v2.config import DemoConfig
from demo_v2.domain.models import MarketInstance, MarketObservation, market_definitions
from demo_v2.domain.trading import EntryAmounts, RuleSpec
from demo_v2.persistence.database import DemoDatabase
from demo_v2.persistence.repository import DemoRepository


@pytest.fixture
def repository(tmp_path):
    database = DemoDatabase(DemoConfig(data_dir=tmp_path / "demo"))
    database.migrate()
    return DemoRepository(database)


@pytest.fixture
def market(repository):
    now = datetime.now(timezone.utc)
    definition = next(item for item in market_definitions() if item.key == "ETH-1h")
    instance = MarketInstance("eth-hour", definition, "condition-eth", "eth-up", "eth-down",
        now-timedelta(minutes=1), now+timedelta(minutes=59), external_id="external-eth",
        slug="verified-eth-hour", discovered_at=now, metadata_source="fake-public", verified=True)
    repository.upsert_instance(instance)
    return instance


@pytest.fixture
def rule(repository, market):
    return repository.create_rule(RuleSpec(name="ETH test", markets=(market.definition.key,),
        entry_trigger="0.74", gross_shares="5", entry_frequency=3, take_profit="0.96", stop_loss="0.66"))


def quote(sequence=1, token="eth-up", instance_id="eth-hour", generation=1, ask="0.74", bid="0.73", at=None):
    at = at or datetime.now(timezone.utc)
    return MarketObservation(instance_id, token, generation, sequence, at, at, "fake-ws", Decimal(bid), Decimal(ask))


def amounts(price="0.74", shares="5"):
    shares, price = Decimal(shares), Decimal(price)
    return EntryAmounts(shares, shares*price, Decimal(0), Decimal(0), shares, shares*price)


def open_position(repository, rule, sequence=1, token="eth-up", key=None):
    return repository.open_position(rule_id=rule["id"], version_id=rule["current_version_id"],
        observation=quote(sequence, token), amounts=amounts(), fee_evidence={"state":"VALID","source":"fake"},
        provenance={"source":"test"}, idempotency_key=key or f"entry-{rule['id']}-{sequence}")
