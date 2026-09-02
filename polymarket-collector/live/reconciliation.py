from __future__ import annotations

import asyncio
from collections import deque
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import random
import statistics
import time
from typing import Any, Callable

from .adapters.base import TradingAdapter
from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso
from .dashboard_schema import mark_reconciled_provenance
from .strategy_repository import (
    FINAL_INTENT_STATES, StrategyRepository, sanitize,
)
from .reconciliation_stability import (
    authoritative_token_balance, classify_missing_position,
    ingest_maker_exit_fills,
)
from .fee_accounting import resolve_trade_fee
from .unknown_exit_recovery import resolve_unknown_open_exit_zero_effect
from .reconciliation_coordinator import GapBackoffTracker
from .trade_window import (
    STATE_WATERMARK_SOURCE, WATERMARK_STATE_DEFAULTS, TradeWindowPolicy,
    bootstrap_watermark, dedupe_by_trade_id, fetch_trade_window,
    next_watermark_state, plan_trade_window, read_watermark_state,
    telemetry as trade_window_telemetry,
)



logger = logging.getLogger(__name__)

POSITION_PROPAGATION_GRACE_SECONDS = 15.0


class TransientAccountModeError(RuntimeError):
    """The closed-only-mode probe failed; the account state is unknown.

    Raised so the reconciliation error path classifies it as a temporary
    error (backoff + retry) instead of an authoritative contradiction.
    """

INTENT_SUBMISSION_GRACE_SECONDS = 15.0
AUTO_RECOVERABLE_POSITION_CORRECTION_MAX_SHARES = Decimal("0.01")
# Order-status can report a terminal MATCHED/FILLED outcome before the
# trades/positions feeds have propagated the corresponding fill. Treating
# that as an immediate zero-fill is a false negative, not a safety measure:
# reconciliation.py:_run_once_serialized reuses INTENT_SUBMISSION_GRACE_SECONDS
# to defer the terminal decision instead of inventing a new timeout.
MATCHED_AWAITING_FILL_STATUSES = frozenset({"matched", "filled"})
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
    # Matched explicitly rather than by name substring so the classification
    # cannot silently regress if the class is ever renamed.
    if isinstance(exc, TransientAccountModeError):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
        token in name or token in message
        for token in (
            "timeout", "temporar", "connection", "network",
            "http 502", "http 503", "http 504",
            "unexpectedresponse", "did not match expected shape",
        )
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
        trade_window: TradeWindowPolicy | None = None,
        fill_drift_lookback_hours: float = 24.0,
    ):
        self.repo = repo
        self.adapter = adapter
        self.strategy_repo = strategy_repo
        self.trade_window = trade_window or TradeWindowPolicy()
        self.fill_drift_lookback_hours = float(fill_drift_lookback_hours)
        self._last_trade_fetch: dict[str, Any] = {}
        self._run_lock = asyncio.Lock()
        self._consecutive_retries = 0
        self._rate_limit_retry_after = 0.0
        self._gap_backoff = GapBackoffTracker()
        self._missing_position_suspects: dict[str, float] = {}
        self._resolved_zero_observations: dict[str, int] = {}
        self._exit_repair_observations: dict[
            str, tuple[tuple[str, ...], int, float]
        ] = {}
        self._unknown_zero_effect_observations: dict[
            str, tuple[tuple[str, int, int], int]
        ] = {}
        self.reconciliation_started_at: str | None = None
        self.reconciliation_finished_at: str | None = None
        self._reconciliation_duration_ms = {
            "success": _BoundedDurationMetric(),
            "failure": _BoundedDurationMetric(),
        }

    async def _read_account_trades(
        self, actor: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any], Callable[[], None]]:
        """Read only the account trades this run still needs.

        Returns the trades, the per-run fetch telemetry, and a callback that
        advances the durable watermark. The callback must be invoked only after
        every fetched trade has been persisted: an un-called watermark leaves
        the same range to be re-read next run, which is the safe direction.
        """
        policy = self.trade_window
        if not policy.enabled:
            # Explicit rollback path: full-history reads, no watermark writes,
            # identical to the pre-incremental behaviour.
            trades = list(await self.adapter.get_trades())
            return (
                trades,
                {"trade_fetch_mode": "full_history", "trade_fetch_remote_count": len(trades)},
                lambda: None,
            )

        states = self.repo.get_states(dict(WATERMARK_STATE_DEFAULTS))
        watermark, slice_seconds = read_watermark_state(states)
        source = str(states.get(STATE_WATERMARK_SOURCE) or "")
        if watermark is None:
            latest = (
                self.strategy_repo.latest_persisted_trade()
                if self.strategy_repo else None
            ) or {}
            watermark, source = bootstrap_watermark(latest.get("matched_at"), policy)

        plan = plan_trade_window(
            watermark_at=watermark, slice_seconds=slice_seconds, policy=policy
        )
        effective_policy = policy.for_bootstrap() if plan.bootstrap else policy
        result = await fetch_trade_window(self.adapter, plan, effective_policy)
        trades = dedupe_by_trade_id(result.trades)

        known: set[str] = set()
        if self.strategy_repo and trades:
            known = self.strategy_repo.known_remote_trade_ids(
                str(trade.get("polymarket_trade_id") or "") for trade in trades
            )
        duplicate_count = sum(
            1 for trade in trades
            if str(trade.get("polymarket_trade_id") or "") in known
        )
        metrics = trade_window_telemetry(
            plan, result, effective_policy, duplicate_count=duplicate_count
        )
        metrics["trade_fetch_mode"] = (
            "bootstrap" if plan.bootstrap else "incremental"
        )
        metrics["trade_fetch_watermark_source"] = source
        self._last_trade_fetch = dict(metrics)

        if result.truncated or metrics["trade_fetch_new_count"]:
            # Silent in the steady state (no new trades, no limits hit); the
            # reconciliation cadence is seconds, so unconditional logging here
            # would be pure noise.
            logger.info(
                "TRADE_FETCH_WINDOW actor=%s mode=%s after=%s before=%s pages=%s "
                "remote=%s new=%s duplicate=%s duration_ms=%s limit=%s",
                actor, metrics["trade_fetch_mode"], metrics["trade_fetch_after"],
                metrics["trade_fetch_before"], metrics["trade_fetch_pages"],
                metrics["trade_fetch_remote_count"], metrics["trade_fetch_new_count"],
                metrics["trade_fetch_duplicate_count"],
                metrics["trade_fetch_duration_ms"],
                metrics["trade_fetch_backlog_or_limit_hit"] or "none",
            )

        def commit() -> None:
            advanced = next_watermark_state(plan, result, effective_policy)
            if source:
                advanced.setdefault(STATE_WATERMARK_SOURCE, source)
            self.repo.set_states(
                {key: str(value) for key, value in advanced.items()}, actor
            )

        return trades, metrics, commit

    def _drifted_exit_intents(self) -> dict[str, dict[str, Any]]:
        """Exits with a maker-child fill that never reached the position.

        Replaces the incidental retry that full-history trade reads used to
        provide. Bounded by a recency cutoff so it cannot become the same
        unbounded scan in SQL form.
        """
        if not self.strategy_repo:
            return {}
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=max(0.0, self.fill_drift_lookback_hours))
        ).isoformat()
        return self.strategy_repo.exit_intents_with_maker_fill_drift(since=since)

    def _schedule_backoff(self, actor: str, error: str) -> float:
        self._consecutive_retries += 1
        # Saturate before exponentiation. Evaluating an unbounded
        # 2 ** retry_count eventually creates an integer that cannot be
        # converted to float, masking the original failure and orphaning the
        # active reconciliation row.
        maximum_exponent = max(
            0,
            math.ceil(
                math.log2(
                    RECONCILIATION_BACKOFF_CAP_SECONDS
                    / RECONCILIATION_BACKOFF_BASE_SECONDS
                )
            ),
        )
        exponent = min(self._consecutive_retries - 1, maximum_exponent)
        base = min(
            RECONCILIATION_BACKOFF_CAP_SECONDS,
            RECONCILIATION_BACKOFF_BASE_SECONDS * (2 ** exponent),
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
        self._gap_backoff.reset()
        self.repo.set_states({
            "reconciliation_retry_count": "0",
            "reconciliation_backoff_seconds": "0",
            "last_reconciliation_error": "",
            "last_reconciliation_success": now_iso(),
        }, actor)

    def reset_retry_backoff(self, actor: str = "financial_event") -> None:
        """Allow new financial evidence to trigger immediate verification."""
        self._reset_backoff(actor)

    def _schedule_gap_backoff(
        self, actor: str, gaps: list[dict[str, Any]]
    ) -> float:
        delay = self._gap_backoff.observe(gaps)
        self._consecutive_retries = self._gap_backoff.repeat_count
        self._rate_limit_retry_after = time.monotonic() + delay
        snapshot = self._gap_backoff.snapshot()
        self.repo.set_states({
            "reconciliation_retry_count": str(self._consecutive_retries),
            "reconciliation_backoff_seconds": f"{delay:.3f}",
            "reconciliation_gap_fingerprint": snapshot["fingerprint"],
            "reconciliation_gap_evidence_fingerprint": (
                snapshot["evidence_fingerprint"]
            ),
            "reconciliation_gap_repeat_count": str(snapshot["repeat_count"]),
            "reconciliation_gap_last_changed_at": snapshot["last_changed_at"],
            "last_reconciliation_error": f"persistent gaps: {len(gaps)}",
        }, actor)
        return delay

    def _observe_exit_repair_evidence(
        self, token_id: str, evidence_key: tuple[str, ...]
    ) -> tuple[int, float]:
        now = time.monotonic()
        prior = self._exit_repair_observations.get(token_id)
        if prior is None or prior[0] != evidence_key:
            observations, first_seen = 1, now
        else:
            observations, first_seen = prior[1] + 1, prior[2]
        self._exit_repair_observations[token_id] = (
            evidence_key, observations, first_seen
        )
        return observations, max(0.0, now - first_seen)

    def _try_authoritative_exit_repair(
        self,
        *,
        local: dict[str, Any],
        authoritative_balance: Decimal,
        market: dict[str, Any],
        actor: str,
        linked_intent_id: str | None = None,
    ) -> dict[str, Any]:
        if self.strategy_repo is None:
            return {"status": "not_applicable"}
        position_id = str(local.get("position_id") or "")
        token_id = str(local.get("token_id") or "")
        intent_id = str(
            linked_intent_id or local.get("active_exit_intent_id") or ""
        )
        intent = self.strategy_repo.intent(intent_id) if intent_id else None
        attempt = (
            self.strategy_repo.matched_exit_attempt_evidence(intent_id)
            if intent is not None else None
        )
        normalized = (attempt or {}).get("normalized") or {}
        remote_order_id = str((intent or {}).get("remote_order_id") or "")
        evidence_order_id = str(
            normalized.get("polymarket_order_id")
            or (attempt or {}).get("remote_order_id")
            or ""
        )
        making = decimal_value(normalized.get("making_amount"))
        taking = decimal_value(normalized.get("taking_amount"))
        acquired = decimal_value(local.get("acquired_shares_text"))
        reason_code = str((intent or {}).get("reason_code") or "").upper()
        action = str((intent or {}).get("action") or "").upper()
        remote_status = str(normalized.get("status") or "").lower()
        proof_valid = bool(
            intent is not None
            and action in {"EXIT", "TP"}
            and reason_code in {
                "REMOTE_MATCHED",
                "REMOTE_MATCHED_FILL_PROPAGATION_PENDING",
            }
            and remote_order_id
            and evidence_order_id == remote_order_id
            and remote_status in MATCHED_AWAITING_FILL_STATUSES
            and making is not None and making > 0
            and taking is not None and taking > 0
            and acquired is not None and acquired >= making
            and acquired - making == authoritative_balance
        )
        evidence_key = (
            remote_order_id,
            canonical_decimal(authoritative_balance),
            canonical_decimal(making) if making is not None else "",
            canonical_decimal(taking) if taking is not None else "",
            str((attempt or {}).get("record_id") or ""),
            "valid" if proof_valid else "unsafe",
        )
        observations, elapsed = self._observe_exit_repair_evidence(
            token_id, evidence_key
        )
        evidence = {
            "source": "conditional_token_balance+matched_order_attempt",
            "authoritative_balance": canonical_decimal(authoritative_balance),
            "remote_order_id": remote_order_id,
            "attempt_record_id": (attempt or {}).get("record_id"),
            "making_amount": (
                canonical_decimal(making) if making is not None else None
            ),
            "taking_amount": (
                canonical_decimal(taking) if taking is not None else None
            ),
            "remote_status": remote_status,
            "observations": observations,
            "observation_elapsed_seconds": round(elapsed, 3),
            "proof_valid": proof_valid,
        }
        if observations < 2 or elapsed < POSITION_PROPAGATION_GRACE_SECONDS:
            return {"status": "confirmation_pending", "evidence": evidence}
        if not proof_valid:
            if str(local.get("state") or "").upper() != "QUARANTINED":
                quarantine = self.strategy_repo.quarantine_position(
                    position_id,
                    reason_code="AUTHORITATIVE_POSITION_MISMATCH_UNREPAIRABLE",
                    evidence=evidence,
                    actor=actor,
                    operator_action_required=True,
                )
                self.strategy_repo.reclassify_pause_as_scoped(
                    actor=actor,
                    incident_scope="POSITION",
                    source_position_id=position_id,
                    operator_action_required=True,
                    reason="AUTHORITATIVE_POSITION_MISMATCH_UNREPAIRABLE",
                )
            else:
                quarantine = {"position_id": position_id, "status": "OPEN"}
            return {
                "status": "quarantined",
                "evidence": evidence,
                "quarantine": quarantine,
            }

        assert intent is not None and attempt is not None
        assert making is not None and taking is not None
        transaction_hashes = normalized.get("transaction_hashes") or []
        transaction_hash = (
            str(transaction_hashes[0]) if transaction_hashes else None
        )
        average_price = taking / making
        remote_fill_id = (
            f"authoritative:{remote_order_id}:{transaction_hash or 'matched'}"
        )
        before = {
            key: local.get(key)
            for key in (
                "state", "acquired_shares_text", "remaining_shares_text",
                "sellable_shares_text", "dust_shares_text", "exit_value_text",
                "exit_fees_text", "realized_pnl_text",
            )
        }
        self.strategy_repo.add_fill(
            intent_id=intent_id,
            remote_trade_id=remote_fill_id,
            shares=making,
            price=average_price,
            fee=Decimal("0"),
            fee_verification_status="UNKNOWN",
            fee_source="authoritative_matched_order_attempt",
            status="MATCHED",
            transaction_hash=transaction_hash,
            matched_at=str(attempt.get("occurred_at") or now_iso()),
            raw={
                "source": "authoritative_exit_repair",
                "order_attempt_record_id": attempt.get("record_id"),
                "remote_order_id": remote_order_id,
                "normalized": normalized,
                "authoritative_balance": canonical_decimal(
                    authoritative_balance
                ),
            },
        )
        summary = self.strategy_repo.fill_summary(intent_id)
        try:
            updated = self.strategy_repo.apply_authoritative_exit_repair(
                position_id=position_id,
                intent_id=intent_id,
                matched_shares=making,
                matched_notional=taking,
                authoritative_balance=authoritative_balance,
                verified_fill_shares=summary["shares"],
                verified_fill_notional=summary["notional"],
                verified_fill_fees=summary["fees"],
                min_sellable=(
                    decimal_value(market.get("min_order_size"))
                    or Decimal("0.000001")
                ),
                actor=actor,
                evidence=evidence,
            )
        except (KeyError, ValueError) as exc:
            evidence = {
                **evidence,
                "state_rebuild_error": f"{type(exc).__name__}: {exc}",
                "verified_fill_shares": canonical_decimal(summary["shares"]),
                "verified_fill_notional": canonical_decimal(summary["notional"]),
            }
            quarantine = self.strategy_repo.quarantine_position(
                position_id,
                reason_code="AUTO_REPAIR_EVIDENCE_CONTRADICTION",
                evidence=evidence,
                actor=actor,
                operator_action_required=True,
            )
            self.strategy_repo.reclassify_pause_as_scoped(
                actor=actor,
                incident_scope="POSITION",
                source_position_id=position_id,
                operator_action_required=True,
                reason="AUTO_REPAIR_EVIDENCE_CONTRADICTION",
            )
            return {
                "status": "quarantined",
                "evidence": evidence,
                "quarantine": quarantine,
            }
        repaired_remaining = (
            decimal_value(updated.get("remaining_shares_text"))
            or Decimal("0")
        )
        if repaired_remaining != authoritative_balance:
            quarantine = self.strategy_repo.quarantine_position(
                position_id,
                reason_code="AUTO_REPAIR_POSTCONDITION_MISMATCH",
                evidence={
                    **evidence,
                    "repaired_remaining": canonical_decimal(repaired_remaining),
                },
                actor=actor,
                operator_action_required=True,
            )
            self.strategy_repo.reclassify_pause_as_scoped(
                actor=actor,
                incident_scope="POSITION",
                source_position_id=position_id,
                operator_action_required=True,
                reason="AUTO_REPAIR_POSTCONDITION_MISMATCH",
            )
            return {
                "status": "quarantined",
                "evidence": evidence,
                "quarantine": quarantine,
            }
        self.strategy_repo.resolve_position_quarantine(
            position_id, actor=actor, reason="AUTHORITATIVE_EXIT_REPAIRED"
        )
        after = {
            key: updated.get(key)
            for key in (
                "state", "acquired_shares_text", "remaining_shares_text",
                "sellable_shares_text", "dust_shares_text", "exit_value_text",
                "exit_fees_text", "realized_pnl_text",
            )
        }
        audit_id = self.strategy_repo.record_authoritative_auto_repair(
            actor=actor,
            position_id=position_id,
            reason="MATCHED_EXIT_BALANCE_CONFIRMED",
            before=before,
            after=after,
            evidence=evidence,
        )
        self.strategy_repo.reclassify_pause_as_scoped(
            actor=actor,
            incident_scope="POSITION",
            source_position_id=position_id,
            operator_action_required=False,
            reason="AUTHORITATIVE_EXIT_REPAIRED",
        )
        self.strategy_repo.alert(
            alert_type="AUTO_REPAIR",
            severity="INFO",
            reason_code="AUTHORITATIVE_EXIT_AUTO_REPAIR",
            message=(
                "[AUTO-REPAIR] matched EXIT aligned from order-attempt and "
                "conditional-token balance"
            ),
            entity_type="position",
            entity_id=position_id,
        )
        self._exit_repair_observations.pop(token_id, None)
        return {
            "status": "repaired",
            "type": "authoritative_matched_exit_repair",
            "position_id": position_id,
            "intent_id": intent_id,
            "token_id": token_id,
            "audit_id": audit_id,
            "before": before,
            "after": after,
            "evidence": evidence,
        }

    async def _reconcile_resolved_local_positions(
        self,
        *,
        remote_positions: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        actor: str,
    ) -> set[str]:
        """Resolve/repair local rows and return tokens handled by scoped logic."""
        handled_tokens: set[str] = set()
        if self.strategy_repo is None:
            return handled_tokens
        remote_by_token = {
            str(item.get("token_id") or ""): item
            for item in remote_positions
            if (decimal_value(item.get("size")) or Decimal("0")) > 0
        }
        for local in self.strategy_repo.reconciliation_positions():
            market = self.repo.latest_market(str(local.get("condition_id") or ""))
            if not market or not bool(market.get("market_resolved")):
                continue
            winner_token = str(market.get("winning_asset_id") or "")
            token_id = str(local.get("token_id") or "")
            if not winner_token or not token_id:
                gaps.append({
                    "type": "resolved_market_missing_winner_identity",
                    "position_id": local.get("position_id"),
                    "condition_id": local.get("condition_id"),
                })
                continue

            linked_intent_id = str(
                local.get("active_exit_intent_id")
                or local.get("tp_intent_id")
                or ""
            )
            linked = None
            if (
                not linked_intent_id
                and str(local.get("state") or "").upper() == "QUARANTINED"
            ):
                quarantine = self.strategy_repo.open_quarantine_for_position(
                    str(local.get("position_id") or "")
                )
                quarantine_evidence = (
                    dict(quarantine.get("evidence") or {})
                    if quarantine else {}
                )
                recovered_remote_id = str(
                    quarantine_evidence.get("remote_order_id") or ""
                )
                recovered_intent = (
                    self.strategy_repo.intent_by_remote_order(
                        recovered_remote_id
                    )
                    if recovered_remote_id else None
                )
                if recovered_intent is not None:
                    linked_intent_id = str(recovered_intent["intent_id"])
            if linked_intent_id:
                linked = self.strategy_repo.intent(linked_intent_id)
                if (
                    linked is not None
                    and str(linked.get("state") or "").upper()
                    not in FINAL_INTENT_STATES
                    and str(local.get("state") or "").upper()
                    not in {"EXIT_RECONCILIATION_REQUIRED", "QUARANTINED"}
                ):
                    # Never race a still-unresolved remote exit with resolution
                    # cleanup. A reconciliation-required exit is the narrow
                    # exception handled below: the authoritative repair exists
                    # specifically to settle that otherwise-stuck identity.
                    continue

            balance = await authoritative_token_balance(self.adapter, token_id)
            evidence_source = "conditional_token_balance"
            observed = balance
            if observed is None and token_id in remote_by_token:
                observed = (
                    decimal_value(remote_by_token[token_id].get("size"))
                    or Decimal("0")
                )
                evidence_source = "remote_position_presence"
            if observed is None:
                # Public position absence alone is weak eventual-consistency
                # evidence and can never terminalize a local financial row.
                self._resolved_zero_observations.pop(token_id, None)
                continue

            winner = token_id == winner_token
            if linked_intent_id and str(local.get("state") or "").upper() in {
                "EXIT_RECONCILIATION_REQUIRED", "QUARANTINED"
            }:
                exit_repair = self._try_authoritative_exit_repair(
                    local=local,
                    authoritative_balance=observed,
                    market=market,
                    actor=actor,
                    linked_intent_id=linked_intent_id,
                )
                repair_status = str(exit_repair.get("status") or "")
                if repair_status == "repaired":
                    repairs.append(exit_repair)
                    handled_tokens.add(token_id)
                    continue
                if repair_status == "quarantined":
                    repairs.append({
                        "type": "position_quarantined",
                        "position_id": local.get("position_id"),
                        "token_id": token_id,
                        "evidence": exit_repair.get("evidence"),
                    })
                    handled_tokens.add(token_id)
                    continue
                if repair_status == "confirmation_pending":
                    gaps.append({
                        "type": "authoritative_exit_repair_confirmation_pending",
                        "position_id": local.get("position_id"),
                        "token_id": token_id,
                        **dict(exit_repair.get("evidence") or {}),
                    })
                    handled_tokens.add(token_id)
                    continue
            if (
                linked is not None
                and str(linked.get("state") or "").upper()
                not in FINAL_INTENT_STATES
            ):
                # The narrow authoritative repair above did not resolve the
                # live exit, so ordinary market-resolution cleanup must still
                # fail closed rather than race it.
                continue
            if str(local.get("state") or "").upper() == "QUARANTINED":
                handled_tokens.add(token_id)
                continue
            if observed > 0:
                self._resolved_zero_observations.pop(token_id, None)
                if not winner:
                    min_order = decimal_value(market.get("min_order_size")) or Decimal("0")
                    local_state = str(local.get("state") or "").upper()
                    terminal_dust = bool(
                        local_state == "DUST"
                        and local.get("closed_at")
                        and (
                            decimal_value(local.get("sellable_shares_text"))
                            or Decimal("0")
                        ) <= 0
                        and min_order > 0
                        and observed < min_order
                    )
                    if terminal_dust:
                        handled_tokens.add(token_id)
                        continue
                    if local_state == "EXITING":
                        residue_repair = (
                            self.strategy_repo.terminalize_exit_residue(
                                position_id=str(local["position_id"]),
                                authoritative_balance=observed,
                                min_order_size=min_order,
                                actor=actor,
                                evidence_source=evidence_source,
                                market_resolved=bool(
                                    market.get("market_resolved")
                                ),
                                winning_asset_id=winner_token,
                            )
                        )
                        if residue_repair.get("status") == "repaired":
                            repairs.append(residue_repair)
                            handled_tokens.add(token_id)
                            continue
                    acquired_local = (
                        decimal_value(local.get("acquired_shares_text"))
                        or Decimal("0")
                    )
                    remaining_local = (
                        decimal_value(local.get("remaining_shares_text"))
                        or Decimal("0")
                    )
                    never_partially_exited = (
                        acquired_local > 0
                        and abs(remaining_local - acquired_local)
                        <= Decimal("0.001")
                        and (
                            decimal_value(local.get("exit_value_text"))
                            or Decimal("0")
                        )
                        <= 0
                    )
                    full_balance_still_held = (
                        abs(observed - acquired_local) <= Decimal("0.001")
                    )
                    if (
                        evidence_source == "conditional_token_balance"
                        and not linked_intent_id
                        and local_state not in {"QUARANTINED", "RESOLVED_LOSER"}
                        and never_partially_exited
                        and full_balance_still_held
                        and not _within_position_propagation_grace(
                            local.get("created_at")
                        )
                    ):
                        # A losing position that rode all the way to resolution
                        # without a STOP still holds its full, now-worthless
                        # token balance. That balance is expected for a loser --
                        # you do not redeem losers -- so it is not a contra-
                        # diction. Terminalize it as RESOLVED_LOSER (booking the
                        # already-realised total loss) instead of wedging
                        # reconciliation forever. The strategy hardening
                        # (state-based STOP, immediate hot-state publication)
                        # is what prevents new positions from reaching this
                        # state; this only unblocks the ones that already did.
                        updated = self.strategy_repo.mark_position_resolved(
                            str(local["position_id"]),
                            winner=False,
                            redeem_pending=False,
                            authoritative=True,
                        )
                        repair = {
                            "type": "resolved_loser_full_balance_terminal",
                            "position_id": local["position_id"],
                            "token_id": token_id,
                            "before_state": local_state,
                            "after_state": updated.get("state"),
                            "authoritative_balance": canonical_decimal(observed),
                        }
                        repairs.append(repair)
                        self.repo.audit(
                            actor,
                            "resolved_position_repair",
                            "ok",
                            "RESOLVED_LOSER_FULL_BALANCE_TERMINAL",
                            repair,
                        )
                        handled_tokens.add(token_id)
                        continue
                    gaps.append({
                        "type": "resolved_loser_authoritative_balance_active",
                        "position_id": local["position_id"],
                        "token_id": token_id,
                        "authoritative_balance": canonical_decimal(observed),
                        "evidence_source": evidence_source,
                    })
                    continue
                before_state = str(local.get("state") or "")
                updated = self.strategy_repo.mark_position_resolved(
                    str(local["position_id"]),
                    winner=True,
                    redeem_pending=True,
                    authoritative=True,
                )
                if str(updated.get("state") or "") != before_state:
                    repair = {
                        "type": "resolved_winner_marked_redeem_pending",
                        "position_id": local["position_id"],
                        "token_id": token_id,
                        "before_state": before_state,
                        "after_state": updated.get("state"),
                        "authoritative_balance": canonical_decimal(observed),
                        "evidence_source": evidence_source,
                    }
                    repairs.append(repair)
                    self.repo.audit(
                        actor,
                        "resolved_position_repair",
                        "ok",
                        "RESOLVED_WINNER_AUTHORITATIVE_BALANCE",
                        repair,
                    )
                continue

            # A zero is actionable only from the authoritative token-balance
            # endpoint, after creation propagation grace and a second complete
            # reconciliation observation.
            if (
                evidence_source != "conditional_token_balance"
                or _within_position_propagation_grace(local.get("created_at"))
            ):
                self._resolved_zero_observations.pop(token_id, None)
                continue
            observations = self._resolved_zero_observations.get(token_id, 0) + 1
            self._resolved_zero_observations[token_id] = observations
            if observations < 2:
                continue
            if winner:
                gaps.append({
                    "type": "resolved_winner_authoritative_zero",
                    "position_id": local["position_id"],
                    "token_id": token_id,
                    "authoritative_balance": "0",
                    "confirmations": observations,
                })
                continue
            before_state = str(local.get("state") or "")
            updated = self.strategy_repo.mark_position_resolved(
                str(local["position_id"]),
                winner=False,
                redeem_pending=False,
                authoritative=True,
            )
            self._resolved_zero_observations.pop(token_id, None)
            repair = {
                "type": "resolved_loser_authoritative_zero",
                "position_id": local["position_id"],
                "token_id": token_id,
                "before_state": before_state,
                "after_state": updated.get("state"),
                "authoritative_balance": "0",
                "confirmations": observations,
                "evidence_source": evidence_source,
            }
            repairs.append(repair)
            self.repo.audit(
                actor,
                "resolved_position_repair",
                "ok",
                "RESOLVED_LOSER_AUTHORITATIVE_ZERO",
                repair,
            )
        return handled_tokens

    async def run_once(
        self,
        actor: str = "system",
        *,
        ready_publish_guard: Callable[[], bool] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        async with self._run_lock:
            now = time.monotonic()
            if not force and now < self._rate_limit_retry_after:
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
                if ready_publish_guard is None:
                    result = await self._run_once_serialized(actor)
                else:
                    result = await self._run_once_serialized(
                        actor, ready_publish_guard=ready_publish_guard
                    )
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
            "trade_fetch": dict(self._last_trade_fetch),
        }

    async def _run_once_serialized(
        self,
        actor: str,
        *,
        ready_publish_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        run_id = self.repo.start_reconciliation()
        gaps: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        auto_recoverable_gaps = 0
        # ENTRY intents deferred this pass because their remote order status
        # is MATCHED/FILLED but no local fill has propagated yet. Populated
        # by the entries loop below; consulted by the remote-positions loop
        # so a position discovered via get_positions() before get_trades()
        # catches up resolves the same intent instead of reading as an
        # unexplained new position.
        pending_entry_tokens: dict[str, dict[str, Any]] = {}
        pending_exit_tokens: set[str] = set()
        try:
            identity = {"status": "MOCK"}
            if hasattr(self.adapter, "identity_preflight"):
                identity = await self.adapter.identity_preflight()  # type: ignore[attr-defined]
                if identity.get("status") != "VERIFIED":
                    gaps.append({"type": "identity_preflight_failed", "status": identity.get("status")})

            if identity.get("status") == "VERIFIED" and hasattr(self.adapter, "get_closed_only_mode"):
                account_mode = await self.adapter.get_closed_only_mode()  # type: ignore[attr-defined]
                account_status = str(account_mode.get("status") or "")
                if account_status == "FAILED":
                    # The *check* failed (timeout, rate limit, 5xx) -- that is
                    # not evidence the account is restricted. Treating it as a
                    # financial contradiction escalated a transient API blip
                    # into a permanent MANUAL_ONLY pause. Raise it as a
                    # temporary error so the normal backoff/retry path owns it.
                    raise TransientAccountModeError(
                        str(account_mode.get("error") or "closed-only check failed")
                    )
                if account_status != "FULL_TRADING":
                    gaps.append({
                        "type": "account_not_full_trading",
                        "status": account_status,
                        "error": str(account_mode.get("error") or ""),
                    })
            balance = await self.adapter.get_balance()
            allowances = await self.adapter.get_allowances()
            remote_open = await self.adapter.get_open_orders()
            remote_trades, trade_fetch, commit_trade_watermark = (
                await self._read_account_trades(actor)
            )
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
                    # Window-scoped now, not lifetime: the account's whole trade
                    # history is no longer re-read on every run.
                    "trades_count": len(remote_trades),
                    "positions_count": len(remote_positions),
                    "allowance_contracts": len(allowances.get("allowances_raw") or {}),
                    "trade_fetch": trade_fetch,
                },
            })

            remote_by_id = {
                str(item.get("polymarket_order_id") or item.get("id")): item
                for item in remote_open
                if item.get("polymarket_order_id") or item.get("id")
            }
            local_orders = self.repo.non_final_orders()
            local_orders_by_remote_id = {
                str(order.get("polymarket_order_id")): order
                for order in local_orders if order.get("polymarket_order_id")
            }
            strategy_intents_by_remote_id: dict[str, dict[str, Any]] = {}
            if self.strategy_repo:
                strategy_remote_ids = set(remote_by_id)
                for trade in remote_trades:
                    trade_order_id = str(
                        trade.get("polymarket_order_id") or ""
                    )
                    if trade_order_id:
                        strategy_remote_ids.add(trade_order_id)
                    raw = trade.get("raw_message") or {}
                    for child in raw.get("maker_orders") or []:
                        if not isinstance(child, dict):
                            continue
                        child_order_id = str(child.get("order_id") or "")
                        if child_order_id:
                            strategy_remote_ids.add(child_order_id)
                strategy_intents_by_remote_id = (
                    self.strategy_repo.intents_by_remote_orders(
                        strategy_remote_ids
                    )
                )
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
            local_remote_ids = set(local_orders_by_remote_id)
            for remote_id in remote_by_id:
                legacy_known = remote_id in local_remote_ids
                strategy_known = remote_id in strategy_intents_by_remote_id
                if not legacy_known and not strategy_known:
                    gaps.append({
                        "type": "remote_order_missing_local",
                        "polymarket_order_id": remote_id,
                    })

            for trade in remote_trades:
                order_id = str(trade.get("polymarket_order_id") or "")
                legacy = local_orders_by_remote_id.get(order_id)
                if legacy and trade.get("price") is not None and trade.get("size") is not None:
                    self.repo.add_fill(int(legacy["local_order_id"]), trade)
                if self.strategy_repo and order_id:
                    intent = strategy_intents_by_remote_id.get(order_id)
                    if intent and trade.get("price") is not None and trade.get("size") is not None:
                        trade_market = self.repo.latest_market(
                            str(trade.get("condition_id") or intent.get("condition_id") or "")
                        )
                        fee_value, fee_status, fee_source = resolve_trade_fee(
                            trade, trade_market
                        )
                        self.strategy_repo.add_fill(
                            intent_id=str(intent["intent_id"]),
                            remote_trade_id=str(trade.get("polymarket_trade_id") or "") or None,
                            shares=decimal_value(trade.get("size")) or Decimal("0"),
                            price=decimal_value(trade.get("price")) or Decimal("0"),
                            fee=fee_value,
                            fee_verification_status=fee_status,
                            fee_source=fee_source,
                            status=str(trade.get("status") or "MATCHED").upper(),
                            transaction_hash=trade.get("transaction_hash"),
                            matched_at=trade.get("matched_at"),
                            raw=trade.get("raw_message") or {},
                        )

            if self.strategy_repo:
                repairs.extend(ingest_maker_exit_fills(
                    self.repo, self.strategy_repo, remote_trades, remote_by_id,
                    strategy_intents_by_remote_id,
                    self._drifted_exit_intents(),
                ))

            # Every trade in the fetched window is now persisted. Only here may
            # the watermark move; anything that raised above leaves it behind so
            # the next run re-reads the same range instead of skipping it.
            commit_trade_watermark()

            if self.strategy_repo:
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

            resolved_handled_tokens = await self._reconcile_resolved_local_positions(
                remote_positions=remote_positions,
                gaps=gaps,
                repairs=repairs,
                actor=actor,
            )

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
                            market_row = self.repo.latest_market(
                                str(intent["condition_id"])
                            ) or {}
                            if bool(market_row.get("market_resolved")) or not bool(
                                market_row.get("accepting_orders", True)
                            ):
                                # The market closed while the intent was still
                                # waiting for a sellable balance, so it can
                                # never be submitted. Only the strategy hot
                                # path cancelled these, and it stops receiving
                                # frames once the market resolves -- leaving
                                # the intent unresolved forever and holding
                                # entries down. It never reached Polymarket
                                # (no remote id, no fills), so cancelling is
                                # local bookkeeping only; the position settles
                                # through redemption.
                                self.strategy_repo.finalize_cancel(
                                    str(intent["intent_id"]),
                                    True,
                                    "WAITING_SELLABLE_MARKET_CLOSED",
                                )
                            continue

                        if (
                            intent_state == "RECONCILIATION_REQUIRED"
                            and str(intent.get("action") or "").upper() in {"EXIT", "TP"}
                            and str(intent.get("order_type") or "").upper() == "FAK"
                        ):
                            intent_id = str(intent["intent_id"])
                            proof_timestamp = str(
                                intent.get("submitted_at")
                                or intent.get("updated_at")
                                or intent.get("created_at")
                                or ""
                            )
                            if _within_intent_submission_grace(proof_timestamp):
                                self._unknown_zero_effect_observations.pop(intent_id, None)
                                continue
                            token_id = str(intent.get("token_id") or "")
                            matching_open = sum(
                                1
                                for item in remote_open
                                if str(item.get("token_id") or item.get("asset_id") or "") == token_id
                                and str(item.get("side") or "").upper() == "SELL"
                            )
                            matching_trades = sum(
                                1
                                for item in remote_trades
                                if str(item.get("token_id") or item.get("asset_id") or "") == token_id
                                and str(item.get("side") or "").upper() == "SELL"
                            )
                            authoritative = await authoritative_token_balance(
                                self.adapter, token_id
                            )
                            if authoritative is not None:
                                signature = (
                                    canonical_decimal(authoritative),
                                    matching_open,
                                    matching_trades,
                                )
                                previous = self._unknown_zero_effect_observations.get(
                                    intent_id
                                )
                                observations = (
                                    previous[1] + 1
                                    if previous is not None and previous[0] == signature
                                    else 1
                                )
                                self._unknown_zero_effect_observations[intent_id] = (
                                    signature,
                                    observations,
                                )
                                if observations >= 2:
                                    try:
                                        repaired = resolve_unknown_open_exit_zero_effect(
                                            self.strategy_repo,
                                            intent_id,
                                            authoritative_balance=authoritative,
                                            identity_verified=identity.get("status") == "VERIFIED",
                                            matching_open_orders=matching_open,
                                            matching_sell_trades=matching_trades,
                                            confirmations=observations,
                                            actor=actor,
                                        )
                                    except (RuntimeError, ValueError) as exc:
                                        gaps.append({
                                            "type": "unknown_exit_zero_effect_proof_rejected",
                                            "intent_id": intent_id,
                                            "reason": str(exc),
                                        })
                                    else:
                                        repairs.append({
                                            "type": "unknown_exit_zero_effect_resolved",
                                            **repaired,
                                        })
                                        self._unknown_zero_effect_observations.pop(
                                            intent_id, None
                                        )
                                        continue
                                else:
                                    gaps.append({
                                        "type": "unknown_exit_zero_effect_confirmation_pending",
                                        "intent_id": intent_id,
                                        "confirmations": observations,
                                    })
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
                    remote_unreadable = ""
                    if remote is None:
                        try:
                            remote = await self.adapter.get_order(remote_id)
                        except Exception as exc:
                            # One unparseable order must never abort the whole
                            # run. It used to: the order that needed resolving
                            # was itself the one whose response could not be
                            # decoded, so every reconciliation died on it and
                            # the intent could never be finalised -- the stuck
                            # order prevented its own resolution.
                            remote = None
                            remote_unreadable = f"{type(exc).__name__}: {exc}"[:200]
                    summary = self.strategy_repo.fill_summary(str(intent["intent_id"]))
                    filled = summary["shares"]
                    if remote_unreadable:
                        market_row = self.repo.latest_market(
                            str(intent["condition_id"])
                        ) or {}
                        closed = bool(
                            market_row.get("market_resolved")
                        ) or not bool(market_row.get("accepting_orders", True))
                        if (
                            closed
                            and intent.get("action") in {"EXIT", "TP"}
                            and filled > 0
                        ):
                            # The market is resolved and no longer accepting
                            # orders, so this order cannot fill again whatever
                            # its unreadable payload says. The fills already
                            # recorded locally are the complete picture.
                            requested = (
                                decimal_value(intent.get("requested_shares_text"))
                                or filled
                            )
                            self.strategy_repo.finalize_exit_intent_state(
                                str(intent["intent_id"]),
                                final_state=(
                                    "FILLED" if filled >= requested
                                    else "PARTIAL_FINAL"
                                ),
                                reason="REMOTE_ORDER_UNREADABLE_MARKET_CLOSED",
                            )
                            continue
                        gaps.append({
                            "type": "remote_order_unreadable",
                            "intent_id": intent["intent_id"],
                            "remote_order_id": remote_id,
                            "error": remote_unreadable,
                        })
                        continue
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
                        if (
                            status in MATCHED_AWAITING_FILL_STATUSES
                            and _within_intent_submission_grace(
                                str(
                                    intent.get("submitted_at")
                                    or intent.get("created_at")
                                    or ""
                                )
                            )
                        ):
                            # The exchange's order-status endpoint and its
                            # trades/positions feeds are not atomically
                            # consistent. A MATCHED/FILLED order with no
                            # local fill yet is a hand-off still in flight,
                            # not a confirmed zero-fill -- the same bounded
                            # uncertainty already tolerated above for
                            # RESERVED/SUBMITTING intents. Re-check next
                            # cycle; if the remote-positions loop below
                            # discovers the fill first, it resolves this same
                            # intent directly via pending_entry_tokens.
                            pending_entry_tokens[str(intent["token_id"])] = intent
                            continue
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
                        requested = decimal_value(intent.get("requested_shares_text")) or filled
                        final_state = (
                            "PARTIAL" if is_open else
                            "FILLED" if filled >= requested else "PARTIAL_FINAL"
                        )
                        if delta <= 0 and not is_open:
                            # Every fill this order produced is already
                            # recorded and it can no longer fill, so it is
                            # finished. Finalising only happened as a side
                            # effect of applying a *new* fill, which left an
                            # order that ended with an unsellable remainder
                            # (below min_order_size) stuck non-final forever,
                            # blocking entries as an UNRESOLVED_INTENT.
                            self.strategy_repo.finalize_exit_intent_state(
                                str(intent["intent_id"]),
                                final_state=final_state,
                                reason="REMOTE_ORDER_CLOSED_NO_FURTHER_FILL",
                            )
                            continue
                        if delta > 0:
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
                        if status in MATCHED_AWAITING_FILL_STATUSES:
                            # MATCHED/FILLED order status can lead the trades
                            # and balance feeds. Preserve the durable remote
                            # identity and keep the exit unresolved: zero fill
                            # evidence is not evidence of a zero-filled exit.
                            self.strategy_repo.update_intent(
                                str(intent["intent_id"]),
                                state="RECONCILIATION_REQUIRED",
                                reason_code=(
                                    "REMOTE_MATCHED_FILL_PROPAGATION_PENDING"
                                ),
                            )
                            pending_exit_tokens.add(str(intent["token_id"]))
                            if not _within_intent_submission_grace(
                                str(
                                    intent.get("submitted_at")
                                    or intent.get("created_at")
                                    or ""
                                )
                            ):
                                if intent.get("position_id"):
                                    self.strategy_repo.require_exit_reconciliation(
                                        str(intent["position_id"])
                                    )
                                gaps.append({
                                    "type": (
                                        "exit_matched_without_fill_evidence"
                                    ),
                                    "intent_id": intent["intent_id"],
                                    "position_id": intent.get("position_id"),
                                    "token_id": intent.get("token_id"),
                                    "polymarket_order_id": remote_id,
                                    "status": status,
                                })
                            continue
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
                    pending_entry = pending_entry_tokens.pop(token_id, None)
                    if existing_position is None and pending_entry is not None:
                        # Authoritative linkage: a still-open ENTRY intent
                        # for this exact token whose remote order status was
                        # already observed as matched/filled earlier in this
                        # same pass (see MATCHED_AWAITING_FILL_STATUSES
                        # above). The positions feed is simply the first
                        # endpoint to confirm the fill here -- resolve the
                        # intent through the normal fill path instead of
                        # treating the position as unexplained.
                        entry_market = self.repo.latest_market(condition_id) or {}
                        average_price = decimal_value(remote.get("average_price")) or Decimal("0")
                        self.strategy_repo.open_position(
                            event_id=str(pending_entry.get("event_id") or condition_id),
                            condition_id=condition_id,
                            token_id=token_id,
                            outcome=str(pending_entry.get("side") or outcome),
                            shares=shares,
                            average_price=average_price,
                            cost_all_in=shares * average_price,
                            fees=Decimal("0"),
                            sellable_shares=Decimal("0"),
                            min_sellable=(
                                decimal_value(entry_market.get("min_order_size"))
                                or Decimal("0")
                            ),
                            entry_intent_id=str(pending_entry["intent_id"]),
                        )
                        continue
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
                    if (
                        bool(market.get("market_resolved"))
                        and str(market.get("winning_asset_id") or "")
                        and token_id != str(market.get("winning_asset_id") or "")
                        and existing_position is not None
                    ):
                        existing_state = str(
                            existing_position.get("state") or ""
                        ).upper()
                        min_order = decimal_value(market.get("min_order_size")) or Decimal("0")

                        terminal_dust = bool(
                            existing_state == "DUST"
                            and existing_position.get("closed_at")
                            and (
                                decimal_value(
                                    existing_position.get("sellable_shares_text")
                                )
                                or Decimal("0")
                            ) <= 0
                            and min_order > 0
                            and shares < min_order
                        )
                        if terminal_dust:
                            continue

                        if existing_state in {"RESOLVED_WINNER", "REDEEM_PENDING"}:
                            corrected = self.strategy_repo.mark_position_resolved(
                                str(existing_position["position_id"]),
                                winner=False,
                                redeem_pending=False,
                                authoritative=True,
                            )
                            after_state = str(corrected.get("state") or "").upper()
                            if after_state == "RESOLVED_LOSER":
                                repair = {
                                    "type": "authoritative_terminal_loser_correction",
                                    "position_id": existing_position["position_id"],
                                    "token_id": token_id,
                                    "before_state": existing_state,
                                    "after_state": after_state,
                                }
                                repairs.append(repair)
                                self.repo.audit(
                                    actor,
                                    "resolved_position_repair",
                                    "ok",
                                    "AUTHORITATIVE_TERMINAL_LOSER_CORRECTION",
                                    repair,
                                )
                                continue

                            gaps.append({
                                "type": "authoritative_terminal_loser_correction_blocked",
                                "position_id": existing_position["position_id"],
                                "token_id": token_id,
                                "before_state": existing_state,
                                "after_state": after_state,
                            })
                            continue

                        if existing_state == "RESOLVED_LOSER":
                            continue

                        if existing_state == "REDEEMED":
                            gaps.append({
                                "type": "redeemed_loser_remote_balance_contradiction",
                                "position_id": existing_position["position_id"],
                                "token_id": token_id,
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
                        gaps.append({
                            "type": "redeemable_position_awaiting_authoritative_resolution",
                            "position_id": position["position_id"],
                            "condition_id": condition_id,
                        })
                for local in self.strategy_repo.reconciliation_positions():
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
                        if str(local.get("token_id")) in resolved_handled_tokens:
                            continue
                        if str(local.get("state") or "").upper() == "QUARANTINED":
                            # Known scoped exposure remains observed by balance/
                            # resolution paths without re-creating a global gap.
                            continue
                        if str(local.get("token_id")) in pending_exit_tokens:
                            # Remote absence while a known EXIT is MATCHED but
                            # its fill feed is pending is the expected opposite
                            # side of the same propagation race. The intent
                            # remains durable and unresolved; do not create a
                            # second position contradiction for that absence.
                            continue
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
                retry_after_seconds = self._schedule_gap_backoff(actor, gaps)
            else:
                retry_after_seconds = 0.0
                self._reset_backoff(actor)
            completed_at = now_iso()
            self.repo.finish_reconciliation(run_id, status, sanitize(gaps))
            readiness_publish_allowed = bool(
                gaps
                or ready_publish_guard is None
                or ready_publish_guard()
            )
            if not gaps:
                mark_reconciled_provenance(self.repo)
            if self.strategy_repo:
                repairable_gap_set = bool(
                    gaps and auto_recoverable_gaps == len(gaps)
                )
                if readiness_publish_allowed:
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
                else:
                    self.repo.audit(
                        actor,
                        "reconciliation_readiness_publish",
                        "ignored",
                        "STALE_WS_GENERATION",
                        {"run_id": run_id},
                    )
                if gaps:
                    self.strategy_repo.alert(
                        alert_type="RECONCILIATION", severity="CRITICAL",
                        reason_code="RECONCILIATION_MISMATCH",
                        message=f"Reconciliation found {len(gaps)} mismatch(es); entries paused",
                        entity_type="account", entity_id="remote_truth",
                    )
                else:
                    for resolved_type, resolved_reason, entity_type, entity_id in (
                        (
                            "RECONCILIATION", "RECONCILIATION_MISMATCH",
                            "account", "remote_truth",
                        ),
                        (
                            "RECONCILIATION", "RECONCILIATION_FAILED",
                            "account", "remote_truth",
                        ),
                        (
                            "SERVICE_RESTART", "RESTART_WITH_OPEN_STATE",
                            "", "",
                        ),
                    ):
                        self.strategy_repo.resolve_alert(
                            alert_type=resolved_type,
                            reason_code=resolved_reason,
                            entity_type=entity_type,
                            entity_id=entity_id,
                            actor=actor,
                            resolution_reason="CLEAN_RECONCILIATION",
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
                        "trade_fetch": trade_fetch,
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
                "published_readiness": readiness_publish_allowed,
            }
        except asyncio.CancelledError:
            # Cancellation is a BaseException on supported Python versions,
            # so the generic handler below cannot terminalize the row.
            task = asyncio.current_task()
            task_name = task.get_name() if task is not None else ""
            if actor == "market_ws_reconnect":
                cancellation_reason = "CANCELLED_WS_SESSION"
            elif task_name == "live-reconciliation":
                cancellation_reason = "CANCELLED_SERVICE_SHUTDOWN"
            else:
                cancellation_reason = "CANCELLED_RECONCILIATION_TASK"
            self.repo.finish_reconciliation(
                run_id, "failed", sanitize(gaps), cancellation_reason
            )
            self.repo.audit(
                actor,
                "live_reconciliation",
                "failed",
                cancellation_reason,
                {"run_id": run_id, "cancelled": True},
            )
            raise
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
