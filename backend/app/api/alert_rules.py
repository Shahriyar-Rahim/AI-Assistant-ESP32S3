from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AutomationEndpoint, AutomationRule, User
from app.workers.automation import WEATHER_METRICS

router = APIRouter()


class WeatherRuleConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    metric: str
    operator: Literal["gte", "lte"]
    threshold: float


class EndpointRuleConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(min_length=1, max_length=36)
    minimum_failures: int = Field(ge=1, le=100)


class NotifyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["notify"] = "notify"
    title: str = Field(min_length=1, max_length=200)


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_type: Literal["weather", "endpoint_failure"]
    conditions: dict[str, object]
    action: NotifyAction
    cooldown_seconds: int = Field(default=1800, ge=300, le=604800)
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=300, le=604800)
    action: NotifyAction | None = None


def _serialize(rule: AutomationRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "name": rule.name,
        "trigger_type": rule.trigger_type,
        "conditions": rule.conditions,
        "action": rule.action,
        "enabled": rule.enabled,
        "cooldown_seconds": rule.cooldown_seconds,
        "last_triggered_at": rule.last_triggered_at.isoformat() if rule.last_triggered_at else None,
    }


@router.get("/automation/rules")
def list_rules(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        rows = db.execute(select(AutomationRule).order_by(AutomationRule.name)).scalars().all()
        return {"rules": [_serialize(row) for row in rows]}


@router.post("/automation/rules", status_code=201)
def create_rule(body: RuleCreate, request: Request, user: User = Depends(require_admin)) -> dict[str, object]:
    if body.trigger_type == "weather":
        conditions = WeatherRuleConditions.model_validate(body.conditions)
        if conditions.metric not in WEATHER_METRICS:
            raise HTTPException(422, "weather metric is not supported")
        normalized = conditions.model_dump()
    else:
        conditions = EndpointRuleConditions.model_validate(body.conditions)
        with request.app.state.db_factory() as db:
            if db.get(AutomationEndpoint, conditions.endpoint_id) is None:
                raise HTTPException(404, "health-check endpoint not found")
        normalized = conditions.model_dump()
    with request.app.state.db_factory() as db:
        rule = AutomationRule(
            id=str(uuid4()),
            name=body.name,
            trigger_type=body.trigger_type,
            conditions=normalized,
            action=body.action.model_dump(),
            cooldown_seconds=body.cooldown_seconds,
            enabled=body.enabled,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        result = _serialize(rule)
    return result


@router.patch("/automation/rules/{rule_id}")
def update_rule(
    rule_id: str,
    body: RuleUpdate,
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, object]:
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(422, "at least one automation rule field is required")
    if values.get("action") is not None:
        values["action"] = values["action"].model_dump()
    with request.app.state.db_factory() as db:
        rule = db.get(AutomationRule, rule_id)
        if rule is None:
            raise HTTPException(404, "automation rule not found")
        for field, value in values.items():
            if value is None:
                raise HTTPException(422, f"{field} cannot be null")
            setattr(rule, field, value)
        db.commit()
        db.refresh(rule)
        return _serialize(rule)


@router.delete("/automation/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, request: Request, _: User = Depends(require_admin)) -> None:
    with request.app.state.db_factory() as db:
        rule = db.get(AutomationRule, rule_id)
        if rule is None:
            raise HTTPException(404, "automation rule not found")
        db.delete(rule)
        db.commit()
