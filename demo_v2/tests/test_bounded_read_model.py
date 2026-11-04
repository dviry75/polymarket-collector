"""Read-side regressions using disposable history, never installed DEMO state."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import tracemalloc

from conftest import open_position
from demo_v2.api.read_model import TradeFilter, TradeQuery, kpi, rule_summary, trade_page
from demo_v2.domain.trading import ExitReason, RuleSpec
from demo_v2.persistence import trade_serials


def test_wide_history_is_paged_before_decoding_and_totals_use_bounded_memory(repository, rule):
    seed = open_position(repository, rule)
    repository.close_position(seed["id"], price=Decimal("0.8"), sell_fee=Decimal("0.00000001"),
        reason=ExitReason.TAKE_PROFIT, occurred_at=datetime.now(timezone.utc), provenance={})
    template = repository.positions()[0]
    template.pop("serial_number")
    template["entry_provenance_json"] = json.dumps({"source": "fake", "evidence": "x" * 65536})
    # More than 15 MiB of evidence, with tied timestamps to exercise ID ordering.
    count = 240
    with repository.database.transaction() as conn:
        for index in range(count):
            row = {**template, "id": f"history-{index:04}", "idempotency_key": f"fake-{index}"}
            conn.execute("INSERT INTO demo_v2_positions (" + ",".join(row) + ") VALUES ("
                         + ",".join("?" for _ in row) + ")", tuple(row.values()))
            trade_serials.allocate(conn, row["id"])
            conn.execute("INSERT INTO demo_v2_ledger VALUES (?,?,?,?,?,?,?,?)", (
                f"cash-{index}", row["entry_at"], row["rule_id"], row["id"], "FAKE_TEST",
                row["net_pnl_text"], f"fake-cash-{index}", "{}"))
    tracemalloc.start()
    try:
        page = trade_page(repository, TradeQuery(limit=3, offset=2))
        stats = kpi(repository, TradeFilter())
        summary = rule_summary(repository, rule["id"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page["total"] == stats["total_trades"] == summary["trade_count"] == count + 1
    assert [row["id"] for row in page["items"]] == ["history-0237", "history-0236", "history-0235"]
    assert all(row["serial_number"] > 0 for row in page["items"])
    assert Decimal(stats["wallet"]) == Decimal(summary["net_pnl"]) == Decimal("0.29999999") * (count + 1)
    assert stats["positive"] == count + 1 and stats["success_rate"] == "100"
    assert Decimal(stats["total_fees"]) == Decimal("0.00000001") * (count + 1)
    assert peak < 3 * 1024 * 1024  # Independent of the >15 MiB historical evidence.
    assert trade_page(repository, TradeQuery(offset=count + 1))["items"] == []


def test_exact_movement_and_offset_date_cohort_matches_page_and_kpi(repository, rule):
    rule = repository.update_rule(rule["id"], RuleSpec.model_validate({**rule["spec"].model_dump(), "entry_frequency": 4}))
    rows = [open_position(repository, rule, sequence) for sequence in range(1, 5)]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # First and second are one instant despite different textual timezone offsets.
    dates = [base, base.astimezone(timezone(timedelta(hours=2))), base + timedelta(seconds=1), base + timedelta(seconds=2)]
    movements = ["1.00000000000000000001", "1.00000000000000000002", "1.00000000000000000001", None]
    with repository.database.transaction() as conn:
        for row, at, movement in zip(rows, dates, movements):
            evidence = {} if movement is None else {"underlying": {"movement": {"percent": movement, "absolute": movement}}}
            conn.execute("UPDATE demo_v2_positions SET entry_at=?,entry_provenance_json=? WHERE id=?",
                         (at.isoformat(), json.dumps(evidence), row["id"]))
    for measurement in ("PERCENT", "ABSOLUTE"):
        filters = dict(asset="ETH", timeframe="1h", rule_id=rule["id"], instance_id=rows[0]["instance_id"],
                       side="UP", status="OPEN", from_date=base, to_date=base + timedelta(seconds=1),
                       movement_measurement=measurement, movement_min=movements[0], movement_max=movements[0])
        page = trade_page(repository, TradeQuery(**filters, limit=1))
        stats = kpi(repository, TradeFilter(**filters))
        assert page["total"] == stats["total_trades"] == 1
        assert page["items"][0]["id"] == rows[0]["id"]
        assert stats["wallet"] == "-3.7" and stats["success_rate"] is None
    assert trade_page(repository, TradeQuery(asset="BTC"))["total"] == 0
    assert kpi(repository, TradeFilter(asset="BTC"))["wallet"] == "0"
