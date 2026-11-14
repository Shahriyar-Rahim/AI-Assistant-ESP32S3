import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, Device, User

router = APIRouter()


@router.get("/devices")
def list_devices(request: Request, _: User = Depends(require_user)) -> dict[str, list[dict[str, object]]]:
    with request.app.state.db_factory() as db:
        registered = db.execute(select(Device).order_by(Device.created_at)).scalars().all()
        return {"devices": [
            {
                "device_id": device.id,
                "name": device.name,
                "connected": device.id in request.app.state.devices,
                "last_seen": request.app.state.devices.get(device.id, (device.last_seen_at.isoformat() if device.last_seen_at else None, False))[0],
                "simulated": request.app.state.devices.get(device.id, (None, False))[1],
                "config": device.config,
            }
            for device in registered
        ] + [
            {
                "device_id": device_id,
                "name": device_id,
                "connected": True,
                "last_seen": last_seen,
                "simulated": simulated,
                "config": {},
            }
            for device_id, (last_seen, simulated) in request.app.state.devices.items()
            if not any(device.id == device_id for device in registered)
        ]}


class DeviceRegistration(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)


@router.post("/devices/register", status_code=201)
def register_device(
    body: DeviceRegistration,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    with request.app.state.db_factory() as db:
        if db.get(Device, body.device_id) is not None:
            raise HTTPException(409, "device id is already registered")
        db.add(Device(
            id=body.device_id,
            name=body.name,
            credential_hash=hashlib.sha256(token.encode()).hexdigest(),
            config={},
        ))
        db.add(AuditLog(
            event_type="device.registered",
            source="dashboard",
            action="device credential issued",
            result="success",
            metadata_json={"user_id": user.id, "device_id": body.device_id},
        ))
        db.commit()
    return {"device_id": body.device_id, "device_token": token}


@router.patch("/devices/{device_id}/config")
async def update_device_config(
    device_id: str,
    body: dict[str, object],
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    return await set_device_config(request.app, device_id, body, user.id)


async def set_device_config(application, device_id: str, body: dict[str, object], user_id: str) -> dict[str, object]:
    allowed = {"brightness", "microphone_enabled", "volume", "display", "listening_policy"}
    if not body or set(body) - allowed:
        raise HTTPException(422, "configuration contains no values or unsupported keys")
    if "brightness" in body and (
        not isinstance(body["brightness"], int)
        or isinstance(body["brightness"], bool)
        or not 0 <= body["brightness"] <= 100
    ):
        raise HTTPException(422, "brightness must be an integer from 0 to 100")
    if "microphone_enabled" in body and not isinstance(body["microphone_enabled"], bool):
        raise HTTPException(422, "microphone_enabled must be boolean")
    if "volume" in body and (
        not isinstance(body["volume"], int) or isinstance(body["volume"], bool) or not 0 <= body["volume"] <= 100
    ):
        raise HTTPException(422, "volume must be an integer from 0 to 100")
    with application.state.db_factory() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        device.config = {**device.config, **body}
        config = device.config
        db.add(AuditLog(
            event_type="device.config_changed",
            source="dashboard",
            device_id=device_id,
            action="device configuration updated",
            result="success",
            metadata_json={"user_id": user_id, "keys": sorted(body)},
        ))
        db.commit()
    websocket = application.state.device_sockets.get(device_id)
    if websocket is not None:
        from datetime import datetime, timezone
        import uuid

        for key, value in body.items():
            message_type = {
                "brightness": "device.brightness",
                "microphone_enabled": "device.microphone",
                "display": "display.state",
            }.get(key, "device.status")
            try:
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": str(uuid.uuid4()),
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": message_type,
                    "payload": {key: value},
                })
            except Exception as exc:
                raise HTTPException(502, "configuration saved but device delivery failed") from exc
    return {"device_id": device_id, "config": config, "delivery": "sent" if websocket else "stored_until_reconnect"}
