from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyCookie


cookie = APIKeyCookie(name="demo_v2_session",auto_error=False)


async def require_demo_session(request: Request, token: str | None = Security(cookie)):
    auth=request.app.state.auth
    if not auth.configured():
        raise HTTPException(503,detail={"code":"DEMO_AUTH_NOT_CONFIGURED"})
    principal=auth.verify(token)
    if principal is None:
        raise HTTPException(401,detail={"code":"DEMO_SESSION_REQUIRED"})
    if request.method not in ("GET","HEAD","OPTIONS") and not auth.verify_csrf(token,request.headers.get("X-Demo-CSRF")):
        raise HTTPException(403,detail={"code":"DEMO_CSRF_REQUIRED"})
    return principal
