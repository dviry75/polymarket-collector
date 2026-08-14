from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .repository import LiveRepository
from .strategy_repository import StrategyRepository


AUTO_OWNERS = {"MACHINE", "RECONCILIATION"}
UNKNOWN_INTENT_STATES = {
    "RESERVED", "SUBMITTING", "SUBMITTED", "LIVE", "PARTIAL",
    "RECONCILIATION_REQUIRED", "CANCEL_REQUESTED", "CANCEL_UNCERTAIN",
}
UNKNOWN_POSITION_STATES = {"EXIT_RECONCILIATION_REQUIRED"}


@dataclass(frozen=True)
class PauseRecoveryResult:
    resumed: bool
    blockers: tuple[str, ...]
    owner: str
    reason: str


class PauseRecoveryCoordinator:
    """Own transient entry holds and release only after a single fail-closed gate set."""

    def __init__(
        self,
        repo: LiveRepository,
        strategy_repo: StrategyRepository,
        market_ws: Any,
        user_ws: Any,
        *,
        freshness_limit_ms: int = 1000,
    ) -> None:
        self.repo = repo
        self.strategy_repo = strategy_repo
        self.market_ws = market_ws
        self.user_ws = user_ws
        self.freshness_limit_ms = freshness_limit_ms

    def _market_blockers(self) -> list[str]:
        health = self.market_ws.health()
        blockers: list[str] = []
        if health.get("status") != "CONNECTED":
            blockers.append("MARKET_WS_NOT_CONNECTED")
        if health.get("stale") is True:
            blockers.append("MARKET_DATA_STALE")
        all_books = health.get("books") or {}
        subscribed = set(health.get("subscribed_asset_ids") or all_books)
        books = {key: value for key, value in all_books.items() if key in subscribed}
        if not books or any(not book.get("ready") for book in books.values()):
            blockers.append("BOOK_NOT_READY")
        ages = [book.get("exchange_age_ms") for book in books.values()]
        if not ages or any(age is None or float(age) > self.freshness_limit_ms for age in ages):
            blockers.append("MARKET_FRESHNESS_OVER_1000MS")
        return blockers

    def blockers(self) -> list[str]:
        blockers = self._market_blockers()
        if self.user_ws.health().get("status") != "CONNECTED":
            blockers.append("USER_WS_NOT_CONNECTED")
        if self.repo.get_state("strategy_readiness", "NOT_READY") != "READY":
            blockers.append("MARKET_READINESS_NOT_READY")
        if self.repo.get_state("reconciliation_readiness", "NOT_READY") != "READY":
            blockers.append("RECONCILIATION_NOT_READY")
        if self.repo.get_state("live_blocked_by_reconciliation", "true").lower() == "true":
            blockers.append("RECONCILIATION_NOT_CLEAN")
        if self.repo.kill_switch_active():
            blockers.append("KILL_SWITCH_ACTIVE")

        intents = self.strategy_repo.unresolved_intents()
        if intents:
            blockers.append("UNRESOLVED_INTENT")
        if any(str(intent.get("state") or "").upper() in UNKNOWN_INTENT_STATES for intent in intents):
            blockers.append("UNKNOWN_ORDER_FILL_OR_CANCELLATION")
        if any(str(position.get("state") or "").upper() in UNKNOWN_POSITION_STATES
               for position in self.strategy_repo.active_positions()):
            blockers.append("UNKNOWN_POSITION_OR_EXPOSURE")
        return list(dict.fromkeys(blockers))

    def _acquire_for_runtime_fault(self) -> None:
        market = self.market_ws.health()
        user = self.user_ws.health()
        if market.get("status") != "CONNECTED":
            self.strategy_repo.set_pause_entries(
                True, "pause_recovery", "MARKET_WS_DOWN",
                owner="MACHINE", auto_recoverable=True,
            )
        elif user.get("status") != "CONNECTED":
            self.strategy_repo.set_pause_entries(
                True, "pause_recovery", "USER_WS_DOWN",
                owner="MACHINE", auto_recoverable=True,
            )
        elif market.get("stale") is True:
            self.strategy_repo.set_pause_entries(
                True, "pause_recovery", "MARKET_DATA_STALE",
                owner="MACHINE", auto_recoverable=True,
            )
        else:
            all_books = market.get("books") or {}
            subscribed = set(market.get("subscribed_asset_ids") or all_books)
            books = {key: value for key, value in all_books.items() if key in subscribed}
            ages = [book.get("exchange_age_ms") for book in books.values()]
            if not books or any(not book.get("ready") for book in books.values()):
                self.strategy_repo.set_pause_entries(
                    True, "pause_recovery", "BOOK_NOT_READY",
                    owner="MACHINE", auto_recoverable=True,
                )
            elif any(age is None or float(age) > self.freshness_limit_ms for age in ages):
                self.strategy_repo.set_pause_entries(
                    True, "pause_recovery", "MARKET_DATA_STALE",
                    owner="MACHINE", auto_recoverable=True,
                )

    def attempt_auto_resume(self) -> PauseRecoveryResult:
        owner = self.repo.get_state("pause_owner", "NONE").upper()
        reason = self.repo.get_state("pause_reason", "")
        auto = self.repo.get_state("pause_auto_recoverable", "false").lower() == "true"
        if not self.strategy_repo.pause_entries() or owner not in AUTO_OWNERS or not auto:
            return PauseRecoveryResult(False, ("PAUSE_NOT_MACHINE_RECOVERABLE",), owner, reason)
        blockers = self.blockers()
        if blockers:
            return PauseRecoveryResult(False, tuple(blockers), owner, reason)
        self.strategy_repo.set_pause_entries(
            False, "pause_recovery", "AUTO_RECOVERY_GATES_CLEAN", owner=owner,
        )
        return PauseRecoveryResult(True, (), owner, reason)

    def tick(self) -> PauseRecoveryResult:
        self._acquire_for_runtime_fault()
        return self.attempt_auto_resume()
