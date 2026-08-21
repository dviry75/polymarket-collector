from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import random
import statistics
import time
from typing import Any

from .adapters.base import TradingAdapter
from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso
from .dashboard_schema import mark_reconciled_provenance
from .strategy_repository import StrategyRepository, sanitize
from .reconciliation_stability import (
    authoritative_token_balance, classify_missing_position,
    ingest_maker_exit_fills,
)



POSITION_PROPAGATION_GRACE_SECONDS = 15.0
INTENT_SUBMISSION_GRACE_SECONDS = 15.0
AUTO_RECOVERABLE_POSITION_CORRECTION_MAX_SHARES = Decimal("0.01")
RECONCILIATION_BACKOFF_BASE_SECONDS = 5.0
RECONCILIATION_BACKOFF_CAP_SECONDS = 60.0
RECONCILIATION_TELEMETRY_CAPACITY = 1_024


class _BoundedDurationMetric:
    """Bounded in-memory durations; percentile sorting occurs on status reads."""

    def __init__(self, capacity: int = RECONCILIATION_TELEMETRY_CAPACITY):
        self.samples: deque[float] = deque(maxlen=capacity)
        self.current: float | None = None
        self.maximum: float | None = None

    def observe(self, value: float) -> None:
        observed = max(0.0, float(value))
        self.samples.append(observed)
        self.current = observed
        self.maximum = observed if self.maximum is None else max(
            self.maximum, observed
        )

    def snapshot(self) -> dict[str, float | int | None]:
        if not self.samples:
            return {
                "count": 0, "current": None, "p50": None, "p95": None,
                "p99": None, "max": self.maximum,
            }
        ordered = sorted(self.samples)

        def percentile(fraction: float) -> float:
            index = min(
                len(ordered) - 1,
                max(0, int(len(ordered) * fraction) - 1),
            )
            return round(ordered[index], 4)

        return {
            "count": len(ordered),
            "current": round(float(self.current or 0.0), 4),
            "p50": round(statistics.median(ordered), 4),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": round(float(self.maximum or 0.0), 4),
        }


def _within_position_propagation_grace(
    created_at: str | None,
) -> bool:
    if not created_at:
        return False

    try:
        created = datetime.fromisoformat(
            str(created_at).replace("Z", "+00:00")
        )

        if created.tzinfo is None:
            created = created.replace(
                tzinfo=timezone.utc
            )

        age = (
            datetime.now(timezone.utc) - created
        ).total_seconds()

        return (
            0
            <= age
            <= POSITION_PROPAGATION_GRACE_SECONDS
        )

    except (TypeError, ValueError):
        return False


def _within_intent_submission_grace(
    timestamp: str | None,
) -> bool:
    if not timestamp:
        return False
    try:
        observed = datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00")
        )
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return 0 <= age <= INTENT_SUBMISSION_GRACE_SECONDS
    except (TypeError, ValueError):
        return False


def _is_rate_limit_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "ratelimit" in name
        or "rate limit" in message
        or "too many requests" in message
        or "http 429" in message
        or "status 429" in message
    )


def _is_temporary_network_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
        token in name or token in message
        for token in ("timeout", "temporar", "connection", "network", "http 502", "http 503", "http 504")
    )


class ReconciliationWorker:
    """Reconciles both the legacy control tables and the durable strategy state.

    Remote account state is the financial truth. Any mismatch pauses entries; a
    correction must be followed by a clean reconciliation before readiness returns.
    """

    def __init__(
        self,
        repo: LiveRepository,
        adapter: TradingAdapter,
        strategy_repo: StrategyRepository | None = None,
    ):
        self.repo = repo
        self.adapter = adapter
        self.strategy_repo = strategy_repo
        self._run_lock = asyncio.Lock()
        self._consecutive_retries = 0
        self._rate_limit_retry_after = 0.0
        self._missing_position_suspects: dict[str, float] = {}
        self.reconciliation_started_at: str | None = None
        self.reconciliation_finished_at: str | None = None
        self._reconciliation_duration_ms = {
            "success": _BoundedDurationMetric(),
            "failure": _BoundedDurationMetric(),
        }

    def _schedule_backoff(self, actor: str, error: str) -> float:
        self._consecutive_retries += 1
        base = min(
            RECONCILIATION_BACKOFF_CAP_SECONDS,
            RECONCILIATION_BACKOFF_BASE_SECONDS
            * (2 ** (self._consecutive_retries - 1)),
        )
        delay = min(
            RECONCILIATION_BACKOFF_CAP_SECONDS,
            base * random.uniform(0.85, 1.15),
        )
        self._rate_limit_retry_after = time.monotonic() + delay
        self.repo.set_states({
            "reconciliation_retry_count": str(self._consecutive_retries),
            "reconciliation_backoff_seconds": f"{delay:.3f}",
            "last_reconciliation_error": error[:500],
        }, actor)
        return delay

    def _reset_backoff(self, actor: str) -> None:
        self._consecutive_retries = 0
        self._rate_limit_retry_after = 0.0
        self.repo.set_states({
            "reconciliation_retry_count": "0",
            "reconciliation_backoff_seconds": "0",
            "last_reconciliation_error": "",
            "last_reconciliation_success": now_iso(),
        }, actor)

    async def run_once(self, actor: str = "system") -> dict[str, Any]:
        async with self._run_lock:
            now = time.monotonic()
            if now < self._rate_limit_retry_after:
                return {
                    "run_id": None,
                    "status": "backoff",
                    "gaps": [],
                    "reason": "RECONCILIATION_BACKOFF",
                    "retry_after_seconds": round(
                        self._rate_limit_retry_after - now, 3
                    ),
                }
            started = time.perf_counter()
            self.reconciliation_started_at = now_iso()
            result: dict[str, Any] | None = None
            try:
                result = await self._run_once_serialized(actor)
                return result
            finally:
                duration_ms = max(0.0, (time.perf_counter() - started) * 1000)
                outcome = (
                    "success"
                    if result is not None and result.get("status") == "ok"
                    else "failure"
                )
                self._reconciliation_duration_ms[outcome].observe(duration_ms)
                self.reconciliation_finished_at = now_iso()

    def health(self) -> dict[str, Any]:
        return {
            "reconciliation_started_at": self.reconciliation_started_at,
            "reconciliation_finished_at": self.reconciliation_finished_at,
            "reconciliation_duration_ms": {
                outcome: metric.snapshot()
                for outcome, metric in self._reconciliation_duration_ms.items()
            },
            "sample_capacity_per_outcome": RECONCILIATION_TELEMETRY_CAPACITY,
        }

    async def _run_once_serialized(self, actor: str) -> dict[str, Any]:
        run_id = self.repo.start_reconciliation()
        gaps: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        auto_recoverable_gaps = 0
        try:
            identity = {"status": "MOCK"}
            if hasattr(self.adapter, "identity_preflight"):
                identity = await self.adapter.identity_preflight()  # type: ignore[attr-defined]
                if identity.get("status") != "VERIFIED":
                    gaps.append({"type": "identity_preflight_failed", "status": identity.get("status")})

            if identity.get("status") == "VERIFIED" and hasattr(self.adapter, "get_closed_only_mode"):
                account_mode = await self.adapter.get_closed_only_mode()  # type: ignore[attr-defined]
                if account_mode.get("status") != "FULL_TRADING":
                    gaps.append({
                        "type": "account_not_full_trading",
                        "status": account_mode.get("status"),
                    })
            balance = await self.adapter.get_balance()
            allowances = await self.adapter.get_allowances()
            remote_open = await self.adapter.get_open_orders()
            remote_trades = await self.adapter.get_trades()
            remote_positions = await self.adapter.get_positions()
            self.repo.store_account_snapshot({
                "sampled_at": now_iso(),
                "configured_profile_address": identity.get("wallet"),
                "resolved_proxy_wallet": identity.get("wallet"),
                "expected_funder_candidate": identity.get("signer"),
                "account_identity_status": identity.get("status"),
                "public_positions_count": len(remote_positions),
                "public_positions_value": canonical_decimal(sum(
                    (decimal_value(item.get("current_value")) or Decimal("0") for item in remote_positions),
                    Decimal("0"),
                )),
                "balance_usd": balance.get("balance_usd"),
                "allowance_usd": allowances.get("allowance_usd"),
                "status": "ok",
                "raw_payload": {
                    "wallet_type": identity.get("wallet_type"),
                    "open_orders_count": len(remote_open),
                    "trades_count": len(remote_trades),
                    "positions_count": len(remote_positions),
                    "allowance_contracts": len(allowances.get("allowances_raw") or {}),
                },
            })

            remote_by_id = {
                str(item.get("polymarket_order_id") or item.get("id")): item
                for item in remote_open
                if item.get("polymarket_order_id") or item.get("id")
            }
            local_orders = self.repo.non_final_orders()
            for order in local_orders:
                remote_id = order.get("polymarket_order_id")
                if remote_id and str(remote_id) not in remote_by_id and order.get("status") in {
                    "live", "submitted", "delayed", "reconciling"
                }:
                    gaps.append({
                        "type": "local_order_missing_remote",
                        "local_order_id": order["local_order_id"],
                        "polymarket_order_id": remote_id,
                    })
            local_remote_ids = {
                str(order.get("polymarket_order_id"))
                for order in local_orders if order.get("polymarket_order_id")
            }
            for remote_id in remote_by_id:
                legacy_known = remote_id in local_remote_ids
                strategy_known = bool(
                    self.strategy_repo and self.strategy_repo.intent_by_remote_order(remote_id)
                )
                if not legacy_known and not strategy_known:
                    gaps.append({
                        "type": "remote_order_missing_local",
                        "polymarket_order_id": remote_id,
                    })

            for trade in remote_trades:
                order_id = str(trade.get("polymarket_order_id") or "")
                legacy = next(
                    (
                        item for item in local_orders
                        if str(item.get("polymarket_order_id") or "") == order_id
                    ),
                    None,
                )
                if legacy and trade.get("price") is not None and trade.get("size") is not None:
                    self.repo.add_fill(int(legacy["local_order_id"]), trade)
                if self.strategy_repo and order_id:
                    intent = self.strategy_repo.intent_by_remote_order(order_id)
                    if intent and trade.get("price") is not None and trade.get("size") is not None:
                        self.strategy_repo.add_fill(
                            intent_id=str(intent["intent_id"]),
                            remote_trade_id=str(trade.get("polymarket_trade_id") or "") or None,
                            shares=decimal_value(trade.get("size")) or Decimal("0"),
                            price=decimal_value(trade.get("price")) or Decimal("0"),
                            fee=decimal_value(trade.get("fee")) or Decimal("0"),
                            fee_verification_status=str(trade.get("fee_verification_status") or "UNKNOWN"),
                            fee_source=trade.get("fee_source"),
                            status=str(trade.get("status") or "MATCHED").upper(),
                            transaction_hash=trade.get("transaction_hash"),
                            matched_at=trade.get("matched_at"),
                            raw=trade.get("raw_message") or {},
                        )

            if self.strategy_repo:
                repairs.extend(ingest_maker_exit_fills(
                    self.repo, self.strategy_repo, remote_trades, remote_by_id
                ))
                for repair in repairs:
                    repaired_position = self.strategy_repo.position_for_token(
                        str(repair.get("token_id") or "")
                    )
                    repaired_remaining = decimal_value(
                        (repaired_position or {}).get("remaining_shares_text")
                    )
                    authoritative = await authoritative_token_balance(
                        self.adapter, str(repair.get("token_id") or "")
                    )
                    repair["authoritative_balance"] = (
                        canonical_decimal(authoritative)
                        if authoritative is not None else "unknown"
                    )
                    if authoritative is None or repaired_remaining != authoritative:
                        gaps.append({
                            "type": "repaired_position_balance_mismatch",
                            "position_id": repair.get("position_id"),
                            "token_id": repair.get("token_id"),
                            "local_shares": (
                                canonical_decimal(repaired_remaining)
                                if repaired_remaining is not None else "unknown"
                            ),
                            "authoritative_balance": repair["authoritative_balance"],
                        })
                        auto_recoverable_gaps += 1

            if self.strategy_repo:
                for intent in self.strategy_repo.unresolved_intents():
                    remote_id = str(intent.get("remote_order_id") or "")
                    if not remote_id:
                        intent_state = str(intent.get("state") or "").upper()
                        if (
                            intent_state == "WAITING_SELLABLE"
                        ):
                            # This state is only created after a confirmed
                            # pre-submission balance failure. No remote order
                            # exists, so absence of remote_order_id is expected.
                            continue

                        if (
                            intent_state in {"RESERVED", "SUBMITTING"}
                            and self.strategy_repo.intent_submission_inflight(
                                str(intent["intent_id"]))
                            and _within_intent_submission_grace(
                                str(
                                    intent.get("updated_at")
                                    or intent.get("created_at")
                                    or ""
                                )
                            )
                        ):
                            # The durable reservation is committed before the
                            # network request. A concurrent reconciliation may
                            # observe that expected hand-off window. If it gets
                            # stuck, the same state becomes a real gap after the
                            # bounded grace period.
                            continue

                        gaps.append({
                            "type": "durable_intent_without_remote_id",
                            "intent_id": intent["intent_id"],
                            "state": intent["state"],
                        })
                        continue
                    remote = remote_by_id.get(remote_id)
                    if remote is None:
                        remote = await self.adapter.get_order(remote_id)
                    summary = self.strategy_repo.fill_summary(str(intent["intent_id"]))
                    filled = summary["shares"]
                    status = str((remote or {}).get("status") or "unknown").lower()
                    if status.startswith("order_status_"):
                        status = status.removeprefix("order_status_")
                    is_open = remote_id in remote_by_id or status in {
                        "live", "open", "delayed", "pending", "retrying", "partially_filled"
                    }
                    terminal = status in {
                        "filled", "matched", "cancelled", "canceled", "rejected",
                        "failed", "unmatched", "expired",
                    }
                    if intent.get("action") == "ENTRY" and filled > 0:
                        market = self.repo.latest_market(str(intent["condition_id"])) or {}
                        self.strategy_repo.open_position(
                            event_id=str(intent["event_id"]),
                            condition_id=str(intent["condition_id"]),
                            token_id=str(intent["token_id"]),
                            outcome=str(intent.get("side") or "UNKNOWN"),
                            shares=filled,
                            average_price=summary["average_price"],
                            cost_all_in=summary["notional"] + summary["fees"],
                            fees=summary["fees"],
                            sellable_shares=Decimal("0"),
                            min_sellable=(
                                decimal_value(market.get("min_order_size")) or Decimal("0")
                            ),
                            entry_intent_id=str(intent["intent_id"]),
                        )
                        continue
                    if intent.get("action") == "ENTRY" and terminal:
                        self.strategy_repo.mark_zero_fill(
                            str(intent["event_id"]),
                            f"REMOTE_{status.upper()}_ZERO_FILL",
                            intent_id=str(intent["intent_id"]),
                        )
                        continue
                    if intent.get("action") in {"EXIT", "TP"} and filled > 0:
                        position = self.strategy_repo.position_for_token(str(intent["token_id"]))
                        if position is None:
                            gaps.append({
                                "type": "exit_fill_without_local_position",
                                "intent_id": intent["intent_id"],
                            })
                            continue
                        prior_shares = decimal_value(intent.get("filled_shares_text")) or Decimal("0")
                        delta = max(Decimal("0"), filled - prior_shares)
                        if delta > 0:
                            requested = decimal_value(intent.get("requested_shares_text")) or filled
                            final_state = (
                                "PARTIAL" if is_open else
                                "FILLED" if filled >= requested else "PARTIAL_FINAL"
                            )
                            market = self.repo.latest_market(str(intent["condition_id"])) or {}
                            self.strategy_repo.apply_exit_fill(
                                position_id=str(position["position_id"]),
                                intent_id=str(intent["intent_id"]),
                                sold_shares=delta,
                                average_price=summary["average_price"],
                                fees=summary["fees"],
                                final_state=final_state,
                                min_sellable=(
                                    decimal_value(market.get("min_order_size"))
                                    or Decimal("0.000001")
                                ),
                                purpose=str(intent.get("purpose") or "RECONCILED_EXIT"),
                                book_hash="account-reconciliation",
                                cumulative_filled_shares=filled,
                                cumulative_notional=summary["notional"],
                                cumulative_fees=summary["fees"],
                            )
                        continue
                    if intent.get("action") in {"EXIT", "TP"} and terminal:
                        if status in {"cancelled", "canceled", "expired"}:
                            self.strategy_repo.finalize_cancel(
                                str(intent["intent_id"]), True, f"REMOTE_{status.upper()}"
                            )
                        else:
                            self.strategy_repo.update_intent(
                                str(intent["intent_id"]),
                                state="REJECTED" if status == "rejected" else "FAILED",
                                reason_code=f"REMOTE_{status.upper()}",
                                final_at=now_iso(),
                            )
                        continue
                    if is_open:
                        self.strategy_repo.update_intent(
                            str(intent["intent_id"]),
                            state="LIVE",
                            filled_shares_text=canonical_decimal(filled),
                        )
                    elif not terminal:
                        gaps.append({
                            "type": "remote_order_state_unknown",
                            "intent_id": intent["intent_id"],
                            "polymarket_order_id": remote_id,
                            "status": status,
                        })

                remote_tokens: set[str] = set()
                for remote in remote_positions:
                    shares = decimal_value(remote.get("size")) or Decimal("0")
                    if shares <= 0:
                        continue
                    token_id = str(remote.get("token_id") or "")
                    condition_id = str(remote.get("condition_id") or "")
                    market = self.repo.latest_market(condition_id) if condition_id else None
                    if not token_id or not market:
                        gaps.append({
                            "type": "remote_position_unknown_market",
                            "condition_id": condition_id,
                            "token_id": token_id,
                        })
                        continue
                    remote_tokens.add(token_id)
                    outcome = self.repo.outcome_for_asset(condition_id, token_id) or str(
                        remote.get("outcome") or "UNKNOWN"
                    ).upper()
                    existing_position = self.strategy_repo.position_for_token(token_id)
                    if existing_position is not None:
                        local_remaining = (
                            decimal_value(existing_position.get("remaining_shares_text"))
                            or Decimal("0")
                        )
                        confirmed_exit_value = (
                            decimal_value(existing_position.get("exit_value_text"))
                            or Decimal("0")
                        )
                        if shares > local_remaining and confirmed_exit_value > 0:
                            if _within_position_propagation_grace(
                                existing_position.get("updated_at")
                            ):
                                # A confirmed SELL is execution truth. The public
                                # positions endpoint may briefly return its old
                                # snapshot, but must never resurrect those shares.
                                continue
                            gaps.append({
                                "type": "remote_position_after_confirmed_exit",
                                "position_id": existing_position["position_id"],
                                "token_id": token_id,
                                "local_shares": canonical_decimal(local_remaining),
                                "remote_shares": canonical_decimal(shares),
                            })
                            continue
                    position, changed = self.strategy_repo.reconcile_remote_position(
                        event_id=str(market.get("event_id") or condition_id),
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        remote_shares=shares,
                        average_price=decimal_value(remote.get("average_price")) or Decimal("0"),
                    )
                    if changed:
                        correction_is_bounded_propagation = bool(
                            existing_position is not None
                            and confirmed_exit_value <= 0
                            and _within_position_propagation_grace(
                                existing_position.get("created_at")
                            )
                            and abs(shares - local_remaining)
                            <= AUTO_RECOVERABLE_POSITION_CORRECTION_MAX_SHARES
                        )
                        gaps.append({
                            "type": "remote_position_corrected_local",
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "remote_shares": canonical_decimal(shares),
                        })
                        if correction_is_bounded_propagation:
                            auto_recoverable_gaps += 1
                    if bool(remote.get("redeemable")):
                        current_value = decimal_value(remote.get("current_value")) or Decimal("0")
                        self.strategy_repo.mark_position_resolved(
                            str(position["position_id"]),
                            winner=current_value > 0,
                            redeem_pending=current_value > 0,
                        )
                for local in self.strategy_repo.active_positions():
                    remaining = (
                        decimal_value(
                            local.get("remaining_shares_text")
                        )
                        or Decimal("0")
                    )

                    sellable = (
                        decimal_value(
                            local.get("sellable_shares_text")
                        )
                        or Decimal("0")
                    )

                    if (
                        remaining > 0
                        and str(local.get("token_id"))
                        not in remote_tokens
                    ):
                        if (
                            sellable <= 0
                            and _within_position_propagation_grace(
                                local.get("created_at")
                            )
                        ):
                            # The trade fill is already authoritative locally,
                            # but Polymarket's position/balance views can lag
                            # briefly. Exits remain blocked because sellable=0.
                            continue

                        cross_check = await classify_missing_position(
                            self.adapter, local, self._missing_position_suspects
                        )
                        if cross_check["status"] in {"confirmed_active", "suspect"}:
                            continue
                        authoritative = cross_check.get("balance")
                        gaps.append({
                            "type": "local_position_missing_remote",
                            "position_id": local["position_id"],
                            "token_id": local["token_id"],
                            "local_shares": canonical_decimal(remaining),
                            "authoritative_balance": (
                                canonical_decimal(authoritative)
                                if authoritative is not None else "unknown"
                            ),
                            "cross_check_status": cross_check["status"],
                        })

            status = "ok" if not gaps else "gaps"
            if gaps:
                retry_after_seconds = self._schedule_backoff(
                    actor, f"persistent gaps: {len(gaps)}"
                )
            else:
                retry_after_seconds = 0.0
                self._reset_backoff(actor)
            completed_at = now_iso()
            self.repo.finish_reconciliation(run_id, status, sanitize(gaps))
            if not gaps:
                mark_reconciled_provenance(self.repo)
            if self.strategy_repo:
                repairable_gap_set = bool(
                    gaps and auto_recoverable_gaps == len(gaps)
                )
                self.strategy_repo.set_reconciliation_state(
                    ready=not gaps,
                    reason=(
                        "RECONCILIATION_GAP"
                        if repairable_gap_set
                        else "RECONCILIATION_CONTRADICTION"
                        if gaps
                        else ""
                    ),
                    actor=actor,
                    auto_recoverable=(
                        repairable_gap_set
                    ),
                    run_id=run_id,
                    finished_at=completed_at,
                )
                if gaps:
                    self.strategy_repo.alert(
                        alert_type="RECONCILIATION", severity="CRITICAL",
                        reason_code="RECONCILIATION_MISMATCH",
                        message=f"Reconciliation found {len(gaps)} mismatch(es); entries paused",
                        entity_type="account", entity_id="remote_truth",
                    )
                self.strategy_repo.timeline(
                    severity="INFO" if not gaps else "CRITICAL",
                    category="RECONCILIATION", component="reconciliation",
                    source=actor, requested_action="ACCOUNT_RECONCILIATION",
                    reason_code="CLEAN" if not gaps else "RECONCILIATION_MISMATCH",
                    result_status="MATCHED" if not gaps else "GAPS",
                    parameters_json={
                        "run_id": run_id,
                        "gaps": len(gaps),
                        "repairs": len(repairs),
                        "retry_count": self._consecutive_retries,
                        "backoff_seconds": round(retry_after_seconds, 3),
                        "open_orders": len(remote_open),
                        "trades": len(remote_trades),
                        "positions": len(remote_positions),
                    },
                )
            self.repo.audit(
                actor, "live_reconciliation", status,
                details={"run_id": run_id, "gaps": len(gaps)},
            )
            return {
                "run_id": run_id, "status": status, "gaps": sanitize(gaps),
                "repairs": sanitize(repairs),
                "retry_count": self._consecutive_retries,
                "retry_after_seconds": round(retry_after_seconds, 3),
            }
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {exc}"[:500]
            rate_limited = _is_rate_limit_error(exc)
            previously_degraded = self._consecutive_retries > 0
            retry_after_seconds = self._schedule_backoff(actor, safe_error)
            completed_at = now_iso()
            self.repo.finish_reconciliation(run_id, "failed", sanitize(gaps), safe_error)
            unauthorized = any(
                token in str(exc).lower()
                for token in ("unauthorized", "invalid api key", "http 401", "status 401")
            )
            temporary_error = (
                rate_limited or _is_temporary_network_error(exc)
                or (unauthorized and previously_degraded)
            )
            # Kill switch is operator-owned. Machine failures acquire only an
            # explicitly-owned entry pause.
            self.repo.set_state("canary_armed", "false", actor)
            if self.strategy_repo:
                self.strategy_repo.set_reconciliation_state(
                    ready=False,
                    reason=(
                        "RECONCILIATION_RATE_LIMITED"
                        if rate_limited
                        else (
                            "RECONCILIATION_TEMPORARY_ERROR"
                            if temporary_error
                            else "RECONCILIATION_FAILED"
                        )
                    ),
                    actor=actor,
                    run_id=run_id,
                    finished_at=completed_at,
                )
                self.strategy_repo.alert(
                    alert_type="RECONCILIATION", severity="CRITICAL",
                    reason_code="RECONCILIATION_FAILED", message=safe_error,
                    entity_type="account", entity_id="remote_truth",
                )
            self.repo.audit(
                actor, "live_reconciliation", "failed", safe_error, {"run_id": run_id}
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "gaps": sanitize(gaps),
                "error": safe_error,
                "rate_limited": rate_limited,
                "retry_after_seconds": round(retry_after_seconds, 3),
            }
