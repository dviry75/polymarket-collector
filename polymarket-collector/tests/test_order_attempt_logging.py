from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import tempfile

from live.adapters.polymarket import RealPolymarketTradingAdapter
from live.config import LiveConfig
from live.order_attempts import OrderAttemptRecorder
from live.repository import LiveRepository
from live.strategy_repository import StrategyRepository


class RequestRejectedError(RuntimeError):
    status_code = 422
    code = "example_rejection"


class FakeTradingClient:
    wallet = "0x2222222222222222222222222222222222222222"
    signer = "0x1111111111111111111111111111111111111111"
    wallet_type = "POLY_PROXY"

    def __init__(self, *, post_result=None, cancel_result=None):
        self.post_result = post_result or {
            "ok": True,
            "status": "matched",
            "order_id": "remote-order-1",
            "transactions_hashes": ["tx-1"],
        }
        self.cancel_result = cancel_result or {
            "canceled": ["remote-order-1"],
            "not_canceled": {},
        }
        self.post_calls = 0
        self.cancel_calls = 0

    async def get_balance_allowance(self, **_kwargs):
        return {
            "balance": 10_000_000,
            "allowances": {"exchange": 10_000_000},
        }

    async def create_limit_order(self, **kwargs):
        return {**kwargs, "signature": "signed-secret-must-not-be-logged"}

    async def create_market_order(self, **kwargs):
        return {**kwargs, "signature": "signed-secret-must-not-be-logged"}

    async def post_order(self, _signed):
        self.post_calls += 1
        if isinstance(self.post_result, BaseException):
            raise self.post_result
        return self.post_result

    async def cancel_order(self, **kwargs):
        self.cancel_calls += 1
        if isinstance(self.cancel_result, BaseException):
            raise self.cancel_result
        return self.cancel_result


def armed_config() -> LiveConfig:
    return LiveConfig(
        trading_mode="LIVE",
        execution_mode="REAL_TRADING",
        live_module_enabled=True,
        live_trading_enabled=True,
        live_order_submission_enabled=True,
        live_adapter="polymarket",
        pause_entries_default=False,
        canary_armed=True,
        live_kill_switch_default=False,
        funder_address=FakeTradingClient.wallet,
        signature_type=1,
    )


def environment(client: FakeTradingClient | None = None):
    temporary = tempfile.TemporaryDirectory()
    base = LiveRepository(Path(temporary.name) / "attempts.sqlite3")
    base.migrate()
    strategy = StrategyRepository(base)
    strategy.migrate()
    adapter = RealPolymarketTradingAdapter(
        armed_config(),
        secure_client=client or FakeTradingClient(),
        attempt_recorder=OrderAttemptRecorder(base),
    )
    return temporary, base, strategy, adapter


def rows(base: LiveRepository):
    with base.connect() as conn:
        result = conn.execute(
            "SELECT * FROM live_order_attempts "
            "ORDER BY occurred_at, record_id"
        ).fetchall()
    return [dict(row) for row in result]


def entry_order(**overrides):
    result = {
        "idempotency_key": "intent-entry-1",
        "durable_intent_reserved": True,
        "event_id": "event-1",
        "condition_id": "condition-1",
        "token_id": "token-1",
        "position_id": None,
        "deal_id": "deal-1",
        "side": "BUY",
        "order_type": "FAK",
        "purpose": "ENTRY",
        "requested_amount_usd": "3.8",
        "max_spend": "5",
        "max_tokens": "5",
        "max_price": "0.76",
    }
    result.update(overrides)
    return result


def stop_order(intent_id="stop-intent-1", **overrides):
    result = {
        "idempotency_key": intent_id,
        "durable_intent_reserved": True,
        "event_id": "stop-event",
        "condition_id": "stop-condition",
        "token_id": "stop-token",
        "position_id": "stop-position",
        "deal_id": "stop-deal",
        "side": "SELL",
        "order_type": "GTC",
        "purpose": "STOP_066",
        "requested_price": "0.55",
        "requested_size": "5.42857",
    }
    result.update(overrides)
    return result


def test_successful_create_order_records_started_and_success():
    temporary, base, _strategy, adapter = environment()
    try:
        result = asyncio.run(adapter.create_order(entry_order()))
        attempts = rows(base)
        assert result["polymarket_order_id"] == "remote-order-1"
        assert [row["result_status"] for row in attempts] == [
            "STARTED", "SUCCESS"
        ]
        assert attempts[0]["attempt_id"] == attempts[1]["attempt_id"]
        assert attempts[1]["remote_order_id"] == "remote-order-1"
        assert attempts[1]["transaction_hash"] == "tx-1"
    finally:
        temporary.cleanup()


def test_sdk_exception_preserves_original_type_message_and_behavior():
    exception = RequestRejectedError("example rejection")
    temporary, base, _strategy, adapter = environment(
        FakeTradingClient(post_result=exception)
    )
    try:
        result = asyncio.run(adapter.create_order(entry_order()))
        attempts = rows(base)
        final = attempts[-1]
        assert result["status"] == "unknown"
        assert "RequestRejectedError: example rejection" in result["failure_reason"]
        assert final["result_status"] == "UNKNOWN"
        assert final["exception_type"] == "RequestRejectedError"
        assert final["exception_message"] == "example rejection"
        assert final["http_status"] == 422
        assert final["response_json"] is None
    finally:
        temporary.cleanup()


def test_rejected_response_persists_raw_sanitized_response():
    response = {
        "ok": False,
        "code": "api_rejected",
        "message": "order rejected",
        "details": {"api_secret": "response-secret", "reason": "balance"},
    }
    temporary, base, _strategy, adapter = environment(
        FakeTradingClient(post_result=response)
    )
    try:
        result = asyncio.run(adapter.create_order(entry_order()))
        final = rows(base)[-1]
        assert result["status"] == "rejected"
        assert final["result_status"] == "REJECTED"
        assert final["exception_type"] is None
        assert "response-secret" not in final["response_json"]
        assert "[REDACTED]" in final["response_json"]
        assert json.loads(final["response_json"])["details"]["reason"] == "balance"
    finally:
        temporary.cleanup()


def test_stop_failure_survives_later_cancel_uncertain_state():
    response = {
        "ok": False,
        "code": "insufficient_balance",
        "message": "tokens are not yet sellable",
    }
    temporary, base, strategy, adapter = environment(
        FakeTradingClient(post_result=response)
    )
    try:
        intent = strategy.reserve_event_entry(
            event_id="stop-event", condition_id="stop-condition",
            token_id="stop-token", side="YES", simultaneous=False,
            reason_code="TEST",
        )
        intent_id = intent["entry_intent_id"]
        result = asyncio.run(
            adapter.create_order(stop_order(intent_id=intent_id))
        )
        assert result["status"] == "rejected"
        before = rows(base)
        strategy.update_intent(
            intent_id, state="CANCEL_UNCERTAIN", reason_code="LATER_REASON"
        )
        after = rows(base)
        assert before == after
        assert after[-1]["purpose"] == "STOP_066"
        assert after[-1]["error_code"] == "insufficient_balance"
        assert "tokens are not yet sellable" in after[-1]["response_json"]
    finally:
        temporary.cleanup()


def test_cancel_success_records_started_and_success():
    temporary, base, _strategy, adapter = environment()
    context = {
        "event_id": "event-1", "intent_id": "intent-1",
        "position_id": "position-1", "purpose": "EMERGENCY_060",
    }
    try:
        result = asyncio.run(
            adapter.cancel_order_with_context("remote-order-1", context)
        )
        attempts = rows(base)
        assert result["success"] is True
        assert [row["result_status"] for row in attempts] == [
            "STARTED", "SUCCESS"
        ]
        assert attempts[-1]["remote_order_id"] == "remote-order-1"
    finally:
        temporary.cleanup()


def test_cancel_exception_preserves_original_exception():
    exception = RequestRejectedError("cancel rejected by API")
    temporary, base, _strategy, adapter = environment(
        FakeTradingClient(cancel_result=exception)
    )
    try:
        result = asyncio.run(
            adapter.cancel_order_with_context(
                "remote-order-1",
                {"event_id": "event-1", "intent_id": "intent-1"},
            )
        )
        final = rows(base)[-1]
        assert result["status"] == "uncertain"
        assert final["result_status"] == "UNKNOWN"
        assert final["exception_type"] == "RequestRejectedError"
        assert final["exception_message"] == "cancel rejected by API"
    finally:
        temporary.cleanup()


def test_cancel_missing_remote_id_records_failed_precondition_without_sdk_call():
    client = FakeTradingClient()
    temporary, base, _strategy, adapter = environment(client)
    try:
        result = asyncio.run(
            adapter.cancel_order_with_context(
                None,
                {"event_id": "event-1", "intent_id": "intent-1"},
            )
        )
        attempts = rows(base)
        assert result["failure_reason"] == "REMOTE_ORDER_ID_MISSING"
        assert [row["result_status"] for row in attempts] == [
            "STARTED", "FAILED_PRECONDITION"
        ]
        assert client.cancel_calls == 0
    finally:
        temporary.cleanup()


def test_sanitizer_redacts_db_and_failure_journal(caplog):
    response = {
        "ok": False,
        "code": "rejected",
        "message": "password=journal-secret",
        "private_key": "response-private",
    }
    temporary, base, _strategy, adapter = environment(
        FakeTradingClient(post_result=response)
    )
    request = entry_order(
        api_secret="request-api-secret",
        private_key="request-private",
        authorization="request-auth",
        password="request-password",
    )
    try:
        with caplog.at_level(logging.ERROR):
            asyncio.run(adapter.create_order(request))
        serialized = json.dumps(rows(base), sort_keys=True)
        for secret in (
            "request-api-secret", "request-private", "request-auth",
            "request-password", "response-private", "journal-secret",
            "signed-secret-must-not-be-logged",
        ):
            assert secret not in serialized
            assert secret not in caplog.text
        assert "[REDACTED]" in serialized
        assert "[REDACTED]" in caplog.text
    finally:
        temporary.cleanup()


def test_correlation_queries_find_create_and_cancel_by_event_and_intent():
    temporary, base, _strategy, adapter = environment()
    context = {
        "event_id": "event-correlation",
        "condition_id": "condition-correlation",
        "token_id": "token-correlation",
        "intent_id": "intent-correlation",
        "position_id": "position-correlation",
        "purpose": "STOP_066",
    }
    try:
        asyncio.run(adapter.create_order(stop_order(
            intent_id="intent-correlation",
            event_id="event-correlation",
            condition_id="condition-correlation",
            token_id="token-correlation",
        )))
        asyncio.run(
            adapter.cancel_order_with_context("remote-order-1", context)
        )
        with base.connect() as conn:
            by_event = conn.execute(
                "SELECT operation FROM live_order_attempts "
                "WHERE event_id=? ORDER BY occurred_at,record_id",
                ("event-correlation",),
            ).fetchall()
            by_intent = conn.execute(
                "SELECT operation FROM live_order_attempts "
                "WHERE intent_id=? ORDER BY occurred_at,record_id",
                ("intent-correlation",),
            ).fetchall()
        assert [row["operation"] for row in by_event] == [
            "CREATE_ORDER", "CREATE_ORDER", "CANCEL_ORDER", "CANCEL_ORDER"
        ]
        assert [row["operation"] for row in by_intent] == [
            "CREATE_ORDER", "CREATE_ORDER", "CANCEL_ORDER", "CANCEL_ORDER"
        ]
    finally:
        temporary.cleanup()


def test_attempt_records_survive_multiple_intent_state_changes():
    temporary, base, strategy, adapter = environment(
        FakeTradingClient(post_result={
            "ok": False, "code": "stop_failed", "message": "original reason"
        })
    )
    try:
        intent = strategy.reserve_event_entry(
            event_id="history-event", condition_id="history-condition",
            token_id="history-token", side="YES", simultaneous=False,
            reason_code="TEST",
        )
        intent_id = intent["entry_intent_id"]
        asyncio.run(adapter.create_order(stop_order(
            intent_id=intent_id, event_id="history-event",
            condition_id="history-condition", token_id="history-token",
        )))
        original = rows(base)
        for state in ("RECONCILIATION_REQUIRED", "CANCEL_REQUESTED", "CANCEL_UNCERTAIN"):
            strategy.update_intent(intent_id, state=state, reason_code=state)
        assert rows(base) == original
        assert "original reason" in original[-1]["response_json"]
    finally:
        temporary.cleanup()


def test_audit_persistence_failure_logs_critical_and_preserves_adapter_result(caplog):
    class FailingRecorder:
        def start(self, _operation, _request):
            raise RuntimeError("password=audit-secret")

    client = FakeTradingClient()
    adapter = RealPolymarketTradingAdapter(
        armed_config(), secure_client=client, attempt_recorder=FailingRecorder()
    )
    with caplog.at_level(logging.CRITICAL):
        result = asyncio.run(adapter.create_order(entry_order()))
    assert result["polymarket_order_id"] == "remote-order-1"
    assert client.post_calls == 1
    assert "POLYMARKET_AUDIT_PERSISTENCE_FAILED" in caplog.text
    assert "audit-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text
