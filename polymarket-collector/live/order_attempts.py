from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid
from typing import Any

from .repository import LiveRepository
from .strategy_repository import sanitize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(sanitize(value), sort_keys=True, separators=(",", ":"), default=str)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:2000]


def _http_status(value: Any) -> int | None:
    for candidate in (
        getattr(value, "status_code", None),
        getattr(value, "http_status", None),
        getattr(getattr(value, "response", None), "status_code", None),
    ):
        try:
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class StartedAttempt:
    attempt_id: str
    created_at: str
    operation: str
    request: dict[str, Any]
    intent_state_before: str | None


class OrderAttemptRecorder:
    """Append-only forensic records for real trading API interactions."""

    def __init__(self, repo: LiveRepository):
        self.repo = repo

    def _intent_state(self, intent_id: Any) -> str | None:
        if not intent_id:
            return None
        with self.repo.connect() as conn:
            row = conn.execute(
                "SELECT state FROM live_strategy_intents WHERE intent_id=?",
                (str(intent_id),),
            ).fetchone()
        return str(row["state"]) if row is not None else None

    @staticmethod
    def _context(request: dict[str, Any]) -> dict[str, Any]:
        return sanitize(dict(request))

    def start(
        self, operation: str, request: dict[str, Any]
    ) -> StartedAttempt:
        safe_request = self._context(request)
        created_at = _now()
        started = StartedAttempt(
            attempt_id=str(uuid.uuid4()),
            created_at=created_at,
            operation=str(operation),
            request=safe_request,
            intent_state_before=self._intent_state(
                safe_request.get("intent_id") or safe_request.get("idempotency_key")
            ),
        )
        self._insert(
            started,
            phase="STARTED",
            occurred_at=created_at,
            completed_at=None,
            result_status="STARTED",
            success=None,
            remote_order_id=safe_request.get("remote_order_id"),
            request_json=_json(safe_request),
        )
        return started

    def result(
        self,
        started: StartedAttempt,
        *,
        result_status: str,
        success: bool | None,
        normalized: dict[str, Any] | None = None,
        response: Any = None,
        exception: BaseException | None = None,
        error_code: Any = None,
        remote_order_id: Any = None,
        transaction_hash: Any = None,
    ) -> None:
        completed_at = _now()
        safe_normalized = sanitize(normalized) if normalized is not None else None
        safe_response = sanitize(response) if response is not None else None
        error_payload: dict[str, Any] | None = None
        exception_type = None
        exception_message = None
        http_status = None
        if exception is not None:
            exception_type = type(exception).__name__
            exception_message = _text(sanitize(str(exception)))
            http_status = _http_status(exception)
            error_payload = sanitize({
                "args": list(getattr(exception, "args", ()) or ()),
                "code": getattr(exception, "code", None),
                "error_code": getattr(exception, "error_code", None),
                "status_code": getattr(exception, "status_code", None),
                "http_status": getattr(exception, "http_status", None),
                "response": getattr(exception, "response", None),
                "body": getattr(exception, "body", None),
                "payload": getattr(exception, "payload", None),
            })
            error_payload = {
                key: value for key, value in error_payload.items() if value is not None
            }
        effective_remote = (
            remote_order_id
            if remote_order_id is not None
            else (safe_normalized or {}).get("polymarket_order_id")
            or started.request.get("remote_order_id")
        )
        effective_tx = (
            transaction_hash
            if transaction_hash is not None
            else (safe_normalized or {}).get("transaction_hash")
        )
        self._insert(
            started,
            phase="RESULT",
            occurred_at=completed_at,
            completed_at=completed_at,
            result_status=str(result_status),
            success=success,
            remote_order_id=effective_remote,
            transaction_hash=effective_tx,
            exception_type=exception_type,
            exception_message=exception_message,
            error_code=_text(
                error_code
                if error_code is not None
                else (safe_normalized or {}).get("failure_reason")
                or (error_payload or {}).get("error_code")
                or (error_payload or {}).get("code")
            ),
            http_status=http_status,
            request_json=_json(started.request),
            normalized_json=_json(safe_normalized) if safe_normalized is not None else None,
            response_json=_json(safe_response) if safe_response is not None else None,
            error_json=_json(error_payload) if error_payload else None,
        )

    def _insert(self, started: StartedAttempt, **values: Any) -> None:
        request = started.request
        intent_id = request.get("intent_id") or request.get("idempotency_key")
        row = {
            "record_id": str(uuid.uuid4()),
            "attempt_id": started.attempt_id,
            "phase": values.get("phase"),
            "occurred_at": values.get("occurred_at"),
            "created_at": started.created_at,
            "completed_at": values.get("completed_at"),
            "event_id": request.get("event_id"),
            "condition_id": request.get("condition_id"),
            "token_id": request.get("token_id"),
            "intent_id": intent_id,
            "position_id": request.get("position_id"),
            "deal_id": request.get("deal_id"),
            "operation": started.operation,
            "purpose": request.get("purpose"),
            "side": request.get("side"),
            "order_type": request.get("order_type"),
            "requested_price_text": request.get("requested_price") or request.get("min_price"),
            "requested_size_text": request.get("requested_size") or request.get("max_tokens"),
            "requested_amount_text": request.get("requested_amount_usd"),
            "max_price_text": request.get("max_price"),
            "max_spend_text": request.get("max_spend"),
            "intent_state_before": started.intent_state_before,
            "intent_state_after": self._intent_state(intent_id),
            "result_status": values.get("result_status"),
            "success": None if values.get("success") is None else int(bool(values.get("success"))),
            "remote_order_id": _text(values.get("remote_order_id")),
            "transaction_hash": _text(values.get("transaction_hash")),
            "exception_type": _text(values.get("exception_type")),
            "exception_message": _text(values.get("exception_message")),
            "error_code": _text(values.get("error_code")),
            "http_status": values.get("http_status"),
            "request_json": values.get("request_json") or "{}",
            "normalized_json": values.get("normalized_json"),
            "response_json": values.get("response_json"),
            "error_json": values.get("error_json"),
        }
        columns = tuple(row)
        placeholders = ",".join("?" for _ in columns)
        with self.repo.connect() as conn:
            conn.execute(
                f"INSERT INTO live_order_attempts({','.join(columns)}) VALUES({placeholders})",
                tuple(row[column] for column in columns),
            )
            conn.commit()
