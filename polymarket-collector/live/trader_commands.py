from __future__ import annotations

from decimal import Decimal
from typing import Any

from .account_identity import MockPublicAccountIdentityClient, PublicAccountIdentityClient
from .backup import LiveBackupManager
from .reconciliation_stability import authoritative_token_balance
from .public_client import MockPublicClobClient, PublicClobClient
from .pause_recovery import PauseRecoveryCoordinator, request_manual_resume
from .repository import now_iso
from .secrets import EnvSecretProvider, GoogleSecretManagerProvider, secret_readiness
from .strategy_repository import sanitize


REDEMPTION_SIZE_TOLERANCE = Decimal("0.0001")


def select_verified_redemption(
    activities: list[dict[str, Any]],
    *,
    wallet: str,
    condition_id: str,
    token_id: str,
    remaining_shares: Decimal,
) -> dict[str, Any] | None:
    """Select exact public redemption proof; reject approximate identities."""
    for item in activities:
        if str(item.get("type") or "").upper() != "REDEEM":
            continue
        if str(item.get("proxyWallet") or "").lower() != wallet.lower():
            continue
        if str(item.get("conditionId") or "").lower() != condition_id.lower():
            continue
        if str(item.get("asset") or "") != token_id:
            continue
        transaction_hash = str(item.get("transactionHash") or "")
        try:
            redeemed_shares = Decimal(str(item.get("size")))
        except Exception:
            continue
        if not transaction_hash.startswith("0x") or len(transaction_hash) != 66:
            continue
        if redeemed_shares < remaining_shares:
            continue
        if redeemed_shares - remaining_shares > REDEMPTION_SIZE_TOLERANCE:
            continue
        return {**item, "redeemed_shares": str(redeemed_shares)}
    return None


class TraderCommandHandler:
    """Single owner for all state-changing Dashboard commands."""

    async def __call__(self, command: str, payload: dict[str, Any]) -> Any:
        # Import lazily to avoid a router/configuration import cycle.
        from .router import (
            paper_service, reconciliation_coordinator, services,
            strategy_services,
        )

        config, repo, adapter, _risk, _orders, reconciliation, market_ws, user_ws, engine, _auth, dry_run = services()
        strategy_repo, runtime = strategy_services()
        command = command.upper()

        if command == "STATUS":
            reconciliation_health = reconciliation.health()
            reconciliation_health["coordinator"] = (
                reconciliation_coordinator().health()
            )
            return sanitize({
                "adapter": {"name": adapter.name},
                "reconciliation": reconciliation_health,
                "market_ws": market_ws.health(),
                "user_ws": user_ws.health(),
                "strategy": runtime.health(),
                "recovery": PauseRecoveryCoordinator(
                    repo, strategy_repo, market_ws, user_ws, config=config
                ).status(),
                "paper": paper_service().health(),
                "provenance": repo.states_with_prefix("provenance_"),
            })
        if command == "AUDIT":
            repo.audit(
                str(payload.get("actor") or "dashboard"),
                str(payload.get("action") or "dashboard_action"),
                str(payload.get("status") or "ok"),
                str(payload.get("reason") or ""),
                payload.get("details") if isinstance(payload.get("details"), dict) else None,
            )
            return {"ok": True}
        if command == "REVOKE_SESSIONS":
            return {"session_version": repo.revoke_all_sessions("operator")}
        if command == "PAUSE_ENTRIES":
            strategy_repo.set_pause_entries(True, "operator", "OPERATOR_PAUSE")
            return {"ok": True, "pause_entries": True}
        if command == "RESUME_ENTRIES":
            result = request_manual_resume(
                config, repo, strategy_repo, market_ws, user_ws
            )
            if not result.get("ok"):
                strategy_repo.timeline(
                    severity="WARNING", category="OPERATOR", component="ipc",
                    source="operator", requested_action="RESUME_ENTRIES",
                    reason_code=str(result.get("reason") or "READINESS_FAILED"),
                    result_status="BLOCKED",
                    parameters_json={"blockers": result.get("blockers", [])},
                )
            return result
        if command == "EMERGENCY_CLOSE_PREVIEW":
            positions = strategy_repo.active_positions()
            ids = {position.get("position_id") for position in positions}
            intents = [
                intent for intent in strategy_repo.unresolved_intents()
                if intent.get("position_id") in ids
            ]
            return sanitize({"positions": positions, "intents": intents})
        if command == "EMERGENCY_CLOSE_EXECUTE":
            result = await runtime.emergency_close_all(
                market_ws.order_books,
                actor=str(payload.get("actor") or "operator"),
            )
            return sanitize(result)
        if command == "ACK_ALERT":
            return sanitize(strategy_repo.acknowledge_alert(int(payload["alert_id"]), "operator"))
        if command == "KILL_SWITCH_SET":
            active = bool(payload.get("active"))
            if active:
                repo.set_state(
                    "reconciliation_auto_recovery_pending", "false", "operator"
                )
            repo.set_state("kill_switch", "true" if active else "false", "operator")
            return {"ok": True, "kill_switch": active}
        if command == "RECONCILIATION_RUN":
            return await reconciliation_coordinator().request(
                actor=str(payload.get("actor") or "operator"), force=True
            )
        if command == "RESOLVE_UNKNOWN_CLOSED_FAK":
            intent_id = str(payload.get("intent_id") or "")
            intent = strategy_repo.intent(intent_id)
            if not intent:
                raise KeyError(intent_id)
            token_id = str(intent.get("token_id") or "")
            condition_id = str(intent.get("condition_id") or "")
            if not token_id or not condition_id:
                raise RuntimeError("intent lacks token/condition identity")

            identity = (
                await adapter.identity_preflight()
                if hasattr(adapter, "identity_preflight")
                else {"status": "MOCK"}
            )
            open_orders = await adapter.get_open_orders()
            trades = await adapter.get_trades()
            balance = await authoritative_token_balance(adapter, token_id)
            if balance is None:
                raise RuntimeError("authoritative conditional balance unavailable")

            matching_open_orders = [
                order for order in open_orders
                if (
                    str(order.get("token_id") or "") == token_id
                    or str(order.get("condition_id") or "") == condition_id
                )
            ]
            matching_sell_trades = [
                trade for trade in trades
                if (
                    str(trade.get("token_id") or "") == token_id
                    and str(trade.get("side") or "").lower() == "sell"
                )
            ]
            recovery = strategy_repo.resolve_unknown_closed_fak_zero_fill(
                intent_id,
                authoritative_balance=balance,
                identity_verified=str(identity.get("status") or "").upper()
                in {"VERIFIED", "MOCK"},
                matching_open_orders=len(matching_open_orders),
                matching_sell_trades=len(matching_sell_trades),
                actor=str(payload.get("actor") or "operator"),
            )
            reconciliation_result = await reconciliation_coordinator().request(
                actor="operator:unknown_fak_recovery",
                evidence_changed=True,
                force=True,
            )
            return sanitize({
                "ok": reconciliation_result.get("status") == "ok",
                "recovery": recovery,
                "reconciliation": reconciliation_result,
            })
        if command == "RESOLVE_REDEEMED_POSITION":
            position_id = str(payload.get("position_id") or "")
            position = next(
                (
                    item for item in strategy_repo.active_positions()
                    if str(item.get("position_id") or "") == position_id
                ),
                None,
            )
            if position is None:
                raise KeyError(position_id)
            condition_id = str(position.get("condition_id") or "")
            token_id = str(position.get("token_id") or "")
            remaining = Decimal(
                str(position.get("remaining_shares_text") or "0")
            )
            market = repo.latest_market(condition_id)
            if (
                not market
                or not bool(market.get("market_resolved"))
                or str(market.get("winning_asset_id") or "") != token_id
            ):
                raise RuntimeError(
                    "position is not the verified market winner"
                )
            unresolved = [
                item for item in strategy_repo.unresolved_intents()
                if str(item.get("position_id") or "") == position_id
            ]
            if unresolved:
                raise RuntimeError("position has unresolved execution intents")
            identity = await adapter.identity_preflight()
            if str(identity.get("status") or "").upper() != "VERIFIED":
                raise RuntimeError("account identity is not verified")
            wallet = str(identity.get("wallet") or "")
            if not wallet:
                raise RuntimeError("verified account wallet is missing")
            authoritative_balance = await authoritative_token_balance(
                adapter, token_id
            )
            if authoritative_balance != Decimal("0"):
                raise RuntimeError(
                    "authoritative token balance is not zero"
                )
            matching_open_orders = [
                order for order in await adapter.get_open_orders()
                if (
                    str(order.get("token_id") or "") == token_id
                    or str(order.get("condition_id") or "") == condition_id
                )
            ]
            if matching_open_orders:
                raise RuntimeError("matching remote order is still open")
            activity = await PublicAccountIdentityClient(
                config.data_api_host
            ).redemption_activity(wallet, condition_id)
            proof = select_verified_redemption(
                activity,
                wallet=wallet,
                condition_id=condition_id,
                token_id=token_id,
                remaining_shares=remaining,
            )
            if proof is None:
                raise RuntimeError(
                    "matching public redemption proof was not found"
                )
            resolved = strategy_repo.mark_position_resolved(
                position_id,
                winner=True,
                redeem_pending=True,
                authoritative=True,
            )
            if str(resolved.get("state") or "").upper() != "REDEEM_PENDING":
                raise RuntimeError(
                    "position did not enter redeem-pending state"
                )
            transaction_hash = str(proof["transactionHash"])
            redeemed = strategy_repo.mark_position_redeemed(
                position_id, transaction_hash
            )
            repo.audit(
                str(payload.get("actor") or "operator"),
                "verified_external_redemption_recovery",
                "ok",
                "PUBLIC_REDEMPTION_AND_ZERO_BALANCE_VERIFIED",
                {
                    "position_id": position_id,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "transaction_hash": transaction_hash,
                    "redeemed_shares": proof.get("redeemed_shares"),
                    "authoritative_balance": "0",
                },
            )
            reconciliation_result = await reconciliation_coordinator().request(
                actor="operator:external_redemption_recovery",
                evidence_changed=True,
                force=True,
            )
            return sanitize({
                "ok": reconciliation_result.get("status") == "ok",
                "position": redeemed,
                "redemption": {
                    "transaction_hash": transaction_hash,
                    "redeemed_shares": proof.get("redeemed_shares"),
                },
                "reconciliation": reconciliation_result,
            })
        if command == "CREATE_RULE":
            return repo.create_rule(dict(payload["rule"]))
        if command == "UPDATE_RULE_STATUS":
            return repo.update_rule_status(int(payload["rule_id"]), str(payload["status"]))
        if command == "MOCK_ORDER":
            if config.live_adapter != "mock":
                raise PermissionError("This command is mock-only")
            await reconciliation_coordinator().request(
                actor="operator", evidence_changed=True, force=True
            )
            order_payload = dict(payload.get("payload") or {})
            order_payload.setdefault("idempotency_key", f"manual-{now_iso()}")
            order_payload.setdefault("requested_amount_usd", config.default_trade_amount_usd)
            order_payload.setdefault("order_type", config.entry_order_type)
            return await engine.entry_intent(order_payload, actor="operator")
        if command == "MARKET_WS_FIXTURE":
            stored = market_ws.process_message(dict(payload.get("payload") or {}))
            return {"stored": stored, "status": market_ws.health()}
        if command == "USER_WS_FIXTURE":
            stored = user_ws.process_message(dict(payload.get("payload") or {}))
            return {"stored": stored, "status": user_ws.health()}
        if command == "DRY_RUN":
            return dry_run.preview(
                dict(payload.get("payload") or {}),
                actor=str(payload.get("actor") or "operator"),
            )
        if command == "MARKET_WS_SMOKE":
            asset_ids = [str(item) for item in payload.get("asset_ids") or []]
            return await market_ws.connect_for_messages(
                config.market_ws_url,
                asset_ids,
                max_messages=int(payload.get("max_messages", 1)),
                timeout_seconds=float(payload.get("timeout_seconds", 20)),
            )
        if command == "MAINTENANCE_DRAIN":
            return repo.request_maintenance_drain("operator")
        if command == "MAINTENANCE_CANCEL":
            return repo.cancel_maintenance_drain("operator")
        if command == "MAINTENANCE_READINESS":
            await reconciliation.run_once(actor="maintenance")
            return repo.refresh_maintenance_readiness("operator")
        if command == "BACKUP_CREATE":
            return LiveBackupManager(config, repo).create_backup("manual").__dict__
        if command == "ACCOUNT_REFRESH":
            client = MockPublicAccountIdentityClient() if payload.get("use_mock") else PublicAccountIdentityClient()
            result = await client.resolve(config.profile_address)
            account = result.__dict__
            account["sampled_at"] = result.refreshed_at or now_iso()
            account["account_login_type"] = config.account_login_type
            account["account_identity_status"] = result.status
            repo.store_account_snapshot(account)
            repo.audit("operator", "public_account_identity_refresh", "ok" if result.status != "UNAVAILABLE" else "blocked", result.error, {"status": result.status})
            return {key: value for key, value in account.items() if key != "raw_public_payload"}
        if command == "SECRETS_READINESS":
            provider = (
                GoogleSecretManagerProvider(config.google_project_id, config.google_secret_prefix)
                if config.google_project_id else EnvSecretProvider()
            )
            return secret_readiness(provider)
        if command == "REFRESH_MARKET_METADATA":
            use_mock = bool(payload.get("use_mock", True))
            client = MockPublicClobClient() if use_mock else PublicClobClient(config.clob_host)
            metadata = await client.build_metadata(
                condition_id=str(payload["condition_id"]),
                event_id=payload.get("event_id"),
                gamma_yes_token_id=payload.get("yes_token_id"),
                gamma_no_token_id=payload.get("no_token_id"),
            )
            repo.upsert_market(metadata)
            return metadata

        raise KeyError(f"Unsupported trader IPC command: {command}")
