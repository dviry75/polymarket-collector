from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from ..domain.trading import FrozenModel, RuleSpec, decimal, text
from ..persistence.repository import dump
from .dependencies import require_demo_session
from .read_model import TradeFilter, TradeQuery, cohort, kpi, trade_view
from .schemas import SessionView,RuleView,RuleList,TradePage,TradeDetail,KPIView,MarketList,HealthView


PREFIX="/demo/api/v1"
auth_router=APIRouter(prefix=PREFIX+"/auth",tags=["Authentication"])
router=APIRouter(prefix=PREFIX,dependencies=[Depends(require_demo_session)])


def respond(value,status=200):
    # Explicit Decimal -> string before Starlette/FastAPI's generic encoder.
    return JSONResponse(json.loads(dump(value)),status_code=status)


class Login(FrozenModel):
    username: Annotated[str,Field(min_length=1,max_length=120)]
    password: SecretStr


class RuleUpdate(FrozenModel):
    spec: RuleSpec
    expected_version_id: str


class Reset(FrozenModel):
    confirmation: str


@auth_router.post("/login",response_model=SessionView)
async def login(body: Login, request: Request):
    """Set host-only HttpOnly DEMO cookie; return CSRF for authenticated writes."""
    auth=request.app.state.auth
    if not auth.configured():
        raise HTTPException(503,detail={"code":"DEMO_AUTH_NOT_CONFIGURED"})
    if auth.rate_limited(request.client.host if request.client else "local"):
        raise HTTPException(429,detail={"code":"DEMO_LOGIN_RATE_LIMIT"},headers={"Retry-After":"60"})
    password=body.password.get_secret_value()
    if len(password)>1024:
        raise HTTPException(401,detail={"code":"DEMO_INVALID_CREDENTIALS"})
    principal=await auth.validate(body.username,password)
    if principal is None:
        raise HTTPException(401,detail={"code":"DEMO_INVALID_CREDENTIALS"})
    token=auth.create_session(principal.subject)
    response=respond({"user":principal.subject,"csrf_token":auth.csrf(token)})
    config=request.app.state.config
    response.set_cookie(config.session_cookie_name,token,max_age=config.session_ttl_seconds,
        httponly=True,secure=config.session_cookie_secure,samesite="strict",path=config.session_cookie_path)
    response.headers["Cache-Control"]="no-store"
    return response


@router.get("/auth/current",tags=["Authentication"],response_model=SessionView)
async def current_session(request: Request, principal=Depends(require_demo_session)):
    return respond({"user":principal.subject,"csrf_token":request.app.state.auth.csrf(request.cookies["demo_v2_session"])})


@router.post("/auth/logout",tags=["Authentication"])
async def logout(request: Request):
    """Revoke all DEMO sessions, including sessions issued before a process restart."""
    request.app.state.auth.revoke()
    response=respond({"logged_out":True,"scope":"ALL_DEMO_SESSIONS"})
    response.delete_cookie("demo_v2_session",path="/demo/",secure=request.app.state.config.session_cookie_secure,
                           httponly=True,samesite="strict")
    return response


def rule_view(repository,row):
    positions=repository.positions(rule_id=row["id"])
    realized=sum((decimal(p["net_pnl_text"]) for p in positions if p["status"]=="CLOSED"),Decimal(0))
    return {key:value for key,value in row.items() if key!="snapshot_json"} | {
        "trade_count":len(positions),"wallet":str(repository.wallet(row["id"])),"net_pnl":text(realized)}


@router.get("/rules",tags=["Rules"],response_model=RuleList)
async def rules(request: Request):
    repo=request.app.state.repository
    return respond({"items":[rule_view(repo,row) for row in repo.rules()]})


@router.post("/rules",status_code=201,tags=["Rules"],response_model=RuleView)
async def create_rule(body: RuleSpec,request: Request):
    repo=request.app.state.repository
    return respond(rule_view(repo,repo.create_rule(body)),201)


@router.get("/rules/{rule_id}",tags=["Rules"],response_model=RuleView)
async def get_rule(rule_id: str,request: Request):
    repo=request.app.state.repository
    return respond(rule_view(repo,repo.rule(rule_id)))


@router.put("/rules/{rule_id}",tags=["Rules"],response_model=RuleView)
async def update_rule(rule_id: str,body: RuleUpdate,request: Request):
    """Full validated spec; compare current version to avoid lost concurrent edits."""
    repo=request.app.state.repository
    return respond(rule_view(repo,repo.update_rule(rule_id,body.spec,expected_version=body.expected_version_id)))


@router.get("/rules/{rule_id}/versions",tags=["Rules"])
async def versions(rule_id: str,request: Request):
    repo=request.app.state.repository
    repo.rule(rule_id)
    with repo.database.connect() as conn:
        rows=[dict(row) for row in conn.execute("SELECT * FROM demo_v2_rule_versions WHERE rule_id=? ORDER BY version",(rule_id,))]
    return respond({"items":[{**{key:value for key,value in row.items() if key!="snapshot_json"},
                               "spec":json.loads(row["snapshot_json"])} for row in rows]})


@router.post("/rules/{rule_id}/{action}",tags=["Rules"],response_model=RuleView)
async def rule_action(rule_id: str, action: Literal["enable","disable","duplicate","archive"],request: Request):
    repo=request.app.state.repository
    row=repo.rule(rule_id)
    fields=row["spec"].model_dump(mode="json")
    if action=="duplicate":
        fields.update(name=(fields["name"][:110]+" (copy)"),archived=False,enabled=False)
        return respond(rule_view(repo,repo.create_rule(RuleSpec.model_validate(fields))),201)
    if action=="enable":
        if fields["archived"]:
            raise HTTPException(409,detail={"code":"DEMO_RULE_ARCHIVED"})
        fields["enabled"]=True
    elif action=="disable":
        fields["enabled"]=False
    elif action=="archive":
        fields.update(enabled=False,archived=True)
    updated=repo.update_rule(rule_id,RuleSpec.model_validate(fields),expected_version=row["current_version_id"])
    return respond(rule_view(repo,updated))


@router.delete("/rules/{rule_id}",tags=["Rules"],response_model=RuleView)
async def archive_rule(rule_id: str,request: Request):
    """Soft archive; retain history and continue existing position management."""
    return await rule_action(rule_id,"archive",request)


@router.get("/trades",tags=["Trades"],response_model=TradePage)
async def trades(request: Request,filters: Annotated[TradeQuery,Query()]):
    """Newest first by entry time/id; date interval [from_date,to_date). Decimal strings."""
    rows=cohort(request.app.state.repository,filters)
    limit,offset=filters.limit,filters.offset
    return respond({"items":[trade_view(row) for row in rows[offset:offset+limit]],"total":len(rows),"limit":limit,"offset":offset})


@router.get("/trades/{position_id}",tags=["Trades"],response_model=TradeDetail)
async def trade_detail(position_id: str,request: Request):
    repo=request.app.state.repository
    with repo.database.connect() as conn:
        row=conn.execute("SELECT * FROM demo_v2_positions WHERE id=?",(position_id,)).fetchone()
        if not row:
            raise KeyError("trade not found")
        row=dict(row)
        version=dict(conn.execute("SELECT * FROM demo_v2_rule_versions WHERE id=?",(row["rule_version_id"],)).fetchone())
        underlying=[dict(value) for value in conn.execute("SELECT * FROM demo_v2_underlying WHERE position_id=? ORDER BY observed_at,id",(position_id,))]
        ledger=[dict(value) for value in conn.execute("SELECT * FROM demo_v2_ledger WHERE position_id=? ORDER BY occurred_at,id",(position_id,))]
    return respond({**trade_view(row),"rule_snapshot":json.loads(version["snapshot_json"]),
                    "market":repo.instance(row["instance_id"]),"underlying_observations":underlying,"ledger":ledger})


@router.get("/kpi",tags=["KPI"],response_model=KPIView)
async def get_kpi(request: Request,filters: Annotated[TradeFilter,Query()]):
    """Same entry cohort as trades; wallet is all signed ledger cash for that cohort.

    Success is positive/(positive+negative)*100, null when denominator is zero.
    Open and break-even trades are excluded from that denominator.
    """
    repo=request.app.state.repository
    return respond(kpi(repo,cohort(repo,filters)))


@router.get("/markets",tags=["Markets"],response_model=MarketList)
async def markets(request: Request):
    runtime=request.app.state.runtime
    data=runtime.providers.market_data
    latest=getattr(data,"latest",{})
    now=runtime.clock()
    instances=[]
    for instance_id,instance in runtime.instances.items():
        if instance_id not in runtime.current.values() and not request.app.state.repository.positions(instance_id=instance_id,status="OPEN"):
            continue
        prices={}
        for side,token in (("UP",instance.up_token_id),("DOWN",instance.down_token_id)):
            observation=latest.get(token)
            prices[side]={"best_bid":observation.best_bid if observation else None,
                "best_ask":observation.best_ask if observation else None,
                "observed_at":observation.observed_at if observation else None,
                "fresh":bool(data and data.token_ready(token)),"listening":token in getattr(data,"token_map",{})}
        evidence=runtime.fees.get(instance_id)
        instances.append({**asdict(instance),"prices":prices,"is_current":instance_id in runtime.current.values(),
            "seconds_from_start":str((now-instance.starts_at).total_seconds()),
            "fee":evidence.current(now).evidence() if evidence else None})
    return respond({"definitions":request.app.state.repository.definitions(),"instances":instances,"as_of":now})


@router.get("/health",tags=["Health"],response_model=HealthView)
async def health(request: Request):
    runtime=request.app.state.runtime
    data=runtime.providers.market_data
    snapshot=request.app.state.health.snapshot()
    return respond({**asdict(snapshot),"entries_allowed":snapshot.entries_allowed,
        "blocker_codes":[c.reason for c in snapshot.components if c.reason],
        "market_data":data.health() if data and hasattr(data,"health") else None,
        "discovery_at":runtime.discovery_at,"recovery_stability_seconds":str(runtime.config.recovery_stability_seconds),
        "engine_generation":runtime.engine.generation,"engine_reason":runtime.engine.reason,
        "workers":{task.get_name():"STOPPED" if task.done() else "RUNNING" for task in runtime.all_tasks()}})


@router.post("/maintenance/reset-trading-data",tags=["Maintenance"])
async def reset_trading(body: Reset,request: Request):
    """Atomic delete of simulation positions/counters/ledger/research. Keep rules.

    No synthetic closing trade is generated, even with open positions.
    """
    if body.confirmation!="RESET_TRADING_DATA":
        raise HTTPException(409,detail={"code":"DEMO_CONFIRMATION_REQUIRED"})
    return respond(await request.app.state.runtime.reset())


@router.post("/maintenance/factory-reset",tags=["Maintenance"])
async def factory_reset(body: Reset,request: Request):
    """Trading reset plus rules/versions. Keep catalog/config/session state."""
    if body.confirmation!="FACTORY_RESET_DEMO":
        raise HTTPException(409,detail={"code":"DEMO_CONFIRMATION_REQUIRED"})
    return respond(await request.app.state.runtime.reset(factory=True))
