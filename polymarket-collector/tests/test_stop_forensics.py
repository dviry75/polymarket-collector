from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal

from live.stop_forensics import (
    StopForensicRecord,
    StopForensicWriter,
    bounded_ladder,
)


def _record(stage: str = "STOP_FIRST_SEEN") -> StopForensicRecord:
    return StopForensicRecord.capture(
        stage=stage,
        episode_id="trigger-1:position-1",
        position={
            "position_id": "position-1",
            "event_id": "event-1",
            "condition_id": "condition-1",
            "token_id": "token-1",
            "remaining_shares_text": "8",
            "sellable_shares_text": "8",
        },
        update={
            "asset_id": "token-1",
            "best_bid": "0.65",
            "best_bid_size": "3",
            "best_ask": "0.66",
            "best_ask_size": "4",
            "generation": 7,
            "update_number": 44,
            "bids": [
                {"price": "0.65", "size": "3"},
                {"price": "0.64", "size": "6"},
                {"price": "0.63", "size": "4"},
            ],
            "asks": [["0.66", "20"]],
        },
        timing={"receive_wall_ns": 123, "book_update_monotonic": 4.5},
        requested_shares="8",
        min_price="0.01",
        critical_queue_depth=3,
        stop_queue_depth=1,
    )


def test_bounded_ladder_stops_after_buffered_target_and_hard_cap() -> None:
    levels = [(str(Decimal("0.9") - Decimal(index) / 100), "1") for index in range(50)]
    assert len(
        bounded_ladder(
            levels,
            target_shares=Decimal("2"),
            buffer_multiple=Decimal("1.5"),
            max_levels=32,
        )
    ) == 3
    assert len(
        bounded_ladder(
            (("0.5", "1") for _ in range(50)),
            target_shares=Decimal("100"),
            max_levels=5,
        )
    ) == 5


def test_writer_persists_linked_record_and_simulation(tmp_path) -> None:
    path = tmp_path / "forensics.sqlite3"
    writer = StopForensicWriter(str(path), flush_interval_seconds=0.01)
    writer.start()
    assert writer.publish(_record())
    assert writer.stop(timeout=2)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT stage, episode_id, simulated_filled_shares, simulated_vwap, "
        "payload_json FROM stop_forensic_records"
    ).fetchone()
    connection.close()

    assert row[:4] == ("STOP_FIRST_SEEN", "trigger-1:position-1", "8", "0.64375")
    payload = json.loads(row[4])
    assert payload["generation"] == "7"
    assert payload["critical_queue_depth"] == 3
    assert ["receive_wall_ns", "123"] in payload["timing"]
    assert writer.stats()["persisted"] == 1


def test_publish_is_nonblocking_and_drops_when_bounded_queue_is_full(tmp_path) -> None:
    writer = StopForensicWriter(str(tmp_path / "unused.sqlite3"), queue_capacity=2)
    started = time.perf_counter()
    assert writer.publish(_record())
    assert writer.publish(_record("STOP_LATCHED"))
    assert not writer.publish(_record("EXIT_PRE_SUBMIT"))
    assert time.perf_counter() - started < 0.1
    assert writer.stats()["dropped"] == 1


def test_writer_database_failure_never_breaks_publish(tmp_path) -> None:
    invalid_path = tmp_path / "directory"
    invalid_path.mkdir()
    writer = StopForensicWriter(str(invalid_path), flush_interval_seconds=0.01)
    writer.start()
    assert writer.publish(_record())
    assert writer.stop(timeout=2)
    stats = writer.stats()
    assert stats["failed_batches"] == 1
    assert "OperationalError" in stats["last_error"]


def test_capture_is_immutable_and_does_not_retain_book_references() -> None:
    bids = [{"price": "0.65", "size": "8"}]
    record = StopForensicRecord.capture(
        stage="STOP_FIRST_SEEN",
        episode_id="e:p",
        position={"position_id": "p", "remaining_shares_text": "8"},
        update={"bids": bids},
    )
    bids[0]["price"] = "0.01"
    assert record.bids == (("0.65", "8"),)
