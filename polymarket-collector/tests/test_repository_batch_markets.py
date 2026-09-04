from pathlib import Path
import tempfile

from live.repository import LiveRepository


def _repo():
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    return temporary, base


def _upsert(base, condition_id, *, resolved=False):
    base.upsert_market({
        "event_id": f"event-{condition_id}",
        "condition_id": condition_id,
        "yes_token_id": f"yes-{condition_id}",
        "no_token_id": f"no-{condition_id}",
        "token_mapping_status": "verified",
        "accepting_orders": not resolved,
        "market_resolved": 1 if resolved else 0,
        "min_order_size": "1",
        "min_tick_size": "0.01",
        "taker_base_fee": 0,
    })


def test_latest_markets_batches_and_matches_single_lookups():
    temp, base = _repo()
    try:
        _upsert(base, "cond-a", resolved=True)
        _upsert(base, "cond-b", resolved=False)
        _upsert(base, "cond-c", resolved=True)

        batch = base.latest_markets(
            ["cond-a", "cond-b", "cond-c", "cond-missing", "cond-a", ""]
        )
        assert set(batch) == {"cond-a", "cond-b", "cond-c"}
        for cid in ("cond-a", "cond-b", "cond-c"):
            single = base.latest_market(cid)
            assert single is not None
            assert batch[cid]["condition_id"] == single["condition_id"]
            assert bool(batch[cid]["market_resolved"]) == bool(
                single["market_resolved"]
            )
    finally:
        temp.cleanup()


def test_latest_markets_empty_input_returns_empty():
    temp, base = _repo()
    try:
        assert base.latest_markets([]) == {}
        assert base.latest_markets(["", None]) == {}
    finally:
        temp.cleanup()
