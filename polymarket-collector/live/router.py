from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from openpyxl import Workbook

from .adapters import MockTradingAdapter, RealPolymarketTradingAdapter, TradingAdapter
from .account_identity import MockPublicAccountIdentityClient, PublicAccountIdentityClient
from .auth import COOKIE_NAME, LiveAuthManager
from .backup import LiveBackupManager
from .config import LiveConfig, redact_mapping
from .dry_run import DryRunService
from .market_websocket import MarketWebSocketManager, UserWebSocketManager
from .order_manager import OrderManager
from .public_client import MockPublicClobClient, PublicClobClient
from .reconciliation import ReconciliationWorker
from .repository import LiveRepository, now_iso
from .risk_manager import RiskManager
from .trading_engine import TradingEngine
from .secrets import EnvSecretProvider, GoogleSecretManagerProvider, secret_readiness


router = APIRouter(prefix="/live", tags=["live"])

_repo: LiveRepository | None = None
_config: LiveConfig | None = None
_adapter: TradingAdapter | None = None
_risk: RiskManager | None = None
_orders: OrderManager | None = None
_reconciliation: ReconciliationWorker | None = None
_market_ws: MarketWebSocketManager | None = None
_user_ws: UserWebSocketManager | None = None
_engine: TradingEngine | None = None
_auth: LiveAuthManager | None = None
_dry_run: DryRunService | None = None
_export_state: dict[str, Any] = {"status": "idle", "path": None, "error": None, "row_counts": None}


def configure(db_path: Path | str, config: LiveConfig | None = None) -> None:
    global _repo, _config, _adapter, _risk, _orders, _reconciliation, _market_ws, _user_ws, _engine, _auth, _dry_run
    _config = config or LiveConfig.from_env()
    _repo = LiveRepository(db_path)
    _repo.migrate(_config.live_kill_switch_default)
    _adapter = MockTradingAdapter(_config.adapter_scenario) if _config.live_adapter == "mock" else RealPolymarketTradingAdapter(_config)
    _risk = RiskManager(_config, _repo)
    _orders = OrderManager(_repo, _risk, _adapter)
    _reconciliation = ReconciliationWorker(_repo, _adapter)
    _market_ws = MarketWebSocketManager(_repo, stale_after_seconds=_config.max_market_data_age_seconds)
    _user_ws = UserWebSocketManager(
        _repo, stale_after_seconds=_config.max_user_state_age_seconds,
        reconciliation=lambda: _reconciliation.run_once(actor="user_ws_reconnect"),
    )
    _engine = TradingEngine(_repo, _orders)
    _auth = LiveAuthManager(_config, session_version_getter=lambda: _repo.get_state("session_version", "1") if _repo else "1")
    _dry_run = DryRunService(_repo, _risk)


def services() -> tuple[LiveConfig, LiveRepository, TradingAdapter, RiskManager, OrderManager, ReconciliationWorker, MarketWebSocketManager, UserWebSocketManager, TradingEngine, LiveAuthManager, DryRunService]:
    if _repo is None or _config is None or _adapter is None or _risk is None or _orders is None or _reconciliation is None or _market_ws is None or _user_ws is None or _engine is None or _auth is None or _dry_run is None:
        raise RuntimeError("LIVE services are not configured")
    return _config, _repo, _adapter, _risk, _orders, _reconciliation, _market_ws, _user_ws, _engine, _auth, _dry_run


def require_live_session(request: Request) -> None:
    config, repo, *_rest, auth, _dry = services()
    if not auth.configured():
        repo.audit("anonymous", "live_login", "blocked", "LIVE login is not configured")
        raise HTTPException(status_code=503, detail="LIVE login is not configured; all /live routes are blocked")
    if not auth.verify_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="LIVE login required")


def require_csrf(request: Request, x_live_csrf_token: Optional[str] = Header(default=None)) -> None:
    *_prefix, auth, _dry = services()
    session_token = request.cookies.get(COOKIE_NAME)
    if not auth.verify_csrf(session_token, x_live_csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_operator(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> None:
    require_csrf(request, x_live_csrf_token)
    config, repo, *_ = services()
    if not config.operator_token:
        repo.audit("anonymous", "operator_auth", "blocked", "LIVE_OPERATOR_TOKEN is not configured")
        raise HTTPException(status_code=403, detail="LIVE operator token is not configured; write actions are blocked")
    if x_live_operator_token != config.operator_token:
        repo.audit("anonymous", "operator_auth", "blocked", "Invalid operator token")
        raise HTTPException(status_code=403, detail="Invalid LIVE operator token")


def require_reauth(request: Request, x_live_reauth_password: Optional[str] = Header(default=None)) -> None:
    _config, repo, *_rest, auth, _dry = services()
    if not auth.verify_password(str(x_live_reauth_password or "")):
        repo.audit("operator", "reauth", "blocked", "INVALID_REAUTH")
        raise HTTPException(status_code=403, detail="Password re-authentication required")


@router.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return """
    <!doctype html><html><head><meta charset="utf-8"><title>LIVE Login</title>
    <style>body{font-family:Arial,sans-serif;margin:32px;background:#f6f7f9;color:#111}form{max-width:360px;background:white;border:1px solid #d8dde6;padding:18px;border-radius:6px}label{display:block;margin:10px 0}input{width:100%;box-sizing:border-box;padding:9px}button{padding:9px 12px;background:#111;color:white;border:0;border-radius:6px}</style>
    </head><body><h1>Polymarket LIVE Login</h1><form id="login"><label>Username<input id="u" autocomplete="username"></label><label>Password<input id="p" type="password" autocomplete="current-password"></label><button>Login</button><p id="msg"></p></form>
    <script>document.getElementById('login').addEventListener('submit',async(e)=>{e.preventDefault();const r=await fetch('/live/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});if(r.ok){location.href='/live'}else{msg.textContent=await r.text();}});</script></body></html>
    """


@router.post("/login")
def login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    config, repo, *_rest, auth, _dry = services()
    key = request.client.host if request.client else "unknown"
    if not auth.configured():
        repo.audit("anonymous", "live_login", "blocked", "LOGIN_NOT_CONFIGURED")
        raise HTTPException(status_code=503, detail="LIVE login is not configured")
    if auth.rate_limited(key):
        repo.audit("anonymous", "live_login", "blocked", "RATE_LIMIT")
        raise HTTPException(status_code=429, detail="Too many login attempts")
    if payload.get("username") != config.login_username or not auth.verify_password(str(payload.get("password") or "")):
        auth.record_failure(key)
        repo.audit("anonymous", "live_login", "blocked", "INVALID_LOGIN")
        raise HTTPException(status_code=401, detail="Invalid login")
    token = auth.create_session(config.login_username)
    csrf_token = auth.csrf_token(token)
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=True, samesite="strict", max_age=config.session_ttl_seconds if config.session_ttl_seconds > 0 else None)
    response.headers["X-Live-CSRF-Token"] = csrf_token
    repo.audit(config.login_username, "live_login", "ok")
    return response


@router.post("/logout")
def logout(request: Request, x_live_csrf_token: Optional[str] = Header(default=None)) -> JSONResponse:
    require_live_session(request)
    require_csrf(request, x_live_csrf_token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    services()[1].audit("operator", "live_logout", "ok")
    return response


@router.post("/sessions/revoke-all")
def revoke_all_sessions(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None), x_live_reauth_password: Optional[str] = Header(default=None)) -> JSONResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    require_reauth(request, x_live_reauth_password)
    version = services()[1].revoke_all_sessions("operator")
    response = JSONResponse({"ok": True, "session_version": version})
    response.delete_cookie(COOKIE_NAME)
    return response


def render_table(rows: list[dict[str, Any]], empty: str = "No rows") -> str:
    if not rows:
        return f"<p class=\"muted\">{html.escape(empty)}</p>"
    headers = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(key))}</th>" for key in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td data-label=\"{html.escape(str(key))}\">{html.escape('' if row.get(key) is None else str(row.get(key)))}</td>"
            for key in headers
        ) + "</tr>"
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _tone_for_value(value: Any) -> str:
    text = str(value).lower()
    if text in {"ok", "online", "connected", "allowed", "pass", "false", "mock", "read only"}:
        return "up"
    if any(part in text for part in ["blocked", "killed", "failed", "error", "true", "unverified", "stale", "gap"]):
        return "down"
    if any(part in text for part in ["warn", "missing", "not_configured", "disabled", "never"]):
        return "warn"
    return "info"


def chip(value: Any, tone: str | None = None) -> str:
    actual_tone = tone or _tone_for_value(value)
    return f'<span class="chip {actual_tone}">{_e(value)}</span>'


def panel(title: str, body: str, right: str = "", class_name: str = "") -> str:
    return f"""
    <section class="panel {class_name}">
      <div class="panel-head">
        <div class="panel-title">{_e(title)}</div>
        <div>{right}</div>
      </div>
      <div class="panel-body">{body}</div>
    </section>
    """


def stat_card(label: str, value: Any, tone: str = "neutral", hint: Any = "") -> str:
    return f"""
    <div class="stat-card">
      <div class="stat-label">{_e(label)}</div>
      <div class="stat-value {tone}">{_e(value)}</div>
      <div class="stat-hint">{_e(hint)}</div>
    </div>
    """


def kv_table(rows: list[tuple[str, Any, str | None]]) -> str:
    body = "".join(
        f"<tr><th>{_e(label)}</th><td>{chip(value, tone) if tone else _e(value)}</td></tr>"
        for label, value, tone in rows
    )
    return f'<table class="kv-table"><tbody>{body}</tbody></table>'


def compact_table(rows: list[dict[str, Any]], columns: list[str] | None = None, empty: str = "No rows") -> str:
    if not rows:
        return f'<div class="empty">{_e(empty)}</div>'
    headers = columns or list(rows[0].keys())
    head = "".join(f"<th>{_e(key)}</th>" for key in headers)
    body = ""
    for row in rows:
        body += "<tr>"
        for key in headers:
            value = row.get(key)
            if key in {"status", "result", "final_decision", "account_identity_status"}:
                body += f"<td data-label=\"{_e(key)}\">{chip(value)}</td>"
            else:
                body += f"<td data-label=\"{_e(key)}\">{_e(value)}</td>"
        body += "</tr>"
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def status_label(config: LiveConfig, repo: LiveRepository, market_ws: MarketWebSocketManager) -> str:
    if repo.kill_switch_active():
        return "KILLED"
    if config.real_submission_armed():
        return "ARMED"
    if config.live_module_enabled and config.live_adapter == "mock":
        return "MOCK"
    if config.live_module_enabled:
        return "READ ONLY"
    if market_ws.health().get("stale"):
        return "DISABLED"
    return "DEGRADED"


def dashboard_model() -> dict[str, Any]:
    config, repo, adapter, risk, orders, recon, market_ws, user_ws, engine, auth, dry = services()
    counts = repo.counts()
    latest_market = repo.latest_market()
    daily_rows = repo.list_table("live_daily_limits", 1)
    daily = daily_rows[0] if daily_rows else {}
    account = repo.latest_account_snapshot() or {}
    market_health = market_ws.health()
    user_health = user_ws.health()
    system_mode = status_label(config, repo, market_ws)
    risk_gates = [
        ("Kill Switch", "BLOCKED" if repo.kill_switch_active() else "PASS", "down" if repo.kill_switch_active() else "up"),
        ("Real Submission", "ARMED" if config.real_submission_armed() else "DISABLED", "down" if config.real_submission_armed() else "up"),
        ("Account Identity", repo.get_state("account_identity_status", "UNVERIFIED"), None),
        ("Market WS", f'{market_health.get("status")} / stale={market_health.get("stale")}', None),
        ("User WS", f'{user_health.get("status")} / stale={user_health.get("stale")}', None),
        ("Reconciliation", repo.get_state("last_reconciliation_at", "never"), None),
        ("Open Orders", f'{counts["open_orders"]}/{config.max_open_orders}', "up" if counts["open_orders"] < config.max_open_orders else "down"),
        ("Open Deals", f'{counts["open_deals"]}/{config.max_open_deals}', "up" if counts["open_deals"] < config.max_open_deals else "down"),
        ("Active Rules", f'{counts.get("active_rules", 0)}/{config.max_active_rules}', "up" if counts.get("active_rules", 0) <= config.max_active_rules else "down"),
        ("Exposure", f'{risk.current_exposure_usd()}/{config.max_total_exposure_usd}', "up"),
        ("Daily PnL", f'{daily.get("realized_pnl_usd", 0)}/-{config.max_daily_realized_loss_usd}', "up"),
        ("Failed Orders Streak", f'{daily.get("consecutive_failed_orders", 0)}/{config.max_consecutive_failed_orders}', "up"),
        ("Losing Deals Streak", f'{daily.get("consecutive_losing_deals", 0)}/{config.max_consecutive_losing_deals}', "up"),
    ]
    return {
        "config": config,
        "repo": repo,
        "adapter": adapter,
        "risk": risk,
        "counts": counts,
        "latest_market": latest_market,
        "daily": daily,
        "account": account,
        "market_health": market_health,
        "user_health": user_health,
        "mode": system_mode,
        "risk_gates": risk_gates,
        "auth": auth,
    }


def dashboard_content(view: str = "overview") -> str:
    model = dashboard_model()
    config: LiveConfig = model["config"]
    repo: LiveRepository = model["repo"]
    risk: RiskManager = model["risk"]
    counts = model["counts"]
    latest_market = model["latest_market"]
    daily = model["daily"]
    account = model["account"]
    market_health = model["market_health"]
    user_health = model["user_health"]
    summary = {
        "mode": model["mode"],
        "adapter": config.live_adapter,
        "kill_switch": repo.kill_switch_active(),
        "market_ws": market_health["status"],
        "user_ws": user_health["status"],
        "reconciliation": repo.get_state("last_reconciliation_at", "never"),
        "account_identity": repo.get_state("account_identity_status", "UNVERIFIED"),
        "active_market": latest_market.get("condition_id") if latest_market else "-",
        "one_dollar_valid": latest_market.get("one_dollar_valid") if latest_market else "-",
        "open_orders": counts["open_orders"],
        "open_deals": counts["open_deals"],
        "exposure_usd": risk.current_exposure_usd(),
    }
    stats = "".join([
        stat_card("System", summary["mode"], _tone_for_value(summary["mode"]), "real writes blocked"),
        stat_card("Kill Switch", summary["kill_switch"], "down" if summary["kill_switch"] else "up", "must stay true before live"),
        stat_card("Market WS", summary["market_ws"], _tone_for_value(summary["market_ws"]), f"stale={market_health.get('stale')}"),
        stat_card("User WS", summary["user_ws"], _tone_for_value(summary["user_ws"]), f"stale={user_health.get('stale')}"),
        stat_card("Exposure", summary["exposure_usd"], "info", f"cap ${config.max_total_exposure_usd}"),
        stat_card("Daily PnL", daily.get("realized_pnl_usd", 0), "up", f"limit -${config.max_daily_realized_loss_usd}"),
    ])
    actions = """
    <div class="actions">
      <button data-live-action="/live/kill-switch/activate">Activate Kill Switch</button>
      <button data-live-action="/live/kill-switch/deactivate" data-reauth="true">Deactivate Kill Switch</button>
      <button data-live-action="/live/reconciliation/run">Run Reconciliation</button>
      <button data-live-action="/live/export/generate">Generate Export</button>
      <a class="button" href="/live/export/download">Download Export</a>
    </div>
    """
    overview = f"""
      <div class="panel" style="border:2px solid #b91c1c;color:#b91c1c;font-weight:800">READ-ONLY — REAL TRADING DISABLED</div>
      <div class="stats-grid">{stats}</div>
      {actions}
      <div class="two-col">
        {panel("Operations", kv_table([
            ("App Mode", summary["mode"], None),
            ("Adapter", config.live_adapter, None),
            ("Login Configured", model["auth"].configured(), None),
            ("Operator Token", bool(config.operator_token), None),
            ("Secret Provider", "Google Secret Manager" if config.google_project_id else "Environment", "info"),
            ("Last Reconciliation", summary["reconciliation"], None),
        ]))}
        {panel("Account", kv_table([
            ("Identity", repo.get_state("account_identity_status", "UNVERIFIED"), None),
            ("Profile", account.get("configured_profile_address") or config.profile_address or "not configured", None),
            ("Proxy Wallet", account.get("resolved_proxy_wallet") or "not resolved", None),
            ("Positions", account.get("public_positions_count", 0), None),
            ("Value", account.get("public_positions_value", 0), None),
        ]))}
      </div>
      {panel("Risk Gates", kv_table(model["risk_gates"]))}
      <div class="two-col">
        {panel("Latest Market", kv_table([
            ("Condition", latest_market.get("condition_id") if latest_market else "-", None),
            ("YES Token", latest_market.get("yes_token_id") if latest_market else "-", None),
            ("NO Token", latest_market.get("no_token_id") if latest_market else "-", None),
            ("$1 Valid", latest_market.get("one_dollar_valid") if latest_market else "-", None),
            ("Best Bid", latest_market.get("best_bid") if latest_market else "-", None),
            ("Best Ask", latest_market.get("best_ask") if latest_market else "-", None),
        ]))}
        {panel("Deployment Readiness", kv_table([
            ("DB Backup", "manual checklist", "warn"),
            ("Secrets", "not configured" if not config.google_project_id else "provider configured", None),
            ("Real Submission", "disabled", "up"),
            ("Live Rule Exists", counts.get("active_rules", 0), "up" if counts.get("active_rules", 0) == 0 else "warn"),
            ("Push Status", "local only", "warn"),
        ]))}
      </div>
    """
    screens = {
        "overview": overview,
        "operations": f"""
          <div class="two-col">
            {panel("Service Health", kv_table([
                ("System", summary["mode"], None),
                ("Market WS", market_health.get("status"), None),
                ("Market WS Last", market_health.get("last_message_at") or "never", None),
                ("User WS", user_health.get("status"), None),
                ("User WS Connected At", user_health.get("connected_at") or "never", None),
                ("User WS Last", user_health.get("last_message_at") or "never", None),
                ("User WS Last PONG", user_health.get("last_pong_at") or "never", None),
                ("User WS Reconnects", user_health.get("reconnect_count"), None),
                ("User WS Orders", user_health.get("order_events_received"), None),
                ("User WS Trades", user_health.get("trade_events_received"), None),
                ("User WS Markets", ", ".join(user_health.get("subscribed_condition_ids") or []) or "none", None),
                ("User WS Error", user_health.get("last_error") or "none", None),
                ("Reconciliation", summary["reconciliation"], None),
                ("Export", _export_state.get("status"), None),
            ]))}
            {panel("Runtime Config", kv_table([
                ("Trading Mode", config.trading_mode, None),
                ("Live Module", config.live_module_enabled, None),
                ("Live Trading", config.live_trading_enabled, None),
                ("Order Submission", config.live_order_submission_enabled, None),
                ("Adapter", config.live_adapter, None),
                ("Redemption", config.redemption_mode, None),
            ]))}
          </div>
          {panel("System State", compact_table(repo.list_table("live_daily_limits", 25)))}
        """,
        "risk": f"""
          <div class="stats-grid">
            {stat_card("Exposure", risk.current_exposure_usd(), "info", f"cap ${config.max_total_exposure_usd}")}
            {stat_card("Open Orders", counts["open_orders"], "info", f"cap {config.max_open_orders}")}
            {stat_card("Open Deals", counts["open_deals"], "info", f"cap {config.max_open_deals}")}
            {stat_card("Active Rules", counts.get("active_rules", 0), "warn" if counts.get("active_rules", 0) else "up", f"cap {config.max_active_rules}")}
          </div>
          {panel("Risk Gates", kv_table(model["risk_gates"]))}
          {panel("Daily Limits", compact_table(repo.list_table("live_daily_limits", 50)))}
        """,
        "logs": f"""
          {panel("Audit Log", compact_table(repo.list_table("live_audit_log", 200), ["id", "occurred_at", "actor", "action", "status", "reason"]))}
          {panel("Alert Center", compact_table([r for r in repo.list_table("live_audit_log", 200) if str(r.get("action", "")).startswith("alert:")], ["id", "occurred_at", "action", "status", "reason"], "No alerts"))}
          {panel("WebSocket Events", compact_table(repo.list_table("live_websocket_events", 100), ["id", "channel", "event_type", "condition_id", "asset_id", "status", "received_at"]))}
        """,
        "market": f"""
          {panel("Market Data", compact_table(repo.list_table("live_markets", 100), ["id", "condition_id", "yes_token_id", "no_token_id", "token_mapping_status", "min_order_size", "min_tick_size", "accepting_orders", "one_dollar_valid", "best_bid", "best_ask", "last_update_at"]))}
          {panel("WebSocket Health", kv_table([
              ("Market Status", market_health.get("status"), None),
              ("Market Last Message", market_health.get("last_message_at") or "never", None),
              ("Market Stale", market_health.get("stale"), None),
              ("Reconnect Attempts", market_health.get("reconnect_attempts"), None),
          ]))}
        """,
        "account": f"""
          {panel("Account Identity", compact_table(repo.list_table("live_account_snapshots", 50), ["id", "sampled_at", "configured_profile_address", "resolved_proxy_wallet", "expected_funder_candidate", "account_identity_status", "public_positions_count", "public_positions_value", "status", "error"]))}
          {panel("Positions", compact_table(repo.list_table("live_positions", 100), ["id", "condition_id", "token_id", "outcome", "size", "average_price", "status", "redeemable_at", "source"]))}
        """,
        "dry-run": f"""
          {panel("Dry Run Studio", '<form id="dry-run-form" class="dry-form"><input name="condition_id" placeholder="condition_id" value="mock-condition"><input name="token_id" placeholder="token_id" value="yes-token"><select name="purpose"><option value="entry">Entry</option><option value="take_profit">Take Profit</option><option value="stop_loss">Stop Loss</option><option value="manual_exit">Manual Exit</option></select><select name="side"><option value="buy">Buy</option><option value="sell">Sell</option></select><input name="requested_price" placeholder="price" value="0.50"><input name="requested_amount_usd" placeholder="amount" value="1"><button type="submit">Preview Dry Run</button></form><pre id="dry-run-result" class="json-box">No preview yet.</pre>')}
          {panel("Dry Run History", compact_table(repo.list_table("live_dry_runs", 100), ["id", "created_at", "actor", "final_decision", "reason_codes_json"]))}
        """,
        "reconciliation": f"""
          {actions}
          {panel("User WebSocket", kv_table([
              ("Status", user_health.get("status"), None),
              ("Connected At", user_health.get("connected_at") or "never", None),
              ("Last Message", user_health.get("last_message_at") or "never", None),
              ("Last PONG", user_health.get("last_pong_at") or "never", None),
              ("Reconnects", user_health.get("reconnect_count"), None),
              ("Order Events", user_health.get("order_events_received"), None),
              ("Trade Events", user_health.get("trade_events_received"), None),
              ("Markets", ", ".join(user_health.get("subscribed_condition_ids") or []) or "none", None),
              ("Error", user_health.get("last_error") or "none", None),
          ]))}
          {panel("Reconciliation Runs", compact_table(repo.list_table("live_reconciliation_runs", 100), ["id", "started_at", "finished_at", "status", "gaps_count", "error"]))}
        """,
        "orders": f"""
          {panel("Orders", compact_table(repo.list_table("live_orders", 200), ["local_order_id", "idempotency_key", "polymarket_order_id", "purpose", "side", "order_type", "requested_amount_usd", "filled_size", "remaining_size", "status", "failure_reason", "created_at"]))}
          {panel("Fills", compact_table(repo.list_table("live_order_fills", 200), ["id", "live_order_id", "polymarket_trade_id", "price", "size", "fee", "status", "matched_at"]))}
          {panel("Deals / Positions", compact_table(repo.list_table("live_deals", 100), ["id", "live_rule_id", "condition_id", "outcome", "side", "status", "requested_amount_usd", "filled_size", "remaining_size", "realized_pnl_usd", "exit_reason"]))}
        """,
        "deployment": f"""
          {panel("Deployment Checklist", kv_table([
              ("SQLite backup command", 'sqlite3 poly_data.sqlite3 ".backup \'poly_data.backup.sqlite3\'"', None),
              ("Mock-only flags", "LIVE_MODULE_ENABLED=true / LIVE_ADAPTER=mock / LIVE_KILL_SWITCH=true", "info"),
              ("Real trading flags", "false", "up"),
              ("Secrets in Git", "prohibited", "down"),
              ("Deployment Performed", "no", "up"),
              ("Rollback", "set LIVE_MODULE_ENABLED=false and restart", "warn"),
          ]))}
          {panel("Outstanding Questions", '<p class="muted">See <code>POLYMARKET_LIVE_PRODUCT_QUESTIONS.md</code> in the workspace.</p>')}
        """,
        "maintenance": f"""
          {panel("Safe Maintenance", kv_table([
              ("Mode", repo.maintenance_status().get("mode"), None),
              ("Phase", repo.maintenance_status().get("phase"), None),
              ("Stop Ready", repo.maintenance_status().get("stop_ready"), None),
              ("Exposure", repo.maintenance_status().get("exposure_usd"), None),
              ("Open Orders", repo.maintenance_status().get("open_orders"), None),
              ("Open Deals", repo.maintenance_status().get("open_deals"), None),
              ("Delay Reason", repo.maintenance_status().get("delay_reason"), None),
              ("Estimated Wait", repo.maintenance_status().get("estimated_wait"), None),
          ]))}
          <div class="actions">
            <button data-live-action="/live/maintenance/drain">Drain After Current Event</button>
            <button data-live-action="/live/maintenance/cancel">Cancel Drain</button>
            <button data-live-action="/live/maintenance/readiness">Refresh Readiness</button>
            <button data-live-action="/live/backup/create">Create SQLite Backup</button>
            <button data-live-action="/live/sessions/revoke-all" data-reauth="true">Revoke All Sessions</button>
          </div>
          {panel("Backups", compact_table(repo.list_table("live_backups", 50), ["id", "created_at", "path", "size_bytes", "sha256", "status", "reason", "error"], "No backups recorded"))}
        """,
    }
    return screens.get(view, overview)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def live_dashboard(request: Request) -> str:
    require_live_session(request)
    view = request.query_params.get("view", "overview")
    nav_items = [
        ("overview", "Overview"),
        ("operations", "Operations"),
        ("risk", "Risk"),
        ("logs", "Logs"),
        ("market", "Market Data"),
        ("account", "Account"),
        ("dry-run", "Dry Run"),
        ("reconciliation", "Reconciliation"),
        ("orders", "Orders"),
        ("deployment", "Deployment"),
        ("maintenance", "Maintenance"),
    ]
    nav = "".join(
        f'<a class="nav-item {"active" if view == key else ""}" href="/live?view={_e(key)}">{_e(label)}</a>'
        for key, label in nav_items
    )
    _config, _repo, *_rest, auth, _dry = services()
    csrf_token = auth.csrf_token(request.cookies.get(COOKIE_NAME))
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <title>Polymarket LIVE</title>
      <style>
        :root {{ --bg:#151923; --panel:#1b202b; --panel2:#202737; --border:#303747; --muted:#9aa4b2; --fg:#f3f6fa; --up:#66d38e; --down:#ff6b6b; --warn:#f6c85f; --info:#78a6ff; --accent:#2a3243; }}
        * {{ box-sizing: border-box; }}
        body {{ margin:0; background:var(--bg); color:var(--fg); font-family: Inter, Arial, sans-serif; font-feature-settings:"cv02","cv03","cv04","cv11"; }}
        code, .mono, table, .stat-value, .json-box {{ font-family:"JetBrains Mono", Consolas, monospace; font-variant-numeric: tabular-nums; }}
        .app {{ min-height:100vh; display:flex; }}
        .sidebar {{ width:245px; flex-shrink:0; border-right:1px solid var(--border); background:var(--panel); display:flex; flex-direction:column; }}
        .brand {{ height:58px; display:flex; align-items:center; gap:10px; padding:0 14px; border-bottom:1px solid var(--border); }}
        .logo {{ width:32px; height:32px; border-radius:8px; display:grid; place-items:center; color:var(--info); background:rgba(120,166,255,.12); border:1px solid rgba(120,166,255,.35); }}
        .brand-title {{ font-size:14px; font-weight:700; }}
        .brand-sub {{ font-size:10px; color:var(--muted); letter-spacing:.12em; text-transform:uppercase; }}
        .nav {{ padding:10px 8px; display:flex; flex-direction:column; gap:3px; }}
        .nav-label {{ padding:10px 8px 4px; color:var(--muted); font-size:10px; letter-spacing:.12em; text-transform:uppercase; }}
        .nav-item {{ color:var(--muted); text-decoration:none; padding:10px 10px; min-height:44px; display:flex; align-items:center; border-radius:8px; border:1px solid transparent; font-size:13px; }}
        .nav-item:hover {{ color:var(--fg); background:var(--accent); }}
        .nav-item.active {{ color:var(--fg); border-color:rgba(120,166,255,.35); background:rgba(120,166,255,.10); }}
        .main {{ flex:1; min-width:0; display:flex; flex-direction:column; }}
        .topbar {{ height:58px; display:flex; align-items:center; gap:10px; padding:0 16px; border-bottom:1px solid var(--border); background:rgba(27,32,43,.76); backdrop-filter:blur(10px); }}
        .search {{ width:330px; max-width:42vw; height:34px; border:1px solid var(--border); background:#111721; color:var(--fg); border-radius:8px; padding:0 10px; }}
        .topbar form {{ margin:0; }}
        .top-status {{ margin-left:auto; display:flex; align-items:center; gap:14px; color:var(--muted); font-size:12px; }}
        .dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; background:var(--up); box-shadow:0 0 14px var(--up); }}
        .content {{ padding:18px; overflow:auto; }}
        .page-head {{ display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:14px; }}
        h1 {{ margin:0; font-size:22px; letter-spacing:0; }}
        .subtitle {{ color:var(--muted); margin-top:4px; font-size:13px; }}
        .stats-grid {{ display:grid; grid-template-columns:repeat(6,minmax(130px,1fr)); gap:10px; margin-bottom:14px; }}
        .stat-card {{ border:1px solid var(--border); background:var(--panel); border-radius:8px; padding:12px; min-height:78px; }}
        .stat-label {{ color:var(--muted); text-transform:uppercase; letter-spacing:.12em; font-size:10px; }}
        .stat-value {{ font-size:22px; font-weight:700; margin-top:4px; overflow-wrap:anywhere; }}
        .stat-hint {{ color:var(--muted); font-size:11px; margin-top:2px; min-height:14px; }}
        .up {{ color:var(--up); }} .down {{ color:var(--down); }} .warn {{ color:var(--warn); }} .info {{ color:var(--info); }} .neutral {{ color:var(--fg); }}
        .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 14px; }}
        button, .button {{ border:1px solid var(--border); background:#111721; color:var(--fg); min-height:44px; padding:10px 12px; border-radius:8px; text-decoration:none; font:inherit; font-size:12px; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; }}
        button:hover, .button:hover {{ background:var(--accent); }}
        .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }}
        .panel {{ border:1px solid var(--border); background:var(--panel); border-radius:8px; margin-bottom:14px; overflow:hidden; }}
        .panel-head {{ height:42px; padding:0 13px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; background:var(--panel2); }}
        .panel-title {{ color:var(--muted); text-transform:uppercase; letter-spacing:.10em; font-size:11px; font-weight:700; }}
        .panel-body {{ padding:12px; }}
        .table-wrap {{ overflow:auto; max-height:520px; border:1px solid var(--border); border-radius:6px; }}
        table {{ width:100%; min-width:980px; border-collapse:collapse; font-size:12px; }}
        th, td {{ border-bottom:1px solid var(--border); padding:7px 9px; white-space:nowrap; text-align:left; vertical-align:top; }}
        thead th {{ position:sticky; top:0; z-index:1; color:var(--muted); background:var(--panel2); text-transform:uppercase; letter-spacing:.08em; font-size:10px; font-weight:600; }}
        .kv-table {{ min-width:0; }}
        .kv-table th {{ width:230px; color:var(--muted); background:transparent; text-transform:none; letter-spacing:0; font-size:12px; }}
        .chip {{ display:inline-flex; align-items:center; border:1px solid var(--border); border-radius:5px; padding:2px 6px; font-size:10px; text-transform:uppercase; letter-spacing:.06em; font-weight:700; }}
        .chip.up {{ color:var(--up); border-color:rgba(102,211,142,.35); background:rgba(102,211,142,.08); }}
        .chip.down {{ color:var(--down); border-color:rgba(255,107,107,.35); background:rgba(255,107,107,.08); }}
        .chip.warn {{ color:var(--warn); border-color:rgba(246,200,95,.35); background:rgba(246,200,95,.08); }}
        .chip.info {{ color:var(--info); border-color:rgba(120,166,255,.35); background:rgba(120,166,255,.08); }}
        .empty, .muted {{ color:var(--muted); }}
        .dry-form {{ display:grid; grid-template-columns:repeat(7, minmax(120px,1fr)); gap:8px; align-items:center; }}
        .dry-form input, .dry-form select {{ min-height:44px; border:1px solid var(--border); background:#111721; color:var(--fg); border-radius:8px; padding:0 10px; font-size:16px; }}
        .json-box {{ margin-top:10px; background:#111721; border:1px solid var(--border); border-radius:8px; padding:10px; color:var(--muted); overflow:auto; max-height:260px; }}
        @media (max-width: 1100px) {{ .sidebar {{ width:210px; }} .stats-grid {{ grid-template-columns:repeat(2,1fr); }} .two-col {{ grid-template-columns:1fr; }} .dry-form {{ grid-template-columns:1fr 1fr; }} }}
        @media (max-width: 760px) {{
          .app {{ display:block; min-height:100vh; }}
          .sidebar {{ position:sticky; top:0; z-index:5; width:100%; border-right:0; border-bottom:1px solid var(--border); }}
          .brand {{ height:54px; padding:0 12px; }}
          .logo {{ width:30px; height:30px; }}
          .brand-title {{ font-size:13px; }}
          .nav {{ padding:8px 10px; flex-direction:row; gap:8px; overflow-x:auto; scroll-snap-type:x proximity; -webkit-overflow-scrolling:touch; }}
          .nav-label {{ display:none; }}
          .nav-item {{ flex:0 0 auto; min-height:44px; padding:9px 12px; scroll-snap-align:start; white-space:nowrap; }}
          .sidebar > .nav:last-child {{ display:none; }}
          .main {{ display:block; }}
          .topbar {{ position:sticky; top:109px; z-index:4; height:auto; min-height:56px; padding:8px 10px; flex-wrap:wrap; align-items:stretch; }}
          .search {{ order:1; width:100%; max-width:none; height:44px; font-size:16px; }}
          .topbar .button, .topbar form, .topbar button {{ flex:1 1 112px; }}
          .top-status {{ order:4; width:100%; margin-left:0; justify-content:space-between; gap:8px; }}
          .content {{ padding:12px 10px 18px; }}
          .page-head {{ align-items:flex-start; gap:10px; margin-bottom:12px; }}
          h1 {{ font-size:20px; }}
          .subtitle {{ font-size:12px; max-width:280px; }}
          .stats-grid {{ grid-template-columns:1fr; gap:8px; }}
          .stat-card {{ min-height:72px; padding:11px; }}
          .stat-value {{ font-size:20px; }}
          .actions {{ display:grid; grid-template-columns:1fr; gap:8px; }}
          .two-col {{ gap:10px; margin-bottom:10px; }}
          .panel {{ margin-bottom:10px; border-radius:8px; }}
          .panel-head {{ min-height:40px; height:auto; padding:9px 11px; }}
          .panel-body {{ padding:10px; }}
          .table-wrap {{ max-height:none; overflow:visible; border:0; }}
          table:not(.kv-table) {{ min-width:0; display:block; font-size:12px; }}
          table:not(.kv-table) thead {{ display:none; }}
          table:not(.kv-table) tbody {{ display:grid; gap:10px; }}
          table:not(.kv-table) tr {{ display:block; border:1px solid var(--border); border-radius:8px; background:#111721; overflow:hidden; }}
          table:not(.kv-table) td {{ display:grid; grid-template-columns:minmax(104px, 38%) 1fr; gap:10px; min-height:36px; padding:8px 10px; white-space:normal; overflow-wrap:anywhere; }}
          table:not(.kv-table) td::before {{ content:attr(data-label); color:var(--muted); text-transform:uppercase; letter-spacing:.08em; font-size:10px; font-weight:700; }}
          .kv-table {{ min-width:0; display:table; table-layout:fixed; }}
          .kv-table th, .kv-table td {{ white-space:normal; overflow-wrap:anywhere; padding:8px 6px; }}
          .kv-table th {{ width:42%; }}
          .dry-form {{ grid-template-columns:1fr; gap:8px; }}
          .json-box {{ max-height:360px; white-space:pre-wrap; overflow-wrap:anywhere; }}
        }}
      </style>
    </head>
    <body>
      <div class="app">
        <aside class="sidebar">
          <div class="brand"><div class="logo">PM</div><div><div class="brand-title">Polymarket LIVE</div><div class="brand-sub">Control Center</div></div></div>
          <nav class="nav"><div class="nav-label">Workspace</div>{nav}</nav>
          <div class="nav-label">Safety</div>
          <div class="nav"><span class="nav-item">Real submission: disabled</span><span class="nav-item">Rules: not seeded</span></div>
        </aside>
        <div class="main">
          <header class="topbar">
            <input class="search" placeholder="Search order, condition, token, reason..." />
            <a class="button" href="/live?view=overview">Refresh</a>
            <button type="button" id="logout-button">Logout</button>
            <div class="top-status"><span><span class="dot"></span> Read-only shell</span><span class="mono">{_e(now_iso())}</span></div>
          </header>
          <main class="content">
            <div class="page-head"><div><h1>{_e(dict(nav_items).get(view, "Overview"))}</h1><div class="subtitle">Operational dashboard inspired by Market Event Radar · real writes remain blocked</div></div>{chip(dashboard_model()["mode"])}</div>
            {dashboard_content(view)}
          </main>
        </div>
      </div>
      <script>
        const liveCsrfToken = "{_e(csrf_token)}";
        async function postLiveAction(path) {{
          const token = window.prompt("LIVE operator token");
          if (!token) return;
          const headers = {{"X-Live-Operator-Token": token, "X-Live-CSRF-Token": liveCsrfToken}};
          const trigger = document.querySelector(`[data-live-action="${{path}}"]`);
          if (trigger && trigger.getAttribute("data-reauth") === "true") {{
            const password = window.prompt("Re-enter admin password");
            if (!password) return;
            headers["X-Live-Reauth-Password"] = password;
          }}
          const response = await fetch(path, {{method: "POST", headers}});
          if (!response.ok) {{
            const text = await response.text();
            window.alert(text);
            return;
          }}
          window.location.reload();
        }}
        const dryRunForm = document.getElementById("dry-run-form");
        if (dryRunForm) {{
          dryRunForm.addEventListener("submit", async (event) => {{
            event.preventDefault();
            const token = window.prompt("LIVE operator token");
            if (!token) return;
            const form = new FormData(dryRunForm);
            const payload = Object.fromEntries(form.entries());
            payload.requested_price = Number(payload.requested_price);
            payload.requested_amount_usd = Number(payload.requested_amount_usd);
            const response = await fetch("/live/dry-run", {{method:"POST", headers:{{"Content-Type":"application/json","X-Live-Operator-Token":token,"X-Live-CSRF-Token":liveCsrfToken}}, body:JSON.stringify(payload)}});
            document.getElementById("dry-run-result").textContent = JSON.stringify(await response.json(), null, 2);
          }});
        }}
        const logoutButton = document.getElementById("logout-button");
        if (logoutButton) {{
          logoutButton.addEventListener("click", async () => {{
            const response = await fetch("/live/logout", {{method:"POST", headers:{{"X-Live-CSRF-Token":liveCsrfToken}}}});
            if (response.ok) location.href = "/live/login";
            else window.alert(await response.text());
          }});
        }}
        document.querySelectorAll("[data-live-action]").forEach((button) => {{
          button.addEventListener("click", () => postLiveAction(button.getAttribute("data-live-action")));
        }});
      </script>
    </body>
    </html>
    """


@router.get("/content", response_class=HTMLResponse)
def live_content(request: Request) -> str:
    require_live_session(request)
    return dashboard_content(request.query_params.get("view", "overview"))


@router.get("/health")
def live_health(request: Request) -> dict[str, Any]:
    require_live_session(request)
    config, repo, adapter, _risk, _orders, _recon, market_ws, user_ws, _engine, auth, _dry = services()
    return {
        "ok": True,
        "time": now_iso(),
        "mode": status_label(config, repo, market_ws),
        "config": config.safe_public_dict(),
        "adapter": adapter.name,
        "kill_switch_active": repo.kill_switch_active(),
        "counts": repo.counts(),
        "market_ws": market_ws.health(),
        "user_ws": user_ws.health(),
        "auth": auth.public_status(),
        "account_identity": repo.latest_account_snapshot(),
        "validation_errors": config.validation_errors(),
    }


@router.get("/markets")
def live_markets(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_markets", 100)


@router.get("/orders")
def live_orders(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_orders", 100)


@router.get("/fills")
def live_fills(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_order_fills", 100)


@router.get("/deals")
def live_deals(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_deals", 100)


@router.get("/rules")
def live_rules(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_rules", 100)


@router.get("/reconciliation")
def live_reconciliation(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_reconciliation_runs", 100)


@router.get("/audit")
def live_audit(request: Request) -> list[dict[str, Any]]:
    require_live_session(request)
    return services()[1].list_table("live_audit_log", 100)


@router.post("/kill-switch/activate")
def activate_kill_switch(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    services()[1].set_state("kill_switch", "true", "operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/kill-switch/deactivate")
def deactivate_kill_switch(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None), x_live_reauth_password: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    require_reauth(request, x_live_reauth_password)
    services()[1].set_state("kill_switch", "false", "operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/reconciliation/run")
async def run_reconciliation(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    await services()[5].run_once(actor="operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/rules")
def create_live_rule(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None), x_live_reauth_password: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    if str(payload.get("status") or "inactive").lower() == "active":
        require_reauth(request, x_live_reauth_password)
    repo = services()[1]
    try:
        entry = float(payload.get("entry_price"))
        sl = float(payload.get("stop_loss_price"))
        tp = float(payload.get("take_profit_price"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Prices must be numeric")
    if entry == 0.5:
        raise HTTPException(status_code=400, detail="entry_price 0.5 is not allowed")
    if sl >= entry or tp <= entry:
        raise HTTPException(status_code=400, detail="SL must be below entry and TP above entry")
    rule = repo.create_rule({
        "name": str(payload.get("name") or "").strip() or "live rule",
        "entry_price": entry,
        "stop_loss_price": sl,
        "take_profit_price": tp,
        "requested_amount_usd": float(payload.get("requested_amount_usd", 1)),
        "entry_order_type": str(payload.get("entry_order_type") or "FOK").upper(),
        "max_entry_slippage": float(payload.get("max_entry_slippage", 0.01)),
        "max_exit_slippage": float(payload.get("max_exit_slippage", 0.02)),
        "status": str(payload.get("status") or "inactive"),
    })
    return rule


@router.post("/orders/mock")
async def create_mock_order(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    config, repo, _adapter, _risk, _orders, reconciliation, _market_ws, _user_ws, engine, _auth, _dry = services()
    if config.live_adapter != "mock":
        raise HTTPException(status_code=403, detail="This endpoint is mock-only")
    await reconciliation.run_once(actor="operator")
    return await engine.entry_intent({
        "idempotency_key": payload.get("idempotency_key") or f"manual-{now_iso()}",
        "live_rule_id": payload.get("live_rule_id"),
        "event_id": payload.get("event_id", "manual-mock-event"),
        "condition_id": payload.get("condition_id", "mock-condition"),
        "token_id": payload.get("token_id", "yes-token"),
        "outcome": payload.get("outcome", "Yes"),
        "requested_price": payload.get("requested_price", 0.51),
        "requested_amount_usd": payload.get("requested_amount_usd", config.default_trade_amount_usd),
        "order_type": payload.get("order_type", config.entry_order_type),
        "mock_scenario": payload.get("mock_scenario"),
    }, actor="operator")


@router.post("/market-ws/fixture")
def process_market_ws_fixture(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    stored = services()[6].process_message(payload)
    return {"stored": stored, "status": services()[6].health()}


@router.post("/user-ws/fixture")
def process_user_ws_fixture(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    stored = services()[7].process_message(payload)
    return {"stored": stored, "status": services()[7].health()}


def write_live_export() -> tuple[Path, dict[str, int]]:
    _config, repo, *_ = services()
    output_dir = repo.db_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"polymarket_live_data_{now_iso().replace(':', '').replace('+', '_')}.xlsx"
    workbook = Workbook(write_only=True)
    tables = [
        "live_markets", "live_rules", "live_deals", "live_orders", "live_order_fills",
        "live_positions", "live_account_snapshots", "live_reconciliation_runs", "live_audit_log",
        "live_websocket_events", "live_dry_runs", "live_daily_limits",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        rows = repo.list_table(table, 100000)
        sheet = workbook.create_sheet(table)
        headers = list(rows[0].keys()) if rows else ["empty"]
        sheet.append(headers)
        count = 0
        for row in rows:
            safe_row = redact_mapping(row)
            sheet.append([safe_row.get(header) for header in headers])
            count += 1
        counts[table] = count
    workbook.save(path)
    return path, counts


def _run_export() -> None:
    try:
        path, counts = write_live_export()
        _export_state.update({"status": "ready", "path": str(path), "error": None, "row_counts": counts})
    except Exception as exc:
        _export_state.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})


@router.post("/export/generate")
def generate_live_export(request: Request, background_tasks: BackgroundTasks, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    _export_state.update({"status": "running", "error": None})
    background_tasks.add_task(_run_export)
    return RedirectResponse("/live", status_code=303)


@router.get("/export/download")
def download_live_export(request: Request):
    require_live_session(request)
    path = _export_state.get("path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="No LIVE export is ready")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=Path(path).name)


@router.post("/account/public-refresh")
async def refresh_account_identity(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None), use_mock: bool = False) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    config, repo, *_ = services()
    client = MockPublicAccountIdentityClient() if use_mock else PublicAccountIdentityClient()
    result = await client.resolve(config.profile_address)
    payload = result.__dict__
    payload["sampled_at"] = result.refreshed_at or now_iso()
    payload["account_login_type"] = config.account_login_type
    payload["account_identity_status"] = result.status
    repo.store_account_snapshot(payload)
    repo.audit("operator", "public_account_identity_refresh", "ok" if result.status != "UNAVAILABLE" else "blocked", result.error, {"status": result.status})
    return {k: v for k, v in payload.items() if k != "raw_public_payload"}


@router.post("/dry-run")
def create_dry_run(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    return services()[-1].preview(payload, actor="operator")


@router.post("/market-ws/smoke")
async def market_ws_smoke(request: Request, payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    config, _repo, _adapter, _risk, _orders, _recon, market_ws, _user_ws, _engine, _auth, _dry = services()
    asset_ids = [str(item) for item in payload.get("asset_ids") or []]
    if not asset_ids:
        raise HTTPException(status_code=400, detail="asset_ids are required for bounded public Market WS smoke test")
    return await market_ws.connect_for_messages(config.market_ws_url, asset_ids, max_messages=int(payload.get("max_messages", 1)), timeout_seconds=float(payload.get("timeout_seconds", 20)))


@router.get("/maintenance/status")
def maintenance_status(request: Request) -> dict[str, Any]:
    require_live_session(request)
    return services()[1].maintenance_status()


@router.post("/maintenance/drain")
def maintenance_drain(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> JSONResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    return JSONResponse(services()[1].request_maintenance_drain("operator"))


@router.post("/maintenance/cancel")
def maintenance_cancel(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> JSONResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    return JSONResponse(services()[1].cancel_maintenance_drain("operator"))


@router.post("/maintenance/readiness")
async def maintenance_readiness(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> JSONResponse:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    await services()[5].run_once(actor="maintenance")
    return JSONResponse(services()[1].refresh_maintenance_readiness("operator"))


@router.post("/backup/create")
def create_live_backup(request: Request, x_live_operator_token: Optional[str] = Header(default=None), x_live_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_live_session(request)
    require_operator(request, x_live_operator_token, x_live_csrf_token)
    config, repo, *_ = services()
    result = LiveBackupManager(config, repo).create_backup("manual")
    return result.__dict__


@router.get("/secrets/readiness")
def secrets_status(request: Request) -> dict[str, Any]:
    require_live_session(request)
    config = services()[0]
    provider = GoogleSecretManagerProvider(config.google_project_id, config.google_secret_prefix) if config.google_project_id else EnvSecretProvider()
    return secret_readiness(provider)


async def refresh_public_market_metadata(condition_id: str, event_id: str | None = None, yes_token_id: str | None = None, no_token_id: str | None = None, use_mock: bool = True) -> dict[str, Any]:
    config, repo, *_ = services()
    client = MockPublicClobClient() if use_mock else PublicClobClient(config.clob_host)
    metadata = await client.build_metadata(
        condition_id=condition_id,
        event_id=event_id,
        gamma_yes_token_id=yes_token_id,
        gamma_no_token_id=no_token_id,
    )
    repo.upsert_market(metadata)
    repo.audit("system", "refresh_public_market_metadata", "ok", details={"condition_id": condition_id, "source": metadata.get("source")})
    return metadata
