from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from live.auth import COOKIE_NAME, LiveAuthManager
from live.config import LiveConfig
from live.dashboard_api import configure_dashboard_api, error_payload, router as dashboard_api_router
from live.ipc import TraderIPCClient
from live.repository import LiveRepository


logger = logging.getLogger("live.dashboard.auth")
app = FastAPI(title="Polymarket LIVE Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(dashboard_api_router)
_config: LiveConfig | None = None
_ipc: TraderIPCClient | None = None
_repo: LiveRepository | None = None
_auth: LiveAuthManager | None = None


def trader_status() -> dict[str, Any] | None:
    if _ipc is None:
        return None
    try:
        value = _ipc.call("STATUS")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


@app.on_event("startup")
def startup() -> None:
    global _config, _ipc, _repo, _auth
    _config = LiveConfig.from_env()
    _repo = LiveRepository(Path(_config.live_db_path), query_only=True)
    _repo.get_state("dashboard_schema_version", "")
    _ipc = TraderIPCClient(_config.trader_socket_path, timeout_seconds=0.75)
    _auth = LiveAuthManager(
        _config,
        session_version_getter=lambda: _repo.get_state("session_version", "1") if _repo else "1",
    )
    configure_dashboard_api(
        Path(_config.live_db_path), _config,
        trader_status_provider=trader_status,
    )


def auth_services() -> tuple[LiveConfig, LiveAuthManager]:
    if _config is None or _auth is None:
        raise HTTPException(status_code=503, detail="dashboard authentication is unavailable")
    return _config, _auth


def safe_next(value: str | None) -> str:
    return "/live-status/" if value not in {"/live-status/", "/live-status"} else str(value)


def same_origin(request: Request, config: LiveConfig) -> bool:
    origin = request.headers.get("origin")
    return not origin or origin.rstrip("/") == config.public_base_url.rstrip("/")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/live"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def dashboard_http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/live/dashboard/v1/"):
        code = {
            401: "AUTHENTICATION_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND",
            422: "INVALID_QUERY", 429: "RATE_LIMITED", 503: "SERVICE_UNAVAILABLE",
        }.get(exc.status_code, "REQUEST_FAILED")
        return JSONResponse(error_payload(code, str(exc.detail)), status_code=exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def dashboard_unhandled_error(request: Request, exc: Exception):
    if request.url.path.startswith("/live/dashboard/v1/"):
        logger.error("dashboard API failed path=%s type=%s", request.url.path, type(exc).__name__)
        return JSONResponse(error_payload("INTERNAL_ERROR", "dashboard request failed"), status_code=500)
    raise exc


@app.get("/live/login", response_class=HTMLResponse)
def login_page(next: str | None = None) -> str:
    destination = safe_next(next)
    return f"""<!doctype html><html lang=\"he\" dir=\"rtl\"><head><meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>התחברות לדאשבורד LIVE</title>
    <style>body{{font-family:Arial,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f6fa;color:#182536}}form{{width:min(360px,calc(100vw - 40px));background:white;border:1px solid #d8e1ec;padding:24px;border-radius:12px;box-shadow:0 14px 40px #1b34551a}}label{{display:block;margin:14px 0}}input{{width:100%;box-sizing:border-box;padding:11px;border:1px solid #bdc9d8;border-radius:7px}}button{{width:100%;padding:11px;background:#1264d8;color:white;border:0;border-radius:7px;font-weight:700}}small,p{{color:#64758a}}#msg{{color:#b42318;min-height:1.2em}}</style></head>
    <body><form id=\"login\"><h1>Polymarket LIVE</h1><p>דאשבורד תפעולי לקריאה בלבד</p><label>שם משתמש<input id=\"u\" autocomplete=\"username\" required></label><label>סיסמה<input id=\"p\" type=\"password\" autocomplete=\"current-password\" required></label><button>התחברות</button><p id=\"msg\"></p><small>הפרטים אינם נשמרים בדפדפן.</small></form>
    <script>const destination={destination!r};login.addEventListener('submit',async(e)=>{{e.preventDefault();msg.textContent='';const r=await fetch('/live/login',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:u.value,password:p.value,next:destination}})}});if(r.ok){{location.assign(destination)}}else{{msg.textContent='ההתחברות נכשלה';}}}});</script></body></html>"""


@app.post("/live/login")
def login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    config, auth = auth_services()
    key = request.client.host if request.client else "unknown"
    if not same_origin(request, config):
        raise HTTPException(status_code=403, detail="cross-origin login is forbidden")
    if not auth.configured():
        logger.warning("dashboard login blocked reason=not_configured client=%s", key)
        raise HTTPException(status_code=503, detail="dashboard login is not configured")
    if auth.rate_limited(key):
        logger.warning("dashboard login blocked reason=rate_limit client=%s", key)
        raise HTTPException(status_code=429, detail="too many login attempts")
    if payload.get("username") != config.login_username or not auth.verify_password(str(payload.get("password") or "")):
        auth.record_failure(key)
        logger.warning("dashboard login blocked reason=invalid_login client=%s", key)
        raise HTTPException(status_code=401, detail="invalid login")
    token = auth.create_session(config.login_username)
    response = JSONResponse({"ok": True, "next": safe_next(str(payload.get("next") or ""))})
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=True, samesite="strict", path="/",
        max_age=config.session_ttl_seconds if config.session_ttl_seconds > 0 else None,
    )
    logger.info("dashboard login ok client=%s", key)
    return response


@app.post("/live/logout")
def logout(request: Request, x_live_csrf_token: str | None = None) -> JSONResponse:
    config, auth = auth_services()
    token = request.cookies.get(COOKIE_NAME)
    supplied = request.headers.get("x-live-csrf-token") or x_live_csrf_token
    if not same_origin(request, config) or not auth.verify_session(token) or not auth.verify_csrf(token, supplied):
        raise HTTPException(status_code=403, detail="logout authorization failed")
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    logger.info("dashboard logout ok")
    return response


@app.get("/live/auth/check")
def auth_check(request: Request) -> Response:
    _config_value, auth = auth_services()
    if not auth.verify_session(request.cookies.get(COOKIE_NAME)):
        return Response(status_code=401)
    return Response(status_code=204, headers={"Cache-Control": "no-store, private"})


@app.get("/live")
@app.get("/live/")
def legacy_live(request: Request) -> RedirectResponse:
    _config_value, auth = auth_services()
    if not auth.verify_session(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/live/login?next=" + quote("/live-status/"), status_code=303)
    return RedirectResponse("/live-status/", status_code=303)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/live-status/", status_code=307)


@app.get("/health")
def public_health() -> dict[str, str]:
    try:
        if _repo is None:
            return {"status": "degraded"}
        _repo.get_state("dashboard_schema_version", "")
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}
