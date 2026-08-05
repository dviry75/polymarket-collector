from __future__ import annotations

from decimal import Decimal
from typing import Any

from .adapters.base import TradingAdapter
from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso
from .strategy_repository import StrategyRepository, sanitize


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

    async def run_once(self, actor: str = "system") -> dict[str, Any]:
        run_id = self.repo.start_reconciliation()
        gaps: list[dict[str, Any]] = []
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
                            status=str(trade.get("status") or "MATCHED").upper(),
                            transaction_hash=trade.get("transaction_hash"),
                            matched_at=trade.get("matched_at"),
                            raw=trade.get("raw_message") or {},
                        )

            if self.strategy_repo:
                for intent in self.strategy_repo.unresolved_intents():
                    remote_id = str(intent.get("remote_order_id") or "")
                    if not remote_id:
                        gaps.append({
                            "type": "durable_intent_without_remote_id",
                            "intent_id": intent["intent_id"],
                            "state": intent["state"],
                        })
                    elif remote_id in remote_by_id:
                        remote = remote_by_id[remote_id]
                        self.strategy_repo.update_intent(
                            str(intent["intent_id"]),
                            state="LIVE",
                            filled_shares_text=str(remote.get("filled_size") or "0"),
                        )

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
                    _position, changed = self.strategy_repo.reconcile_remote_position(
                        event_id=str(market.get("event_id") or condition_id),
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        remote_shares=shares,
                        average_price=decimal_value(remote.get("average_price")) or Decimal("0"),
                    )
                    if changed:
                        gaps.append({
                            "type": "remote_position_corrected_local",
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "remote_shares": canonical_decimal(shares),
                        })
                for local in self.strategy_repo.active_positions():
                    remaining = decimal_value(local.get("remaining_shares_text")) or Decimal("0")
                    if remaining > 0 and str(local.get("token_id")) not in remote_tokens:
                        gaps.append({
                            "type": "local_position_missing_remote",
                            "position_id": local["position_id"],
                            "token_id": local["token_id"],
                            "local_shares": canonical_decimal(remaining),
                        })

            status = "ok" if not gaps else "gaps"
            self.repo.finish_reconciliation(run_id, status, sanitize(gaps))
            if self.strategy_repo:
                self.strategy_repo.set_reconciliation_state(
                    ready=not gaps,
                    reason="RECONCILIATION_GAP" if gaps else "",
                    actor=actor,
                )
                if gaps:
                    self.strategy_repo.alert(
                        alert_type="RECONCILIATION", severity="CRITICAL",
                        reason_code="RECONCILIATION_MISMATCH",
                        message=f"Reconciliation found {len(gaps)} mismatch(es); entries paused",
                        entity_type="run", entity_id=str(run_id),
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
                        "open_orders": len(remote_open),
                        "trades": len(remote_trades),
                        "positions": len(remote_positions),
                    },
                )
            self.repo.audit(
                actor, "live_reconciliation", status,
                details={"run_id": run_id, "gaps": len(gaps)},
            )
            return {"run_id": run_id, "status": status, "gaps": sanitize(gaps)}
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {exc}"[:500]
            self.repo.finish_reconciliation(run_id, "failed", sanitize(gaps), safe_error)
            if self.strategy_repo:
                self.strategy_repo.set_reconciliation_state(
                    ready=False, reason="RECONCILIATION_FAILED", actor=actor
                )
                self.strategy_repo.alert(
                    alert_type="RECONCILIATION", severity="CRITICAL",
                    reason_code="RECONCILIATION_FAILED", message=safe_error,
                    entity_type="run", entity_id=str(run_id),
                )
            self.repo.audit(
                actor, "live_reconciliation", "failed", safe_error, {"run_id": run_id}
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "gaps": sanitize(gaps),
                "error": safe_error,
            }
