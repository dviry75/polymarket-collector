import asyncio
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from live.adapters.mock import MockTradingAdapter
from live.config import LiveConfig
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository
from live.strategy_runtime import LiveStrategyRuntime


class _RecordingSellAdapter(MockTradingAdapter):
    def __init__(self, response=None):
        super().__init__(scenario="delayed")
        self.create_calls = []
        self.response = response

    async def create_order(self, order):
        self.create_calls.append(dict(order))
        if self.response is not None:
            return dict(self.response)
        return {"success": True, "status": "delayed",
                "polymarket_order_id": f"remote-{len(self.create_calls)}"}


async def _ok_reconcile(_reason):
    return {"status": "ok"}


def _case(name, *, bids, best_bid, shares=Decimal("5"), sellable=None,
          paper=True, adapter=None, reconciliation=None):
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "live.sqlite3")
    base.migrate()
    repo = StrategyRepository(base)
    repo.migrate()
    event_id = f"btc-updown-5m-{name}"
    condition_id = f"cond-{name}"
    token_id = f"tok-{name}"
    base.upsert_market({
        "event_id": event_id, "condition_id": condition_id,
        "yes_token_id": token_id, "no_token_id": f"no-{name}",
        "token_mapping_status": "verified", "accepting_orders": True,
        "min_order_size": "1", "min_tick_size": "0.01", "taker_base_fee": 0,
    })
    repo.reserve_event_entry(
        event_id=event_id, condition_id=condition_id, token_id=token_id,
        side="YES", simultaneous=False, reason_code="ENTRY_PRICE_EXACT",
    )
    position = repo.open_position(
        event_id=event_id, condition_id=condition_id, token_id=token_id,
        outcome="YES", shares=shares, average_price=Decimal("0.74"),
        cost_all_in=Decimal("3.70"), fees=Decimal("0"),
        sellable_shares=shares if sellable is None else sellable,
        min_sellable=Decimal("1"),
    )
    config = LiveConfig(
        live_module_enabled=True,
        execution_mode="PAPER_TRADING" if paper else "READ_ONLY",
        paper_trading_enabled=paper, stop_loss_retry_delay_ms=0,
    )
    runtime = LiveStrategyRuntime(
        config, base, repo, adapter or MockTradingAdapter(),
        reconciliation=reconciliation,
    )
    update = {
        "asset_id": token_id,
        "best_bid": best_bid,
        "generation": 7,
        "update_number": 7,
        "message_hash": f"hash-{name}",
        "exchange_timestamp_ms": 1_700_000_000_000,
        "exchange_age_ms": 15,
        "receive_latency_ms": 6,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": "0.72", "size": "9"}],
        "received_at": "2026-09-06T12:00:00.000+00:00",
    }
    return temporary, base, repo, runtime, position, update


class ExitForensicsIntegrationTests(unittest.TestCase):
    def _finish(self, runtime):
        runtime._exit_evidence.drain_pending()
        runtime._exit_evidence.reconcile_active(set())
        runtime._exit_evidence.drain_pending()

    def test_bad_paper_stop_exit_produces_deep_audit(self):
        temp, base, repo, runtime, position, update = _case(
            "bad", bids=[("0.50", "5")], best_bid="0.50",
        )
        try:
            asyncio.run(runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="f1",
            ))
            self.assertEqual(
                repo.position_for_token(position["token_id"])["state"], "CLOSED"
            )
            self._finish(runtime)

            row = repo.exit_audit(position["position_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["exit_purpose"], "STOP_066")
            self.assertEqual(row["latch_source"], "supervisor")
            self.assertEqual(row["cross_best_bid_text"], "0.50")
            self.assertEqual(row["cross_book_hash"], "hash-bad")
            self.assertEqual(row["exit_outcome"], "BAD_EXIT")
            self.assertEqual(row["deep_capture"], 1)
            self.assertEqual(Decimal(row["actual_fill_vwap_text"]), Decimal("0.50"))
            self.assertIsNotNone(row["submitted_at"])
            self.assertEqual(row["min_price_floor_text"], "0.01")

            snaps = {
                s["phase"]: s
                for s in repo.exit_book_snapshots(row["exit_audit_id"])
            }
            self.assertLessEqual({"CROSS", "SUBMIT"}, set(snaps))
            self.assertIn("0.50", snaps["SUBMIT"]["bids_json"])
        finally:
            temp.cleanup()

    def test_clean_paper_stop_exit_is_light_row_only(self):
        temp, base, repo, runtime, position, update = _case(
            "ok", bids=[("0.64", "50")], best_bid="0.64",
        )
        try:
            asyncio.run(runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="f1",
            ))
            self._finish(runtime)
            row = repo.exit_audit(position["position_id"])
            self.assertEqual(row["exit_outcome"], "ACCEPTABLE")
            self.assertEqual(row["deep_capture"], 0)
            self.assertEqual(Decimal(row["actual_fill_vwap_text"]), Decimal("0.64"))
            self.assertEqual(repo.exit_book_snapshots(row["exit_audit_id"]), [])
        finally:
            temp.cleanup()

    def test_no_cross_no_row(self):
        temp, base, repo, runtime, position, update = _case(
            "nocross", bids=[("0.70", "5")], best_bid="0.70",
        )
        try:
            asyncio.run(runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="f1",
            ))
            self._finish(runtime)
            self.assertIsNone(repo.exit_audit(position["position_id"]))
        finally:
            temp.cleanup()

    def test_waiting_sellable_resume_still_captures_submit(self):
        adapter = _RecordingSellAdapter(response={
            "success": False, "status": "blocked",
            "submission_state": "NOT_SUBMITTED",
            "failure_reason": "INSUFFICIENT_BALANCE", "message": "INSUFFICIENT_BALANCE",
        })
        temp, base, repo, runtime, position, update = _case(
            "resume", bids=[("0.66", "5")], best_bid="0.66",
            sellable=Decimal("0"), paper=False, adapter=adapter,
            reconciliation=_ok_reconcile,
        )
        try:
            asyncio.run(runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="f1",
            ))
            # parked WAITING_SELLABLE
            self.assertEqual(
                repo.intent(repo.position_for_token(position["token_id"])
                            ["active_exit_intent_id"])["state"],
                "WAITING_SELLABLE",
            )
            # shares settle; a fresh sub-stop frame resumes the SELL
            adapter.response = None
            repo.reconcile_remote_position(
                event_id=position["event_id"], condition_id=position["condition_id"],
                token_id=position["token_id"], outcome="YES",
                remote_shares=Decimal("5"), average_price=Decimal("0.74"),
            )
            asyncio.run(runtime._refresh_hot_state_once())
            crash = {**update, "best_bid": "0.40",
                     "bids": [{"price": "0.40", "size": "5"}], "message_hash": "h2"}
            asyncio.run(runtime._manage_position(
                market={}, update=crash, event_ready=True, frame_hash="f2",
            ))
            self.assertTrue(adapter.create_calls)
            self._finish(runtime)

            row = repo.exit_audit(position["position_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["exit_purpose"], "STOP_066")
            self.assertIsNotNone(row["submitted_at"])
            self.assertEqual(row["submit_best_bid_text"], "0.40")
            self.assertEqual(row["settlement_wait"], 1)
        finally:
            temp.cleanup()

    def test_operator_emergency_close_captures_submit(self):
        adapter = _RecordingSellAdapter()
        temp, base, repo, runtime, position, update = _case(
            "operator", bids=[("0.50", "5")], best_bid="0.50",
            paper=False, adapter=adapter, reconciliation=_ok_reconcile,
        )

        class _Book:
            ready = True
            generation = 3
            update_number = 3
            last_message_hash = "mh"

            def view(self, **_):
                return {**update, "event_type": "operator_emergency"}

        class _Books:
            books = {position["token_id"]: _Book()}

        try:
            res = asyncio.run(runtime.emergency_close_all(_Books(), actor="op"))
            self.assertEqual(res["status"], "executed")
            self.assertTrue(adapter.create_calls)
            self._finish(runtime)

            row = repo.exit_audit(position["position_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["exit_purpose"], "EMERGENCY_OPERATOR")
            self.assertIsNotNone(row["submitted_at"])
            self.assertEqual(row["submit_best_bid_text"], "0.50")
            self.assertIn("0.50", {
                s["phase"]: s for s in repo.exit_book_snapshots(row["exit_audit_id"])
            }.get("SUBMIT", {}).get("bids_json", ""))
        finally:
            temp.cleanup()

    def test_hooks_never_raise_into_exit_path(self):
        temp, base, repo, runtime, position, update = _case(
            "boom", bids=[("0.50", "5")], best_bid="0.50",
        )
        try:
            def boom(*a, **k):
                raise RuntimeError("collector down")

            runtime._exit_evidence.note_cross = boom
            runtime._exit_evidence.note_latch = boom
            # the SELL must still complete despite telemetry raising
            asyncio.run(runtime._manage_position(
                market={}, update=update, event_ready=True, frame_hash="f1",
            ))
            self.assertEqual(
                repo.position_for_token(position["token_id"])["state"], "CLOSED"
            )
            self.assertIn("EXIT_EVIDENCE_NOTE", runtime.last_error)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
