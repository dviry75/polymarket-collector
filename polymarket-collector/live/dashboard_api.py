from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, LiveAuthManager
from .config import LiveConfig
from .dashboard_infrastructure import InfrastructureSampler
from .dashboard_read_model import DashboardQueryError, DashboardReadModel, resolve_window
from .repository import LiveRepository


class DashboardMeta(BaseModel):
    as_of: str
    timezone: str = "Asia/Jerusalem"
    environment: str
    execution_mode: str
    cutover_at: str | None = None
    source: str
    verified: bool
    api_version: str = "v1"


class DashboardResponse(BaseModel):
    meta: DashboardMeta
    data: dict[str, Any]


class DashboardErrorBody(BaseModel):
    code: str
    message: str


class DashboardErrorResponse(BaseModel):
    error: DashboardErrorBody
    as_of: str


class ResponseCache:
    def __init__(self):
        self._condition = threading.Condition()
        self._values: dict[str, tuple[float, Any]] = {}
        self._loading: set[str] = set()

    def get(self, key: str, ttl_seconds: float, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._condition:
            cached = self._values.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return copy.deepcopy(cached[1])
            while key in self._loading:
                self._condition.wait(timeout=3.0)
                cached = self._values.get(key)
                if cached and time.monotonic() - cached[0] < ttl_seconds:
                    return copy.deepcopy(cached[1])
            self._loading.add(key)
        try:
            value = loader()
        finally:
            with self._condition:
                if "value" in locals():
                    self._values[key] = (time.monotonic(), value)
                self._loading.discard(key)
                self._condition.notify_all()
        return copy.deepcopy(value)


class SessionRateLimiter:
    def __init__(self, *, limit: int = 600, window_seconds: int = 60):
        self.limit = max(10, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.limit:
                return False
            attempts.append(now)
            if len(self._attempts) > 10_000:
                self._attempts = defaultdict(deque, {key: attempts})
            return True


router = APIRouter(prefix="/live/dashboard/v1", tags=["live-dashboard-v1"])
_model: DashboardReadModel | None = None
_infrastructure: InfrastructureSampler | None = None
_auth: LiveAuthManager | None = None
_trader_status_provider: Callable[[], dict[str, Any] | None] | None = None
_rate_limiter = SessionRateLimiter()
_response_cache = ResponseCache()


def configure_dashboard_api(
    db_path: str | Path,
    config: LiveConfig,
    *,
    trader_status_provider: Callable[[], dict[str, Any] | None] | None = None,
) -> None:
    global _model, _infrastructure, _auth, _trader_status_provider
    repo = LiveRepository(db_path, query_only=True)
    environment = str(getattr(config, "environment", "LIVE") or "LIVE")
    _model = DashboardReadModel(
        repo, environment=environment, execution_mode="REAL_TRADING",
        market_stale_seconds=float(config.max_market_data_age_seconds),
    )
    _infrastructure = InfrastructureSampler(db_path)
    _auth = LiveAuthManager(
        config,
        session_version_getter=lambda: repo.get_state("session_version", "1"),
    )
    _trader_status_provider = trader_status_provider


def _services() -> tuple[DashboardReadModel, InfrastructureSampler, LiveAuthManager]:
    if _model is None or _infrastructure is None or _auth is None:
        raise RuntimeError("dashboard API is not configured")
    return _model, _infrastructure, _auth


def require_dashboard_session(request: Request) -> None:
    _model_value, _infra, auth = _services()
    if not auth.configured():
        raise HTTPException(status_code=503, detail="dashboard login is not configured")
    token = request.cookies.get(COOKIE_NAME)
    if not auth.verify_session(token):
        raise HTTPException(status_code=401, detail="dashboard login required")
    key = hashlib.sha256((token or request.client.host if request.client else "unknown").encode()).hexdigest()
    if not _rate_limiter.allow(key):
        raise HTTPException(status_code=429, detail="dashboard rate limit exceeded")


def _meta(model: DashboardReadModel) -> DashboardMeta:
    return DashboardMeta(**model.metadata())


def _response(data: dict[str, Any]) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return DashboardResponse(meta=_meta(model), data=data)


def _window(
    range_key: str,
    from_date: str | None,
    to_date: str | None,
):
    try:
        return resolve_window(range_key, from_date=from_date, to_date=to_date)
    except DashboardQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _trader_status() -> dict[str, Any] | None:
    if _trader_status_provider is None:
        return None
    try:
        return _trader_status_provider()
    except Exception:
        return None


@router.get("/overview", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def overview(
    range: str = Query("today", pattern="^(today|yesterday|3d|7d|30d|custom)$"),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    window = _window(range, from_date, to_date)
    key = f"overview:{window.start_utc.isoformat()}:{window.end_utc.isoformat()}"
    return _response(_response_cache.get(key, 2.0, lambda: model.overview(window, trader_status=_trader_status())))


@router.get("/equity", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def equity() -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.account_equity())


@router.get("/pnl/summary", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def pnl_summary(
    range: str = Query("today", pattern="^(today|yesterday|3d|7d|30d|custom)$"),
    from_date: str | None = None,
    to_date: str | None = None,
) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.pnl_summary(_window(range, from_date, to_date)))


@router.get("/pnl/timeseries", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def pnl_timeseries(
    range: str = Query("7d", pattern="^(today|yesterday|3d|7d|30d|custom)$"),
    from_date: str | None = None,
    to_date: str | None = None,
) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    window = _window(range, from_date, to_date)
    key = f"timeseries:{window.start_utc.isoformat()}:{window.end_utc.isoformat()}"
    return _response(_response_cache.get(key, 2.0, lambda: model.pnl_timeseries(window)))


@router.get("/trades", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def trades(
    range: str = Query("7d", pattern="^(today|yesterday|3d|7d|30d|custom)$"),
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(25, ge=1, le=100),
) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    window = _window(range, from_date, to_date)
    key = f"trades:{window.start_utc.isoformat()}:{window.end_utc.isoformat()}:{page}:{page_size}"
    return _response(_response_cache.get(key, 2.0, lambda: model.trade_history(window, page=page, page_size=page_size)))


@router.get("/positions", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def positions() -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.open_positions())


@router.get("/orders", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def orders(
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(25, ge=1, le=100),
) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.open_orders(page=page, page_size=page_size))


@router.get("/markets", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def markets() -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.markets())


@router.get("/activity", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def activity(limit: int = Query(20, ge=1, le=100)) -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.recent_activity(limit=limit))


@router.get("/alerts", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def alerts() -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.alerts())


@router.get("/infrastructure", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def infrastructure() -> DashboardResponse:
    _model_value, infrastructure_value, _auth_value = _services()
    return _response(infrastructure_value.sample())


@router.get("/health", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def health() -> DashboardResponse:
    model, _infra, _auth_value = _services()
    return _response(model.health(_trader_status()))


@router.get("/session", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def session(request: Request) -> DashboardResponse:
    _model_value, _infra_value, auth = _services()
    token = request.cookies.get(COOKIE_NAME)
    return _response({"authenticated": True, "csrf_token": auth.csrf_token(token), "expires_in_seconds": auth.config.session_ttl_seconds})


@router.get("/filters", response_model=DashboardResponse, dependencies=[Depends(require_dashboard_session)])
def filters() -> DashboardResponse:
    return _response({
        "ranges": ["today", "yesterday", "3d", "7d", "30d", "custom"],
        "max_custom_days": 90,
        "timezone": "Asia/Jerusalem",
        "page_sizes": [10, 25, 50, 100],
        "qualities": ["REAL", "UNAVAILABLE", "STALE", "PARTIAL", "ESTIMATED", "ERROR"],
    })


def error_payload(code: str, message: str) -> dict[str, Any]:
    return DashboardErrorResponse(
        error=DashboardErrorBody(code=code, message=message),
        as_of=datetime.now(timezone.utc).isoformat(),
    ).model_dump()
