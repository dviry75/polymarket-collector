from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import random
import time
from typing import Any

from .adapters.base import TradingAdapter
from .order_book import canonical_decimal, decimal_value
from .repository import LiveRepository, now_iso
from .dashboard_schema import mark_reconciled_provenance
from .strategy_repository import StrategyRepository, sanitize



POSITION_PROPAGATION_GRACE_SECONDS = 15.0
RATE_LIMIT_BACKOFF_BASE_SECONDS = 15.0
RATE_LIMIT_BACKOFF_CAP_SECONDS = 120.0


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
        self._consecutive_rate_limits = 0
        self._rate_limit_retry_after = 0.0

    async def run_once(self, actor: str = "system") -> dict[str, Any]:
        async with self._run_lock:
            now = time.monotonic()
            if now < self._rate_limit_retry_after:
                return {
                    "run_id": None,
                    "status": "backoff",
                    "gaps": [],
                    "reason": "RECONCILIATION_RATE_LIMIT_BACKOFF",
                    "retry_after_seconds": round(
                        self._rate_limit_retry_after - now, 3
                    ),
                }
            return await self._run_once_serialized(actor)

    async def _run_once_serialized(self, actor: str) -> dict[str, Any]:
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
                            fee_verification_status=str(trade.get("fee_verification_status") or "UNKNOWN"),
                            fee_source=trade.get("fee_source"),
                            status=str(trade.get("status") or "MATCHED").upper(),
                            transaction_hash=trade.get("transaction_hash"),
                            matched_at=trade.get("matched_at"),
                            raw=trade.get("raw_message") or {},
                        )

            if self.strategy_repo:
                for intent in self.strategy_repo.unresolved_intents():
                    remote_id = str(intent.get("remote_order_id") or "")
                    if not remote_id:
                        if (
                            str(intent.get("state") or "").upper()
                            == "WAITING_SELLABLE"
                        ):
                            # This state is only created after a confirmed
                            # pre-submission balance failure. No remote order
                            # exists, so absence of remote_order_id is expected.
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
                        gaps.append({
                            "type": "remote_position_corrected_local",
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "remote_shares": canonical_decimal(shares),
                        })
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

                        gaps.append({
                            "type": "local_position_missing_remote",
                            "position_id": local["position_id"],
                            "token_id": local["token_id"],
                            "local_shares": canonical_decimal(remaining),
                        })

            status = "ok" if not gaps else "gaps"
            self._consecutive_rate_limits = 0
            self._rate_limit_retry_after = 0.0
            self.repo.finish_reconciliation(run_id, status, sanitize(gaps))
            if not gaps:
                mark_reconciled_provenance(self.repo)
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
            rate_limited = _is_rate_limit_error(exc)
            retry_after_seconds = 0.0
            if rate_limited:
                self._consecutive_rate_limits += 1
                base_delay = min(
                    RATE_LIMIT_BACKOFF_CAP_SECONDS,
                    RATE_LIMIT_BACKOFF_BASE_SECONDS
                    * (2 ** (self._consecutive_rate_limits - 1)),
                )
                retry_after_seconds = min(
                    RATE_LIMIT_BACKOFF_CAP_SECONDS,
                    base_delay * random.uniform(0.8, 1.2),
                )
                self._rate_limit_retry_after = time.monotonic() + retry_after_seconds
            else:
                self._consecutive_rate_limits = 0
                self._rate_limit_retry_after = 0.0
            self.repo.finish_reconciliation(run_id, "failed", sanitize(gaps), safe_error)
            temporary_error = rate_limited or _is_temporary_network_error(exc)
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
