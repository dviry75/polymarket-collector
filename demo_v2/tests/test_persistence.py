from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from demo_v2.domain.models import OfficialResolution
from demo_v2.domain.trading import ExitReason, RuleSpec
from demo_v2.persistence.repository import DemoRepository
from conftest import amounts, open_position, quote


def test_catalog_wal_foreign_keys_and_idempotent_migrations(repository):
    repository.database.migrate()
    assert len(repository.definitions()) == 21
    with repository.database.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_schema").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO demo_v2_entry_counts VALUES ('missing','missing',1)")


def test_migration_history_tampering_is_rejected(repository):
    with repository.database.transaction() as conn:
        conn.execute("UPDATE demo_v2_schema SET checksum='invalid'")
    with pytest.raises(ValueError, match="migration history"):
        repository.database.migrate()


def test_verified_identity_cannot_change_or_guess_tokens(repository, market):
    with pytest.raises(ValueError, match="verified"):
        repository.upsert_instance(replace(market, instance_id="unknown", verified=False))
    with pytest.raises(ValueError, match="identity changed"):
        repository.upsert_instance(replace(market, up_token_id="different-token"))
    with pytest.raises(ValueError, match="already bound"):
        repository.upsert_instance(replace(market, instance_id="other", external_id="other", condition_id="other"))


def test_concurrent_positions_shared_side_quota_and_restart(repository, market, rule):
    up = open_position(repository, rule, 1)
    down = open_position(repository, rule, 2, token="eth-down")
    assert up["id"] != down["id"]
    assert repository.entry_count(rule["id"], market.instance_id) == 2
    restarted = DemoRepository(repository.database)
    assert len(restarted.positions(status="OPEN")) == 2
    open_position(restarted, rule, 3)
    with pytest.raises(ValueError, match="frequency"):
        open_position(restarted, rule, 4, token="eth-down")
    assert restarted.wallet() == Decimal("-11.10")
    assert restarted.wallet(rule["id"]) == restarted.wallet()


def test_position_version_and_exit_snapshot_survive_edit_archive(repository, market, rule):
    position = open_position(repository, rule)
    changed = RuleSpec.model_validate({**rule["spec"].model_dump(), "take_profit": "0.99", "enabled": False, "archived": True})
    updated = repository.update_rule(rule["id"], changed)
    assert updated["version"] == 2
    assert repository.positions()[0]["rule_version_id"] == position["rule_version_id"]
    assert repository.positions()[0]["tp_text"] == "0.96"
    with repository.database.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE demo_v2_rule_versions SET snapshot_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM demo_v2_rule_versions")
    with pytest.raises(ValueError, match="eligible"):
        open_position(repository, updated, 2)
    closed = repository.close_position(position["id"], price=Decimal("0.97"), sell_fee=Decimal(0),
        reason=ExitReason.TAKE_PROFIT, occurred_at=datetime.now(timezone.utc), provenance={"source":"observed"})
    assert closed["status"] == "CLOSED"


def test_idempotent_entry_exit_and_break_even_cash_reconciliation(repository, market, rule):
    position = open_position(repository, rule)
    assert open_position(repository, rule)["id"] == position["id"]
    assert repository.entry_count(rule["id"], market.instance_id) == 1
    assert repository.wallet() == Decimal("-3.70")
    for _ in range(2):
        closed = repository.close_position(position["id"], price=Decimal("0.74"), sell_fee=Decimal(0),
            reason=ExitReason.TAKE_PROFIT, occurred_at=datetime.now(timezone.utc), provenance={"source":"test"})
    assert closed["result"] == "BREAK_EVEN"
    assert Decimal(closed["net_pnl_text"]) == repository.wallet() == 0
    with repository.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_ledger").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE demo_v2_ledger SET amount_text='100'")


def test_resolution_settles_both_sides_and_never_invents_sell_fee(repository, market, rule):
    up = open_position(repository, rule, 1)
    down = open_position(repository, rule, 2, token="eth-down")
    resolution = OfficialResolution(market.instance_id, market.up_token_id, datetime.now(timezone.utc), "fake-official")
    assert repository.resolve(resolution) == 2
    assert repository.resolve(resolution) == 0
    positions = {row["id"]:row for row in repository.positions()}
    assert positions[up["id"]]["exit_price_text"] == "1"
    assert positions[down["id"]]["exit_price_text"] == "0"
    assert all(row["sell_fee_text"] == "0" for row in positions.values())
    assert repository.wallet() == sum((Decimal(row["net_pnl_text"]) for row in positions.values()), Decimal(0))
    with pytest.raises(ValueError, match="conflicting"):
        repository.resolve(replace(resolution, winner_token_id=market.down_token_id))


def test_entry_rollback_covers_counter_position_runtime_and_ledger(repository, market, rule, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected ledger failure")
    monkeypatch.setattr(repository, "_ledger", fail)
    with pytest.raises(RuntimeError, match="injected"):
        open_position(repository, rule)
    assert repository.positions() == []
    assert repository.entry_count(rule["id"], market.instance_id) == 0
    assert repository.wallet() == 0
    with repository.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM demo_v2_entry_runtime").fetchone()[0] == 0


def test_exit_and_resolution_rollback(repository, market, rule, monkeypatch):
    position = open_position(repository, rule)
    before = repository.wallet()
    def fail(*args, **kwargs):
        raise RuntimeError("injected credit failure")
    monkeypatch.setattr(repository, "_ledger", fail)
    with pytest.raises(RuntimeError):
        repository.resolve(OfficialResolution(market.instance_id, market.up_token_id, datetime.now(timezone.utc), "fake-official"))
    assert repository.positions()[0]["status"] == "OPEN"
    assert repository.instance(market.instance_id)["resolution_status"] == "PENDING"
    assert repository.wallet() == before


def test_reset_with_open_positions_is_atomic_and_preserves_rules(repository, market, rule):
    open_position(repository, rule)
    generation = repository.next_generation()
    repository.reset()
    assert not repository.positions() and repository.wallet() == 0
    assert repository.rule(rule["id"])["version"] == 1
    assert repository.entry_count(rule["id"], market.instance_id) == 0
    assert repository.next_generation() > generation
    repository.reset(factory=True)
    assert repository.rules() == []
    assert len(repository.definitions()) == 21


def test_independent_rule_wallets(repository, market, rule):
    other = repository.create_rule(rule["spec"])
    open_position(repository, rule)
    open_position(repository, other)
    assert repository.wallet() == repository.wallet(rule["id"])+repository.wallet(other["id"])
    assert repository.wallet() < 0


def test_next_instance_has_its_own_shared_counter(repository, market, rule):
    open_position(repository, rule)
    next_market = replace(market, instance_id="eth-next", external_id="external-next", condition_id="condition-next",
                          up_token_id="next-up", down_token_id="next-down")
    repository.upsert_instance(next_market)
    repository.open_position(rule_id=rule["id"], version_id=rule["current_version_id"],
        observation=quote(instance_id=next_market.instance_id, token="next-down"), amounts=amounts(),
        fee_evidence={"state":"VALID"}, provenance={}, idempotency_key="next-instance-entry")
    assert repository.entry_count(rule["id"], market.instance_id) == 1
    assert repository.entry_count(rule["id"], next_market.instance_id) == 1


def test_unknown_zero_fee_is_not_valid_evidence(repository, market, rule):
    with pytest.raises(ValueError, match="fee evidence"):
        repository.open_position(rule_id=rule["id"], version_id=rule["current_version_id"],
            observation=quote(), amounts=amounts(), fee_evidence={"state":"UNKNOWN","rate":"0"},
            provenance={}, idempotency_key="invalid-fees")
    assert repository.wallet() == 0


@pytest.mark.parametrize("changes", [
    {"gross_shares":"4.99"}, {"entry_frequency":0}, {"entry_frequency":True},
    {"entry_trigger":0.74}, {"entry_trigger":"NaN"}, {"markets":["BTC-1d"]},
    {"weekly_schedule":[{"weekday":1,"start":"10:00","end":"10:00"}]},
    {"entry_windows":[{"start_seconds":10,"end_seconds":5}]},
])
def test_rule_validation_rejects_invalid_or_binary_float_financial_values(changes):
    with pytest.raises(ValueError):
        RuleSpec.model_validate({"name":"test","markets":["ETH-1h"],"entry_trigger":"0.74",**changes})
