from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .market_discovery import _dump
from .repository import LiveRepository


def _winner(market: dict[str, Any]) -> tuple[str, str] | None:
    outcomes = market.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return None
    winners: list[tuple[str, str]] = []
    for outcome in outcomes.values():
        if not isinstance(outcome, dict):
            continue
        try:
            price = Decimal(str(outcome.get("price")))
        except (InvalidOperation, ValueError):
            continue
        token_id = str(outcome.get("token_id") or "")
        label = str(outcome.get("label") or "")
        if price == Decimal("1") and token_id and label:
            winners.append((token_id, label))
    return winners[0] if len(winners) == 1 else None


class MarketResolutionReconciler:
    """Cold-path resolver hosted by its own process, apart from trading."""

    def __init__(
        self,
        repo: LiveRepository,
        *,
        client_factory: Callable[[], Any] | None = None,
        grace_seconds: int = 60,
        batch_size: int = 10,
        clock: Callable[[], float] = time.time,
    ):
        self.repo = repo
        self.client_factory = client_factory
        self.grace_seconds = max(0, int(grace_seconds))
        self.batch_size = max(1, min(int(batch_size), 100))
        self.clock = clock

    async def run_once(self) -> dict[str, int]:
        if self.client_factory is None:
            from polymarket import AsyncPublicClient
            client = AsyncPublicClient()
        else:
            client = self.client_factory()
        rows = self.repo.markets_pending_resolution(
            ended_before_epoch=int(self.clock()) - self.grace_seconds,
            limit=self.batch_size,
        )
        result = {"checked": 0, "resolved": 0, "pending": 0, "errors": 0}
        try:
            for row in rows:
                event_id = str(row.get("event_id") or "")
                result["checked"] += 1
                try:
                    event = _dump(await client.get_event(slug=event_id))
                    markets = event.get("markets") or []
                    market = next((item for item in markets if isinstance(item, dict)
                        and str(item.get("condition_id") or "")
                        == str(row.get("condition_id") or "")), None)
                    state = (market or {}).get("state") or {}
                    winner = _winner(market or {})
                    if state.get("closed") is not True or winner is None:
                        result["pending"] += 1
                        continue
                    winning_asset_id, winning_outcome = winner
                    expected_assets = {str(row.get("yes_token_id") or ""),
                                       str(row.get("no_token_id") or "")}
                    if winning_asset_id not in expected_assets:
                        raise ValueError("winner token does not match stored market tokens")
                    self.repo.mark_market_resolved_from_rest(
                        str(row["condition_id"]), winning_asset_id, winning_outcome)
                    self.repo.audit("market_resolution", "market_resolved", "ok",
                        details={"event_id": event_id, "condition_id": row["condition_id"],
                                 "winning_outcome": winning_outcome})
                    result["resolved"] += 1
                except Exception as exc:
                    result["errors"] += 1
                    self.repo.audit("market_resolution", "resolution_refresh", "error",
                        f"{type(exc).__name__}: {exc}"[:500],
                        {"event_id": event_id, "condition_id": row.get("condition_id")})
        finally:
            await client.close()
        return result
