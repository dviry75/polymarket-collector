from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable

from .config import LiveConfig
from .recovery_policy import PauseState, ReleasePolicy, recovery_policy
from .repository import LiveRepository, now_iso
from .strategy_repository import StrategyRepository


UNKNOWN_INTENT_STATES = {
    "RESERVED", "SUBMITTING", "SUBMITTED", "LIVE", "PARTIAL",
    "RECONCILIATION_REQUIRED", "CANCEL_REQUESTED", "CANCEL_UNCERTAIN",
}
UNKNOWN_POSITION_STATES = {"EXIT_RECONCILIATION_REQUIRED"}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_ms(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() * 1000)


@dataclass(frozen=True)
class RecoveryBlocker:
    code: str
    source: str
    details: str = ""
    age_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseEvaluation:
    safe_to_resume: bool
    evaluated_at: str
    pause_generation: int
    blockers: tuple[RecoveryBlocker, ...]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "safe_to_resume": self.safe_to_resume,
            "evaluated_at": self.evaluated_at,
            "pause_generation": self.pause_generation,
            "blockers": [item.as_dict() for item in self.blockers],
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class PauseRecoveryResult:
    resumed: bool
    blockers: tuple[str, ...]
    owner: str
    reason: str
    generation: int = 0
    state: str = ""


class EntryReleaseEvaluator:
    """The sole source of truth for entry-release safety."""

    def __init__(
        self,
        config: LiveConfig,
        repo: LiveRepository,
        strategy_repo: StrategyRepository,
        market_ws: Any,
        user_ws: Any,
    ) -> None:
        self.config = config
        self.repo = repo
        self.strategy_repo = strategy_repo
        self.market_ws = market_ws
        self.user_ws = user_ws

    @staticmethod
    def _add(
        blockers: list[RecoveryBlocker],
        code: str,
        source: str,
        details: str = "",
        age_ms: float | None = None,
    ) -> None:
        if code not in {item.code for item in blockers}:
            blockers.append(RecoveryBlocker(code, source, details, age_ms))

    def evaluate_entry_release_gates(
        self, *, allow_manual_policy: bool = False
    ) -> ReleaseEvaluation:
        now = datetime.now(timezone.utc)
        evaluated_at = now.isoformat()
        pause = self.strategy_repo.pause_record()
        blockers: list[RecoveryBlocker] = []
        evidence: dict[str, Any] = {}

        market = self.market_ws.health()
        evidence["market_ws"] = market
        if market.get("status") != "CONNECTED":
            self._add(
                blockers, "MARKET_WS_NOT_CONNECTED", "market_ws",
                str(market.get("status") or "UNKNOWN"),
            )
        if market.get("stale") is True:
            self._add(blockers, "MARKET_DATA_STALE", "market_ws")

        all_books = market.get("books") or {}
        subscribed = set(market.get("subscribed_asset_ids") or all_books)
        books = {
            str(key): value
            for key, value in all_books.items()
            if key in subscribed
        }
        if not books:
            self._add(blockers, "BOOK_NOT_READY", "market_books", "no subscribed books")
        market_age_limit_ms = float(self.config.max_market_data_age_seconds) * 1000
        book_evidence: dict[str, Any] = {}
        for token_id, book in books.items():
            ready = bool(book.get("ready"))
            age = book.get("exchange_age_ms")
            try:
                numeric_age = float(age) if age is not None else None
            except (TypeError, ValueError):
                numeric_age = None
            book_evidence[token_id] = {
                "ready": ready,
                "reason": book.get("reason"),
                "exchange_age_ms": numeric_age,
            }
            if not ready:
                self._add(
                    blockers, "BOOK_NOT_READY", "market_books",
                    f"{token_id}:{book.get('reason') or 'NOT_READY'}",
                    numeric_age,
                )
            if numeric_age is None or numeric_age > market_age_limit_ms:
                self._add(
                    blockers, "MARKET_DATA_STALE", "market_books",
                    f"{token_id}:exchange timestamp outside configured threshold",
                    numeric_age,
                )
        evidence["books"] = book_evidence
        evidence["market_age_limit_ms"] = market_age_limit_ms

        user = self.user_ws.health()
        user_age = _age_ms(user.get("last_message_at"), now=now)
        evidence["user_ws"] = {**user, "age_ms": user_age}
        if user.get("status") != "CONNECTED":
            self._add(
                blockers, "USER_WS_NOT_CONNECTED", "user_ws",
                str(user.get("status") or "UNKNOWN"),
                user_age,
            )
        user_limit_ms = float(self.config.max_user_state_age_seconds) * 1000
        if (
            user.get("stale") is True
            or user_age is None
            or user_age > user_limit_ms
        ):
            self._add(
                blockers, "USER_WS_STALE", "user_ws",
                "private state freshness not proven", user_age,
            )

        strategy_readiness = self.repo.get_state(
            "strategy_readiness", "NOT_READY"
        )
        if strategy_readiness != "READY":
            self._add(
                blockers, "MARKET_READINESS_NOT_READY", "strategy",
                self.repo.get_state("strategy_block_reason", ""),
            )

        reconciliation_readiness = self.repo.get_state(
            "reconciliation_readiness", "NOT_READY"
        )
        last_clean_reconciliation = self.repo.get_state(
            "last_successful_reconciliation_at", ""
        )
        evidence["reconciliation"] = {
            "readiness": reconciliation_readiness,
            "last_clean_at": last_clean_reconciliation,
        }
        if reconciliation_readiness != "READY":
            self._add(
                blockers, "RECONCILIATION_NOT_READY", "reconciliation",
                self.repo.get_state("reconciliation_block_reason", ""),
            )
        if self.repo.get_state(
            "live_blocked_by_reconciliation", "true"
        ).lower() == "true":
            self._add(
                blockers, "RECONCILIATION_NOT_CLEAN", "reconciliation"
            )

        if self.repo.kill_switch_active():
            self._add(blockers, "KILL_SWITCH_ACTIVE", "operator")

        if self.config.execution_mode == "REAL_TRADING":
            heartbeat = self.repo.get_state(
                "order_heartbeat_status", "DISABLED"
            )
            heartbeat_at = self.repo.get_state(
                "last_successful_heartbeat_at", ""
            )
            evidence["heartbeat"] = {
                "status": heartbeat,
                "last_success_at": heartbeat_at,
            }
            if heartbeat != "OK":
                self._add(
                    blockers, "HEARTBEAT_NOT_OK", "heartbeat", heartbeat
                )
            if pause.get("pause_cause") == "HEARTBEAT_FAILURE" and not (
                heartbeat_at
                and heartbeat_at > str(pause.get("pause_acquired_at") or "")
            ):
                self._add(
                    blockers,
                    "HEARTBEAT_SUCCESS_NOT_NEWER_THAN_PAUSE",
                    "heartbeat",
                )

        unresolved = self.strategy_repo.entry_blocking_intents()
        if unresolved:
            self._add(
                blockers, "UNRESOLVED_INTENT", "financial_state",
                f"{len(unresolved)} unresolved intent(s)",
            )
        if any(
            str(intent.get("state") or "").upper() in UNKNOWN_INTENT_STATES
            for intent in unresolved
        ):
            self._add(
                blockers,
                "UNKNOWN_ORDER_FILL_OR_CANCELLATION",
                "financial_state",
            )
        if any(
            str(position.get("state") or "").upper()
            in UNKNOWN_POSITION_STATES
            for position in self.strategy_repo.entry_blocking_positions()
        ):
            self._add(
                blockers,
                "UNKNOWN_POSITION_OR_EXPOSURE",
                "financial_state",
            )

        geographic = self.repo.get_state(
            "geographic_availability", "NOT_CHECKED"
        )
        geographic_checked_at = self.repo.get_state(
            "geographic_checked_at", ""
        )
        geographic_age = _age_ms(geographic_checked_at, now=now)
        geographic_ttl_ms = float(
            getattr(self.config, "geographic_preflight_ttl_seconds", 3600)
        ) * 1000
        evidence["geography"] = {
            "status": geographic,
            "checked_at": geographic_checked_at,
            "age_ms": geographic_age,
        }
        if geographic != "ALLOWED":
            self._add(
                blockers, "GEOGRAPHIC_NOT_ALLOWED", "geographic", geographic
            )
        elif geographic_age is None or geographic_age > geographic_ttl_ms:
            self._add(
                blockers,
                "GEOGRAPHIC_EVIDENCE_STALE",
                "geographic",
                age_ms=geographic_age,
            )

        config_errors = self.config.validation_errors()
        evidence["config_errors"] = config_errors
        if config_errors:
            self._add(
                blockers, "CONFIG_INVALID", "configuration",
                "; ".join(config_errors),
            )

        if (
            self.config.execution_mode == "REAL_TRADING"
            and not self.config.continuous_trading_enabled
            and self.repo.get_state(
                "canary_armed", "false"
            ).lower() != "true"
        ):
            self._add(blockers, "CANARY_NOT_ARMED", "canary")

        generation = int(pause.get("pause_generation", 0) or 0)
        release_policy = str(
            pause.get("release_policy")
            or recovery_policy(str(pause.get("pause_cause") or "")).release_policy
        )
        acquired_at = str(pause.get("pause_acquired_at") or "")
        financial_verified = self.repo.get_state(
            "recovery_financial_verified_generation", ""
        )
        if (
            pause.get("pause_entries")
            and release_policy == ReleasePolicy.MANUAL_ONLY
            and not allow_manual_policy
        ):
            self._add(
                blockers, "MANUAL_ONLY_PAUSE", "pause_policy",
                str(pause.get("pause_cause") or "UNKNOWN"),
            )
        if (
            pause.get("pause_entries")
            and release_policy
            == ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION
            and financial_verified != str(generation)
        ):
            self._add(
                blockers,
                "FINANCIAL_REPAIR_NOT_VERIFIED",
                "reconciliation",
            )

        cause = str(pause.get("pause_cause") or "")
        clean_after_pause_causes = {
            "RECONCILIATION_GAP",
            "RECONCILIATION_RATE_LIMITED",
            "RECONCILIATION_TEMPORARY_ERROR",
            "USER_WS_DOWN",
            "USER_WS_STALE",
            "USER_WS_QUEUE_LOSS",
            "USER_WS_PERSISTENCE_UNCERTAIN",
            "EXIT_RECONCILIATION_REQUIRED",
            "CANCEL_UNCERTAIN",
            "SAFETY_STARTUP_HOLD",
            "STARTUP_RECONCILIATION_REQUIRED",
        }
        if (
            pause.get("pause_entries")
            and cause in clean_after_pause_causes
            and not (
                acquired_at
                and last_clean_reconciliation
                and last_clean_reconciliation > acquired_at
            )
        ):
            self._add(
                blockers,
                "RECONCILIATION_CLEAN_EVIDENCE_NOT_NEWER_THAN_PAUSE",
                "reconciliation",
            )

        if self.repo.get_state(
            "recovery_engine_status", "STARTING"
        ) == "DEGRADED":
            self._add(
                blockers, "AUTO_RECOVERY_DEGRADED", "recovery_engine"
            )

        return ReleaseEvaluation(
            safe_to_resume=not blockers,
            evaluated_at=evaluated_at,
            pause_generation=generation,
            blockers=tuple(blockers),
            evidence=evidence,
        )


class PauseRecoveryCoordinator:
    """Single state machine for detection, verification, stability and release."""

    BLOCKER_TO_CAUSE = {
        "CONFIG_INVALID": "CONFIG_INVALID",
        "KILL_SWITCH_ACTIVE": "KILL_SWITCH_ACTIVE",
        "CANARY_NOT_ARMED": "CANARY_NOT_ARMED",
        "GEOGRAPHIC_NOT_ALLOWED": "GEOGRAPHIC_AVAILABILITY_FAILED",
        "GEOGRAPHIC_EVIDENCE_STALE": "GEOGRAPHIC_EVIDENCE_STALE",
        "MARKET_WS_NOT_CONNECTED": "MARKET_WS_DOWN",
        "USER_WS_NOT_CONNECTED": "USER_WS_DOWN",
        "USER_WS_STALE": "USER_WS_STALE",
        "MARKET_DATA_STALE": "MARKET_DATA_STALE",
        "BOOK_NOT_READY": "BOOK_NOT_READY",
        "HEARTBEAT_NOT_OK": "HEARTBEAT_FAILURE",
        "AUTO_RECOVERY_DEGRADED": "RECOVERY_MONITOR_TEMPORARY_ERROR",
        "UNRESOLVED_INTENT": "RECONCILIATION_GAP",
        "UNKNOWN_ORDER_FILL_OR_CANCELLATION": "RECONCILIATION_GAP",
        "UNKNOWN_POSITION_OR_EXPOSURE": "RECONCILIATION_GAP",
    }

    def __init__(
        self,
        repo: LiveRepository,
        strategy_repo: StrategyRepository,
        market_ws: Any,
        user_ws: Any,
        *,
        config: LiveConfig | None = None,
    ) -> None:
        self.repo = repo
        self.strategy_repo = strategy_repo
        self.market_ws = market_ws
        self.user_ws = user_ws
        self.config = config or LiveConfig()
        self._blocker_first_seen: dict[str, float] = {}
        self.evaluator = EntryReleaseEvaluator(
            self.config, repo, strategy_repo, market_ws, user_ws
        )

    def evaluate_entry_release_gates(
        self, *, allow_manual_policy: bool = False
    ) -> ReleaseEvaluation:
        return self.evaluator.evaluate_entry_release_gates(
            allow_manual_policy=allow_manual_policy
        )

    def blockers(self) -> list[str]:
        return [
            blocker.code
            for blocker in self.evaluate_entry_release_gates().blockers
        ]

    def _cause_for_blocker(self, blocker: RecoveryBlocker) -> str | None:
        if blocker.code == "MARKET_READINESS_NOT_READY":
            return (
                self.repo.get_state("strategy_block_reason", "").upper()
                or "MARKET_DATA_NOT_READY"
            )
        if blocker.code in {
            "RECONCILIATION_NOT_READY",
            "RECONCILIATION_NOT_CLEAN",
        }:
            return (
                self.repo.get_state(
                    "reconciliation_block_reason", ""
                ).upper()
                or "RECONCILIATION_FAILED"
            )
        return self.BLOCKER_TO_CAUSE.get(blocker.code)

    def _acquire_from_evaluation(
        self, evaluation: ReleaseEvaluation
    ) -> None:
        now = time.monotonic()
        present_codes = {blocker.code for blocker in evaluation.blockers}
        self._blocker_first_seen = {
            code: first_seen
            for code, first_seen in self._blocker_first_seen.items()
            if code in present_codes
        }
        already_paused = self.strategy_repo.pause_entries()
        debounced_codes = {
            "BOOK_NOT_READY",
            "MARKET_DATA_STALE",
            "MARKET_READINESS_NOT_READY",
        }
        causes: list[str] = []
        for blocker in evaluation.blockers:
            cause = self._cause_for_blocker(blocker)
            if not cause:
                continue
            policy = recovery_policy(cause)
            if (
                not already_paused
                and blocker.code in debounced_codes
                and policy.release_policy == ReleasePolicy.AUTO_WHEN_CLEAN
            ):
                first_seen = self._blocker_first_seen.setdefault(
                    blocker.code, now
                )
                if (
                    now - first_seen
                    < self.config.recovery_detection_debounce_seconds
                ):
                    continue
            causes.append(cause)
        if not causes:
            return
        causes.sort(
            key=lambda reason: {
                ReleasePolicy.AUTO_WHEN_CLEAN: 1,
                ReleasePolicy.AUTO_AFTER_REPAIR_AND_VERIFICATION: 2,
                ReleasePolicy.MANUAL_ONLY: 3,
            }[recovery_policy(reason).release_policy],
            reverse=True,
        )
        self.strategy_repo.acquire_pause(
            actor="pause_recovery",
            reason=causes[0],
            owner="MACHINE",
        )

    def attempt_auto_resume(self) -> PauseRecoveryResult:
        evaluation = self.evaluate_entry_release_gates()
        self._acquire_from_evaluation(evaluation)
        record = self.strategy_repo.pause_record()
        generation = int(record.get("pause_generation", 0) or 0)
        owner = str(record.get("pause_owner") or "NONE")
        reason = str(record.get("pause_cause") or record.get("pause_reason") or "")

        if not record.get("pause_entries"):
            detecting = bool(evaluation.blockers)
            self.repo.set_states(
                {
                    "recovery_status": (
                        "DETECTING" if detecting else "HEALTHY"
                    ),
                    "recovery_engine_status": "HEALTHY",
                    "recovery_blockers_json": "[]",
                },
                "pause_recovery",
            )
            return PauseRecoveryResult(
                False,
                tuple(
                    blocker.code for blocker in evaluation.blockers
                ),
                owner, reason, generation, PauseState.TRADING,
            )

        blocker_dicts = [
            blocker.as_dict() for blocker in evaluation.blockers
        ]
        if blocker_dicts:
            self.strategy_repo.reset_stability(
                expected_generation=generation,
                blockers=blocker_dicts,
            )
            return PauseRecoveryResult(
                False,
                tuple(blocker.code for blocker in evaluation.blockers),
                owner,
                reason,
                generation,
                str(record.get("pause_state") or ""),
            )

        eligible_since = str(record.get("pause_eligible_since") or "")
        if not eligible_since:
            started_at = now_iso()
            self.strategy_repo.set_waiting_stability(
                expected_generation=generation,
                eligible_since=started_at,
            )
            return PauseRecoveryResult(
                False, ("STABILITY_WINDOW",), owner, reason,
                generation, PauseState.PAUSED_WAITING_STABILITY,
            )

        start = _parse_time(eligible_since)
        elapsed = (
            (datetime.now(timezone.utc) - start).total_seconds()
            if start is not None
            else 0.0
        )
        if elapsed < float(self.config.recovery_stability_seconds):
            return PauseRecoveryResult(
                False, ("STABILITY_WINDOW",), owner, reason,
                generation, PauseState.PAUSED_WAITING_STABILITY,
            )

        released = self.strategy_repo.release_pause_cas(
            expected_generation=generation,
            expected_owner=owner,
            actor="pause_recovery",
            reason="AUTO_RECOVERY_GATES_STABLE",
        )
        return PauseRecoveryResult(
            released,
            () if released else ("STALE_RELEASE_REJECTED",),
            owner,
            reason,
            generation,
            PauseState.TRADING if released else str(record.get("pause_state") or ""),
        )

    def tick(self) -> PauseRecoveryResult:
        self.repo.set_states(
            {"recovery_engine_status": "HEALTHY"}, "pause_recovery"
        )
        self.strategy_repo.escalate_unknown_pause_if_expired(
            actor="pause_recovery"
        )
        return self.attempt_auto_resume()

    def mark_degraded(self, exc: Exception) -> None:
        self.repo.set_states(
            {
                "recovery_engine_status": "DEGRADED",
                "recovery_last_action": "RECOVERY_TICK",
                "recovery_last_result": (
                    f"{type(exc).__name__}: {exc}"
                )[:500],
            },
            "pause_recovery",
        )
        self.strategy_repo.acquire_pause(
            actor="pause_recovery",
            reason="RECOVERY_MONITOR_TEMPORARY_ERROR",
            owner="MACHINE",
        )

    def status(self) -> dict[str, Any]:
        evaluation = self.evaluate_entry_release_gates()
        record = self.strategy_repo.pause_record()
        eligible_since = _parse_time(record.get("pause_eligible_since"))
        elapsed_ms = (
            int(
                max(
                    0.0,
                    (datetime.now(timezone.utc) - eligible_since).total_seconds(),
                )
                * 1000
            )
            if eligible_since
            else 0
        )
        return {
            "trading_status": (
                "PAUSED"
                if record.get("pause_entries")
                else "GATED"
                if evaluation.blockers
                else "ENABLED"
            ),
            "auto_recovery_status": self.repo.get_state(
                "recovery_status", "UNKNOWN"
            ),
            "pause": record,
            "pause_age_seconds": (
                (_age_ms(record.get("pause_acquired_at"), now=datetime.now(timezone.utc)) or 0)
                / 1000
            ),
            "current_blockers": [
                blocker.as_dict() for blocker in evaluation.blockers
            ],
            "stability_elapsed_ms": elapsed_ms,
            "stability_target_ms": int(self.config.recovery_stability_seconds * 1000),
            "last_recovery_action": self.repo.get_state(
                "recovery_last_action", ""
            ),
            "last_recovery_result": self.repo.get_state(
                "recovery_last_result", ""
            ),
            "last_auto_recovery_at": self.repo.get_state(
                "last_auto_recovery_at", ""
            ),
            "evaluation": evaluation.as_dict(),
        }

def request_manual_resume(
    config: LiveConfig,
    repo: LiveRepository,
    strategy_repo: StrategyRepository,
    market_ws: Any,
    user_ws: Any,
) -> dict[str, Any]:
    """Operator resume uses the same gates; it only bypasses MANUAL_ONLY policy."""
    evaluator = EntryReleaseEvaluator(
        config, repo, strategy_repo, market_ws, user_ws
    )
    evaluation = evaluator.evaluate_entry_release_gates(
        allow_manual_policy=True
    )
    if evaluation.blockers:
        return {
            "ok": False,
            "reason": "READINESS_FAILED",
            "blockers": [
                blocker.as_dict() for blocker in evaluation.blockers
            ],
            "evaluation": evaluation.as_dict(),
        }
    record = strategy_repo.pause_record()
    if not record.get("pause_entries"):
        return {
            "ok": True,
            "pause_entries": False,
            "already_resumed": True,
            "evaluation": evaluation.as_dict(),
        }
    released = strategy_repo.release_pause_cas(
        expected_generation=int(record["pause_generation"]),
        expected_owner=str(record.get("pause_owner") or "NONE"),
        actor="operator",
        reason="READINESS_VERIFIED",
    )
    return {
        "ok": released,
        "pause_entries": not released,
        "reason": "" if released else "STALE_RELEASE_REJECTED",
        "blockers": [] if released else [{
            "code": "STALE_RELEASE_REJECTED",
            "source": "pause_state",
            "details": "pause generation or owner changed during evaluation",
            "age_ms": None,
        }],
        "evaluation": evaluation.as_dict(),
    }
