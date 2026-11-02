from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import require_admin
from app.core.security import hash_password, issue_access_token, verify_password
from app.models import AuditLog, User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=12, max_length=1024)
    role: str = Field(default="user", pattern=r"^(admin|user)$")


@router.post("/auth/login")
def login(body: LoginRequest, request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    if not settings.auth_signing_secret:
        raise HTTPException(status_code=503, detail="dashboard authentication is not configured")
    with request.app.state.db_factory() as db:
        user = db.query(User).filter(User.username == body.username, User.active.is_(True)).one_or_none()
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = issue_access_token(
            user.id,
            user.role,
            settings.auth_signing_secret,
            settings.access_token_minutes * 60,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": settings.access_token_minutes * 60,
            "username": user.username,
            "role": user.role,
        }


@router.get("/users")
def list_users(request: Request, _: User = Depends(require_admin)) -> dict[str, list[dict[str, object]]]:
    with request.app.state.db_factory() as db:
        rows = db.execute(select(User).order_by(User.created_at)).scalars().all()
        return {"users": [
            {"id": user.id, "username": user.username, "role": user.role, "active": user.active, "created_at": user.created_at.isoformat()}
            for user in rows
        ]}


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, admin: User = Depends(require_admin)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        if db.query(User).filter(User.username == body.username).first():
            raise HTTPException(409, "username already exists")
        user = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
        db.add(user)
        db.flush()
        db.add(AuditLog(
            event_type="security.user_created",
            source="dashboard",
            action="dashboard user created",
            result="success",
            metadata_json={"admin_id": admin.id, "user_id": user.id, "role": user.role},
        ))
        db.commit()
        return {"id": user.id, "username": user.username, "role": user.role, "active": user.active}
