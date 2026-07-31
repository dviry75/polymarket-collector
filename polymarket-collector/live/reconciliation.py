from __future__ import annotations

from typing import Any

from .adapters.base import TradingAdapter
from .repository import LiveRepository


class ReconciliationWorker:
    def __init__(self, repo: LiveRepository, adapter: TradingAdapter):
        self.repo = repo
        self.adapter = adapter

    async def run_once(self, actor: str = "system") -> dict[str, Any]:
        run_id = self.repo.start_reconciliation()
        gaps: list[dict[str, Any]] = []
        try:
            remote_open = await self.adapter.get_open_orders()
            remote_by_id = {str(item.get("polymarket_order_id") or item.get("id")): item for item in remote_open}
            local_orders = self.repo.non_final_orders()
            for order in local_orders:
                remote_id = order.get("polymarket_order_id")
                if remote_id and str(remote_id) not in remote_by_id and order.get("status") in {"live", "submitted", "delayed", "reconciling"}:
                    gaps.append({"type": "local_order_missing_remote", "local_order_id": order["local_order_id"], "polymarket_order_id": remote_id})
            local_remote_ids = {str(order.get("polymarket_order_id")) for order in local_orders if order.get("polymarket_order_id")}
            for remote_id, remote in remote_by_id.items():
                if remote_id not in local_remote_ids:
                    gaps.append({"type": "remote_order_missing_local", "polymarket_order_id": remote_id, "remote": remote})
            for trade in await self.adapter.get_trades():
                order_id = trade.get("local_order_id")
                if not order_id and trade.get("polymarket_order_id"):
                    local = next(
                        (
                            item for item in local_orders
                            if str(item.get("polymarket_order_id")) == str(trade.get("polymarket_order_id"))
                        ),
                        None,
                    )
                    order_id = local.get("local_order_id") if local else None
                if order_id and trade.get("price") is not None and trade.get("size") is not None:
                    self.repo.add_fill(int(order_id), trade)
            for position in await self.adapter.get_positions():
                if position.get("orphan"):
                    gaps.append({"type": "remote_position_missing_local", "position": position})
            status = "ok" if not gaps else "gaps"
            self.repo.finish_reconciliation(run_id, status, gaps)
            self.repo.audit(actor, "live_reconciliation", status, details={"run_id": run_id, "gaps": len(gaps)})
            return {"run_id": run_id, "status": status, "gaps": gaps}
        except Exception as exc:
            self.repo.finish_reconciliation(run_id, "failed", gaps, f"{type(exc).__name__}: {exc}")
            self.repo.audit(actor, "live_reconciliation", "failed", str(exc), {"run_id": run_id})
            return {"run_id": run_id, "status": "failed", "gaps": gaps, "error": str(exc)}
