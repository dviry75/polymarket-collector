from __future__ import annotations

from typing import Any

from .account_identity import MockPublicAccountIdentityClient, PublicAccountIdentityClient
from .backup import LiveBackupManager
from .public_client import MockPublicClobClient, PublicClobClient
from .pause_recovery import PauseRecoveryCoordinator, request_manual_resume
from .repository import now_iso
from .secrets import EnvSecretProvider, GoogleSecretManagerProvider, secret_readiness
from .strategy_repository import sanitize


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
