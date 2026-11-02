from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AutomationEndpoint, EndpointHealthCheck, User
from app.services.automation.health_checks import check_endpoint, validate_endpoint_url

router = APIRouter()


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: HttpUrl
    interval_seconds: int = Field(default=600, ge=60, le=86_400)
    enabled: bool = True


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: HttpUrl | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    enabled: bool | None = None


def _serialize(endpoint: AutomationEndpoint) -> dict[str, object]:
    return {
        "id": endpoint.id,
        "name": endpoint.name,
        "url": endpoint.url,
        "interval_seconds": endpoint.interval_seconds,
        "enabled": endpoint.enabled,
        "last_status": endpoint.last_status,
        "last_response_ms": endpoint.last_response_ms,
        "last_checked_at": endpoint.last_checked_at.isoformat() if endpoint.last_checked_at else None,
        "failure_count": endpoint.failure_count,
    }


@router.get("/automation/endpoints")
def list_endpoints(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        endpoints = db.execute(select(AutomationEndpoint).order_by(AutomationEndpoint.name)).scalars().all()
        return {"endpoints": [_serialize(endpoint) for endpoint in endpoints]}


@router.post("/automation/endpoints", status_code=201)
async def create_endpoint(body: EndpointCreate, request: Request, _: User = Depends(require_admin)) -> dict[str, object]:
    url = str(body.url)
    try:
        validate_endpoint_url(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with request.app.state.db_factory() as db:
        endpoint = AutomationEndpoint(
            name=body.name,
            url=url,
            interval_seconds=body.interval_seconds,
            enabled=body.enabled,
        )
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        return _serialize(endpoint)


@router.patch("/automation/endpoints/{endpoint_id}")
def update_endpoint(
    endpoint_id: str,
    body: EndpointUpdate,
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, object]:
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(422, "at least one endpoint field must be provided")
    if values.get("url") is not None:
        try:
            values["url"] = validate_endpoint_url(str(values["url"]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    with request.app.state.db_factory() as db:
        endpoint = db.get(AutomationEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "endpoint not found")
        for field, value in values.items():
            if value is None:
                raise HTTPException(422, f"{field} cannot be null")
            setattr(endpoint, field, str(value) if field == "url" else value)
        db.commit()
        db.refresh(endpoint)
        return _serialize(endpoint)


@router.delete("/automation/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(endpoint_id: str, request: Request, _: User = Depends(require_admin)):
    with request.app.state.db_factory() as db:
        endpoint = db.get(AutomationEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "endpoint not found")
        db.delete(endpoint)
        db.commit()


@router.post("/automation/endpoints/{endpoint_id}/ping")
async def manual_ping(endpoint_id: str, request: Request, _: User = Depends(require_admin)) -> dict[str, object]:
    try:
        return await check_endpoint(request.app, endpoint_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/automation/endpoints/{endpoint_id}/history")
def endpoint_history(
    endpoint_id: str,
    request: Request,
    limit: int = 50,
    _: User = Depends(require_user),
) -> dict[str, object]:
    if not 1 <= limit <= 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    with request.app.state.db_factory() as db:
        if db.get(AutomationEndpoint, endpoint_id) is None:
            raise HTTPException(404, "endpoint not found")
        rows = db.execute(
            select(EndpointHealthCheck)
            .where(EndpointHealthCheck.endpoint_id == endpoint_id)
            .order_by(EndpointHealthCheck.checked_at.desc())
            .limit(limit)
        ).scalars().all()
        return {"history": [{
            "checked_at": row.checked_at.isoformat(),
            "http_status": row.http_status,
            "response_ms": row.response_ms,
            "result": row.result,
            "error": row.error,
        } for row in rows]}
