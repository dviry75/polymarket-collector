from fastapi import HTTPException


async def require_demo_session() -> None:
    # Fail closed until DEMO-owned login/session/revocation are implemented.
    # In particular, an existing LIVE cookie never authenticates this API.
    raise HTTPException(status_code=503, detail={"code": "DEMO_AUTH_NOT_IMPLEMENTED"})
