from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from openpyxl import Workbook

from .adapters import MockTradingAdapter, RealPolymarketTradingAdapter, TradingAdapter
from .config import LiveConfig, redact_mapping
from .market_websocket import MarketWebSocketManager, UserWebSocketManager
from .order_manager import OrderManager
from .public_client import MockPublicClobClient, PublicClobClient
from .reconciliation import ReconciliationWorker
from .repository import LiveRepository, now_iso
from .risk_manager import RiskManager
from .trading_engine import TradingEngine


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
_export_state: dict[str, Any] = {"status": "idle", "path": None, "error": None, "row_counts": None}


def configure(db_path: Path | str, config: LiveConfig | None = None) -> None:
    global _repo, _config, _adapter, _risk, _orders, _reconciliation, _market_ws, _user_ws, _engine
    _config = config or LiveConfig.from_env()
    _repo = LiveRepository(db_path)
    _repo.migrate(_config.live_kill_switch_default)
    _adapter = MockTradingAdapter(_config.adapter_scenario) if _config.live_adapter == "mock" else RealPolymarketTradingAdapter(_config)
    _risk = RiskManager(_config, _repo)
    _orders = OrderManager(_repo, _risk, _adapter)
    _reconciliation = ReconciliationWorker(_repo, _adapter)
    _market_ws = MarketWebSocketManager(_repo, stale_after_seconds=_config.max_market_data_age_seconds)
    _user_ws = UserWebSocketManager(_repo)
    _engine = TradingEngine(_repo, _orders)


def services() -> tuple[LiveConfig, LiveRepository, TradingAdapter, RiskManager, OrderManager, ReconciliationWorker, MarketWebSocketManager, UserWebSocketManager, TradingEngine]:
    if _repo is None or _config is None or _adapter is None or _risk is None or _orders is None or _reconciliation is None or _market_ws is None or _user_ws is None or _engine is None:
        raise RuntimeError("LIVE services are not configured")
    return _config, _repo, _adapter, _risk, _orders, _reconciliation, _market_ws, _user_ws, _engine


def require_operator(x_live_operator_token: Optional[str] = Header(default=None)) -> None:
    config, repo, *_ = services()
    if not config.operator_token:
        repo.audit("anonymous", "operator_auth", "blocked", "LIVE_OPERATOR_TOKEN is not configured")
        raise HTTPException(status_code=403, detail="LIVE operator token is not configured; write actions are blocked")
    if x_live_operator_token != config.operator_token:
        repo.audit("anonymous", "operator_auth", "blocked", "Invalid operator token")
        raise HTTPException(status_code=403, detail="Invalid LIVE operator token")


def render_table(rows: list[dict[str, Any]], empty: str = "No rows") -> str:
    if not rows:
        return f"<p class=\"muted\">{html.escape(empty)}</p>"
    headers = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(key))}</th>" for key in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{html.escape('' if row.get(key) is None else str(row.get(key)))}</td>" for key in headers) + "</tr>"
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


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


def dashboard_content() -> str:
    config, repo, _adapter, _risk, _orders, _recon, market_ws, user_ws, _engine = services()
    counts = repo.counts()
    latest_market = repo.latest_market()
    summary = {
        "mode": status_label(config, repo, market_ws),
        "adapter": config.live_adapter,
        "kill_switch": repo.kill_switch_active(),
        "market_ws": market_ws.health()["status"],
        "user_ws": user_ws.health()["status"],
        "reconciliation": repo.get_state("last_reconciliation_at", "never"),
        "active_market": latest_market.get("condition_id") if latest_market else "-",
        "one_dollar_valid": latest_market.get("one_dollar_valid") if latest_market else "-",
        "open_orders": counts["open_orders"],
        "open_deals": counts["open_deals"],
    }
    return f"""
      <section class="header">
        <div>
          <h1>POLYMARKET LIVE</h1>
          <p class="warning">This isolated area is designed for future real-money trading. Real order submission is blocked by default in this build.</p>
        </div>
        <div class="mode">{html.escape(str(summary["mode"]))}</div>
      </section>
      <section class="cards">
        {''.join(f'<div class="metric"><div class="metric-label">{html.escape(str(k))}</div><div class="metric-value">{html.escape(str(v))}</div></div>' for k, v in summary.items())}
      </section>
      <section class="actions">
        <button data-live-action="/live/kill-switch/activate">Activate Kill Switch</button>
        <button data-live-action="/live/kill-switch/deactivate">Deactivate Kill Switch</button>
        <button data-live-action="/live/reconciliation/run">Run Reconciliation</button>
        <button data-live-action="/live/export/generate">Generate LIVE Export</button>
        <a class="button" href="/live/export/download">Download LIVE Export</a>
      </section>
      <section class="grid">
        <div class="card"><h2>Live Markets</h2>{render_table(repo.list_table("live_markets", 25))}</div>
        <div class="card"><h2>Live Rules</h2>{render_table(repo.list_table("live_rules", 25))}</div>
        <div class="card"><h2>Live Deals / Positions</h2>{render_table(repo.list_table("live_deals", 25))}</div>
        <div class="card"><h2>Live Orders</h2>{render_table(repo.list_table("live_orders", 50))}</div>
        <div class="card"><h2>Order Fills</h2>{render_table(repo.list_table("live_order_fills", 50))}</div>
        <div class="card"><h2>Reconciliation</h2>{render_table(repo.list_table("live_reconciliation_runs", 25))}</div>
        <div class="card"><h2>Audit Log</h2>{render_table(repo.list_table("live_audit_log", 50))}</div>
        <div class="card"><h2>WebSocket Events</h2>{render_table(repo.list_table("live_websocket_events", 50))}</div>
      </section>
    """


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def live_dashboard() -> str:
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Polymarket LIVE</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 24px; background: #f6f7f9; color: #111; }}
        .header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 3px solid #111; padding-bottom: 12px; }}
        h1 {{ margin: 0; letter-spacing: 0; }}
        h2 {{ margin: 0 0 10px 0; }}
        .warning {{ font-weight: 700; color: #8a1f11; max-width: 920px; }}
        .mode {{ padding: 10px 14px; background: #111; color: white; font-weight: 700; }}
        .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 18px 0; }}
        .metric {{ border: 1px solid #d8dde6; background: white; padding: 12px; border-radius: 6px; min-height: 72px; }}
        .metric-label {{ color: #5b6472; font-size: 12px; }}
        .metric-value {{ font-size: 18px; font-weight: 700; overflow-wrap: anywhere; }}
        .actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 18px 0; }}
        button, .button {{ border: 0; background: #243b53; color: white; padding: 9px 12px; border-radius: 6px; text-decoration: none; font: inherit; cursor: pointer; }}
        .grid {{ display: grid; grid-template-columns: 1fr; gap: 16px; }}
        .card {{ background: white; border: 1px solid #d8dde6; border-radius: 6px; padding: 14px; }}
        .table-wrap {{ overflow-x: auto; max-height: 420px; border: 1px solid #e2e6ee; }}
        table {{ border-collapse: collapse; width: 100%; min-width: 980px; font-size: 12px; }}
        th, td {{ border: 1px solid #e2e6ee; padding: 6px; white-space: nowrap; vertical-align: top; }}
        th {{ background: #eef2f7; position: sticky; top: 0; }}
        .muted {{ color: #667085; }}
      </style>
    </head>
    <body>
      {dashboard_content()}
      <script>
        async function postLiveAction(path) {{
          const token = window.prompt("LIVE operator token");
          if (!token) return;
          const response = await fetch(path, {{method: "POST", headers: {{"X-Live-Operator-Token": token}}}});
          if (!response.ok) {{
            const text = await response.text();
            window.alert(text);
            return;
          }}
          window.location.reload();
        }}
        document.querySelectorAll("[data-live-action]").forEach((button) => {{
          button.addEventListener("click", () => postLiveAction(button.getAttribute("data-live-action")));
        }});
      </script>
    </body>
    </html>
    """


@router.get("/content", response_class=HTMLResponse)
def live_content() -> str:
    return dashboard_content()


@router.get("/health")
def live_health() -> dict[str, Any]:
    config, repo, adapter, _risk, _orders, _recon, market_ws, user_ws, _engine = services()
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
        "validation_errors": config.validation_errors(),
    }


@router.get("/markets")
def live_markets() -> list[dict[str, Any]]:
    return services()[1].list_table("live_markets", 100)


@router.get("/orders")
def live_orders() -> list[dict[str, Any]]:
    return services()[1].list_table("live_orders", 100)


@router.get("/fills")
def live_fills() -> list[dict[str, Any]]:
    return services()[1].list_table("live_order_fills", 100)


@router.get("/deals")
def live_deals() -> list[dict[str, Any]]:
    return services()[1].list_table("live_deals", 100)


@router.get("/rules")
def live_rules() -> list[dict[str, Any]]:
    return services()[1].list_table("live_rules", 100)


@router.get("/reconciliation")
def live_reconciliation() -> list[dict[str, Any]]:
    return services()[1].list_table("live_reconciliation_runs", 100)


@router.get("/audit")
def live_audit() -> list[dict[str, Any]]:
    return services()[1].list_table("live_audit_log", 100)


@router.post("/kill-switch/activate")
def activate_kill_switch(x_live_operator_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_operator(x_live_operator_token)
    services()[1].set_state("kill_switch", "true", "operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/kill-switch/deactivate")
def deactivate_kill_switch(x_live_operator_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_operator(x_live_operator_token)
    services()[1].set_state("kill_switch", "false", "operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/reconciliation/run")
async def run_reconciliation(x_live_operator_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_operator(x_live_operator_token)
    await services()[5].run_once(actor="operator")
    return RedirectResponse("/live", status_code=303)


@router.post("/rules")
def create_live_rule(payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_operator(x_live_operator_token)
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
async def create_mock_order(payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_operator(x_live_operator_token)
    config, repo, _adapter, _risk, _orders, reconciliation, _market_ws, _user_ws, engine = services()
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
def process_market_ws_fixture(payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_operator(x_live_operator_token)
    stored = services()[6].process_message(payload)
    return {"stored": stored, "status": services()[6].health()}


@router.post("/user-ws/fixture")
def process_user_ws_fixture(payload: dict[str, Any] = Body(...), x_live_operator_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    require_operator(x_live_operator_token)
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
def generate_live_export(background_tasks: BackgroundTasks, x_live_operator_token: Optional[str] = Header(default=None)) -> RedirectResponse:
    require_operator(x_live_operator_token)
    _export_state.update({"status": "running", "error": None})
    background_tasks.add_task(_run_export)
    return RedirectResponse("/live", status_code=303)


@router.get("/export/download")
def download_live_export():
    path = _export_state.get("path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="No LIVE export is ready")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=Path(path).name)


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
