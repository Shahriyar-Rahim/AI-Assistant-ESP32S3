from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, Device, OTARelease, User

router = APIRouter()
CHANNELS = {"development", "beta", "stable"}
MAX_FIRMWARE_BYTES = 32 * 1024 * 1024


def _serialize(release: OTARelease) -> dict[str, object]:
    return {
        "id": release.id,
        "version": release.version,
        "channel": release.channel,
        "sha256": release.sha256,
        "size_bytes": release.size_bytes,
        "hardware_target": release.hardware_target,
        "created_at": release.created_at.isoformat(),
    }


@router.get("/ota/releases")
def list_releases(
    request: Request,
    channel: str | None = None,
    _: User = Depends(require_user),
) -> dict[str, object]:
    if channel is not None and channel not in CHANNELS:
        raise HTTPException(422, "channel must be development, beta, or stable")
    with request.app.state.db_factory() as db:
        query = select(OTARelease)
        if channel:
            query = query.where(OTARelease.channel == channel)
        releases = db.execute(query.order_by(OTARelease.created_at.desc()).limit(100)).scalars().all()
        return {"releases": [_serialize(item) for item in releases]}


@router.post("/ota/releases", status_code=201)
async def upload_release(
    request: Request,
    version: str = Form(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._+-]*$"),
    channel: str = Form(default="development"),
    hardware_target: str = Form(default="esp32-s3-n16r8", min_length=1, max_length=64),
    firmware: UploadFile = File(...),
    user: User = Depends(require_admin),
) -> dict[str, object]:
    if channel not in CHANNELS:
        raise HTTPException(422, "channel must be development, beta, or stable")
    if firmware.content_type not in {"application/octet-stream", "application/x-binary"}:
        await firmware.close()
        raise HTTPException(415, "firmware upload must use application/octet-stream")
    data = await firmware.read(MAX_FIRMWARE_BYTES + 1)
    await firmware.close()
    if not data or len(data) > MAX_FIRMWARE_BYTES:
        raise HTTPException(413, "firmware image must be between 1 byte and 32 MiB")
    if data[0] != 0xE9:
        raise HTTPException(422, "uploaded file is not an ESP image (invalid image magic)")
    digest = hashlib.sha256(data).hexdigest()
    storage_dir = Path(request.app.state.settings.firmware_storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    destination = storage_dir / f"{digest}.bin"
    if not destination.exists():
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=storage_dir, prefix=".upload-", delete=False) as temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, destination)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise HTTPException(500, "firmware image could not be stored") from exc
    with request.app.state.db_factory() as db:
        release = OTARelease(
            id=str(uuid4()),
            version=version,
            channel=channel,
            sha256=digest,
            storage_path=str(destination),
            size_bytes=len(data),
            hardware_target=hardware_target,
            created_by=user.id,
        )
        db.add(release)
        db.add(AuditLog(
            event_type="ota.release_uploaded",
            source="dashboard",
            action="firmware release uploaded",
            result="success",
            metadata_json={
                "user_id": user.id,
                "release_id": release.id,
                "version": version,
                "channel": channel,
                "sha256": digest,
                "size_bytes": len(data),
            },
        ))
        db.commit()
        db.refresh(release)
        return _serialize(release)


class ChannelUpdate(BaseModel):
    channel: str = Field(pattern=r"^(development|beta|stable)$")


@router.patch("/ota/releases/{release_id}/channel")
def update_channel(
    release_id: str,
    body: ChannelUpdate,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        release = db.get(OTARelease, release_id)
        if release is None:
            raise HTTPException(404, "firmware release not found")
        previous = release.channel
        release.channel = body.channel
        db.add(AuditLog(
            event_type="ota.channel_changed",
            source="dashboard",
            action="firmware release channel changed",
            result="success",
            metadata_json={"user_id": user.id, "release_id": release_id, "from": previous, "to": body.channel},
        ))
        db.commit()
        db.refresh(release)
        return _serialize(release)


@router.post("/ota/releases/{release_id}/deploy/{device_id}")
async def deploy_release(
    release_id: str,
    device_id: str,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        release = db.get(OTARelease, release_id)
        device = db.get(Device, device_id)
        if release is None or device is None:
            raise HTTPException(404, "firmware release or device not found")
        if device.config.get("hardware_target", "esp32-s3-n16r8") != release.hardware_target:
            raise HTTPException(409, "firmware release does not match the device hardware target")
        manifest = {
            **_serialize(release),
            "download_path": f"/api/v1/ota/releases/{release.id}/binary",
        }
        db.add(AuditLog(
            event_type="ota.deploy_requested",
            source="dashboard",
            device_id=device_id,
            action="firmware deployment requested",
            result="success",
            metadata_json={"user_id": user.id, "release_id": release_id, "sha256": release.sha256},
        ))
        db.commit()
    delivery = "stored_until_reconnect"
    async with request.app.state.ota_pending_lock:
        request.app.state.ota_pending[device_id] = manifest
        websocket = request.app.state.device_sockets.get(device_id)
        if websocket is not None:
            try:
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": str(uuid4()),
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "ota.available",
                    "payload": {"release": manifest},
                })
                delivery = "sent"
                request.app.state.ota_pending.pop(device_id, None)
            except (RuntimeError, OSError) as exc:
                raise HTTPException(502, "deployment recorded but device notification failed") from exc
    return {"device_id": device_id, "release": manifest, "delivery": delivery}


@router.get("/ota/releases/{release_id}/progress")
def release_progress(
    release_id: str,
    device_id: str,
    request: Request,
    _: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        if db.get(OTARelease, release_id) is None:
            raise HTTPException(404, "firmware release not found")
    return request.app.state.ota_progress.get((device_id, release_id), {
        "device_id": device_id,
        "release_id": release_id,
        "state": "not_started",
    })


@router.get("/ota/releases/{release_id}/binary")
def download_release(release_id: str, request: Request) -> FileResponse:
    supplied = request.headers.get("authorization", "")
    token = supplied.removeprefix("Bearer ").strip() if supplied.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(401, "device bearer token required")
    with request.app.state.db_factory() as db:
        release = db.get(OTARelease, release_id)
        if release is None:
            raise HTTPException(404, "firmware release not found")
        devices = db.execute(select(Device)).scalars().all()
        valid = any(
            hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), device.credential_hash)
            for device in devices
        )
        shared_valid = bool(request.app.state.settings.device_token) and hmac.compare_digest(
            token, request.app.state.settings.device_token
        )
        if not (valid or shared_valid):
            raise HTTPException(401, "device authentication failed")
        path = Path(release.storage_path).resolve()
        root = Path(request.app.state.settings.firmware_storage_dir).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(404, "firmware image is unavailable")
        release_data = _serialize(release)
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            event_type="ota.binary_downloaded",
            source="device",
            action="firmware binary downloaded",
            result="success",
            metadata_json={"release_id": release_id, "sha256": release_data["sha256"]},
        ))
        db.commit()
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"firmware-{release_data['version']}.bin",
        headers={"X-Firmware-SHA256": str(release_data["sha256"])},
    )
