from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import AuthenticationError, verify_access_token
from app.models import User

bearer = HTTPBearer(auto_error=False)


def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    settings = request.app.state.settings
    if not settings.auth_signing_secret:
        raise HTTPException(status_code=503, detail="dashboard authentication is not configured")
    if credentials is None:
        raise HTTPException(status_code=401, detail="bearer token required")
    try:
        claims = verify_access_token(credentials.credentials, settings.auth_signing_secret)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    with request.app.state.db_factory() as db:
        user = db.get(User, claims["sub"])
        if user is None or not user.active:
            raise HTTPException(status_code=401, detail="user is disabled or no longer exists")
        if user.role != claims["role"]:
            raise HTTPException(status_code=401, detail="role changed; sign in again")
        request.state.user = user
        request.state.user_id = user.id
        request.state.user_role = user.role
        return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="administrator role required")
    return user
