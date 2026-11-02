from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, Device, DisplayConfiguration, User
from app.services.device_display import DISPLAY_PAGES, build_display_payload, fetch_display_weather

router = APIRouter()
DisplayState = Literal["idle", "listening", "thinking", "speaking", "happy", "sad", "error", "sleeping"]
AnimationName = Literal["none", "pulse", "bounce", "fade", "wave"]


class DisplayUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DisplayState = "idle"
    emoji: str = Field(default="🙂", min_length=1, max_length=8)
    animation: AnimationName = "none"
    animation_duration_ms: int = Field(default=600, ge=0, le=10_000)
    emoji_rules: dict[DisplayState, str] = Field(default_factory=dict, max_length=8)


class DisplayPageUpdate(BaseModel):
    page: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def validate_values(self):
        if self.page not in DISPLAY_PAGES:
            raise ValueError("unsupported display page")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


@router.get("/devices/{device_id}/display")
def get_display(device_id: str, request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        if db.get(Device, device_id) is None:
            raise HTTPException(404, "device not found")
        current = db.get(DisplayConfiguration, device_id)
        return {
            "device_id": device_id,
            "configuration": current.configuration if current else DisplayUpdate().model_dump(),
            "revision": current.revision if current else 0,
            "delivery": "stored",
        }


@router.put("/devices/{device_id}/display")
async def update_display(
    device_id: str,
    body: DisplayUpdate,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    values = body.model_dump()
    with request.app.state.db_factory() as db:
        if db.get(Device, device_id) is None:
            raise HTTPException(404, "device not found")
        row = db.get(DisplayConfiguration, device_id)
        if row is None:
            row = DisplayConfiguration(device_id=device_id, configuration=values, revision=1)
            db.add(row)
        else:
            row.configuration = values
            row.revision += 1
        db.add(AuditLog(
            event_type="display.configuration",
            source="dashboard",
            device_id=device_id,
            action="display configuration changed",
            result="success",
            metadata_json={"user_id": user.id, "state": body.state, "animation": body.animation},
        ))
        db.commit()
        revision = row.revision
    websocket = request.app.state.device_sockets.get(device_id)
    delivery = "stored_until_reconnect"
    if websocket is not None:
        try:
            await websocket.send_json({
                "protocol_version": 1,
                "message_id": str(uuid4()),
                "device_id": device_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "display.state",
                "payload": {**values, "revision": revision},
            })
            delivery = "sent"
        except (RuntimeError, OSError) as exc:
            raise HTTPException(502, "display configuration saved but device delivery failed") from exc
    return {
        "device_id": device_id,
        "configuration": values,
        "revision": revision,
        "delivery": delivery,
    }


@router.put("/devices/{device_id}/display/page")
async def update_display_page(
    device_id: str,
    body: DisplayPageUpdate,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        location = device.config.get("display_location", {})

    weather = None
    latitude = body.latitude if body.latitude is not None else location.get("latitude")
    longitude = body.longitude if body.longitude is not None else location.get("longitude")
    if body.page == "weather":
        if latitude is None or longitude is None:
            raise HTTPException(422, "weather page requires a location; provide latitude and longitude")
        try:
            weather = await fetch_display_weather(latitude, longitude)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "weather service request failed") from exc
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc

    with request.app.state.db_factory() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        current = db.get(DisplayConfiguration, device_id)
        configuration = dict(current.configuration) if current else DisplayUpdate().model_dump()
        configuration["page"] = body.page
        if body.latitude is not None and body.longitude is not None:
            device_config = dict(device.config)
            device_config["display_location"] = {
                "latitude": body.latitude,
                "longitude": body.longitude,
            }
            device.config = device_config
        if current is None:
            current = DisplayConfiguration(device_id=device_id, configuration=configuration, revision=1)
            db.add(current)
        else:
            current.configuration = configuration
            current.revision += 1
        db.add(AuditLog(
            event_type="display.page",
            source="dashboard",
            device_id=device_id,
            action="display page changed",
            result="success",
            metadata_json={"user_id": user.id, "page": body.page},
        ))
        db.commit()
        revision = current.revision

    payload = build_display_payload(request.app.state.db_factory, body.page, weather)
    websocket = request.app.state.device_sockets.get(device_id)
    delivery = "stored_until_reconnect"
    if websocket is not None:
        try:
            await websocket.send_json({
                "protocol_version": 1,
                "message_id": str(uuid4()),
                "device_id": device_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "display.state",
                "payload": {**payload, "revision": revision},
            })
            delivery = "sent"
        except (RuntimeError, OSError) as exc:
            raise HTTPException(502, "display page saved but device delivery failed") from exc
    return {"device_id": device_id, "page": body.page, "revision": revision, "delivery": delivery}
