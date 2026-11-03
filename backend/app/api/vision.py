import asyncio
import hashlib
import hmac
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.dependencies import require_user
from app.models import Device
from app.services.vision import engine, store
from app.services.vision.monitor import VisionError

router = APIRouter(prefix="/vision")
MAX_FRAME_BYTES = 2_000_000


def _monitor(request: Request):
    return request.app.state.vision


async def _run(call):
    try:
        return await call
    except VisionError as error:
        raise HTTPException(409, str(error)) from error


class MonitorToggle(BaseModel):
    enabled: bool


class EnrollRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: Literal["owner", "known"] = "known"


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["owner", "known", "stranger"] | None = None


class MemoryCreate(BaseModel):
    kind: Literal["home", "text"]
    label: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=8000)


class LabelRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class RecordingRequest(BaseModel):
    action: Literal["start", "stop"]
    seconds: int | None = Field(default=None, ge=5, le=86400)


@router.get("/status")
async def status(request: Request, user=Depends(require_user)) -> dict:
    return _monitor(request).status()


@router.post("/monitor")
async def toggle(payload: MonitorToggle, request: Request, user=Depends(require_user)) -> dict:
    monitor = _monitor(request)
    await monitor.set_enabled(payload.enabled)
    return monitor.status()


@router.post("/telegram/test")
async def telegram_test(request: Request, user=Depends(require_user)) -> dict:
    monitor = _monitor(request)
    if not monitor.telegram.configured:
        raise HTTPException(409, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env and restart the backend")
    if not await monitor.telegram.send_message("Your room security assistant is connected to Telegram."):
        raise HTTPException(502, "Telegram rejected the message; check the bot token and chat id")
    return {"sent": True}


@router.get("/people")
async def people(request: Request, user=Depends(require_user)) -> list[dict]:
    return list((await asyncio.to_thread(store.identities, request.app.state.db_factory)).values())


@router.post("/enroll")
async def enroll(payload: EnrollRequest, request: Request, user=Depends(require_user)) -> dict:
    monitor = _monitor(request)
    identity_id = await _run(monitor.enroll(payload.name.strip(), payload.role))
    return {"id": identity_id}


@router.patch("/people/{identity_id}")
async def update_person(identity_id: str, payload: PersonUpdate, request: Request, user=Depends(require_user)) -> dict:
    if not await asyncio.to_thread(store.set_identity, request.app.state.db_factory, identity_id, payload.name, payload.role):
        raise HTTPException(404, "Person not found")
    await _monitor(request).reload()
    return {"updated": True}


@router.delete("/people/{identity_id}")
async def delete_person(identity_id: str, request: Request, user=Depends(require_user)) -> dict:
    if not await asyncio.to_thread(store.delete_identity, request.app.state.db_factory, identity_id):
        raise HTTPException(404, "Person not found")
    monitor = _monitor(request)
    monitor.gallery.remove(identity_id)
    await monitor.reload()
    return {"deleted": True}


@router.get("/events")
async def events(request: Request, limit: int = 50, user=Depends(require_user)) -> list[dict]:
    return await asyncio.to_thread(store.recent_events, request.app.state.db_factory, max(1, min(limit, 500)))


@router.get("/memories")
async def memories(request: Request, kind: str | None = None, user=Depends(require_user)) -> list[dict]:
    return await asyncio.to_thread(store.memories, request.app.state.db_factory, _monitor(request).uid(user.id), kind)


@router.post("/memories")
async def add_memory(payload: MemoryCreate, request: Request, user=Depends(require_user)) -> dict:
    memory_id = await asyncio.to_thread(
        store.add_memory, request.app.state.db_factory, _monitor(request).uid(user.id), payload.kind, payload.label, payload.content)
    return {"id": memory_id}


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str, request: Request, user=Depends(require_user)) -> dict:
    if not await asyncio.to_thread(store.delete_memory, request.app.state.db_factory, _monitor(request).uid(user.id), memory_id):
        raise HTTPException(404, "Memory not found")
    return {"deleted": True}


@router.post("/memories/object")
async def remember_object(payload: LabelRequest, request: Request, user=Depends(require_user)) -> dict:
    return {"id": await _run(_monitor(request).remember_object(payload.label.strip().lower()))}


@router.post("/memories/read-text")
async def read_text(request: Request, label: str = "", user=Depends(require_user)) -> dict:
    return {"text": await _run(_monitor(request).read_paper(label.strip()))}


@router.get("/snapshot")
async def snapshot(request: Request, user=Depends(require_user)) -> Response:
    frame = await _run(_monitor(request).capture())
    return Response(engine.encode_jpeg(frame, 80), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/photo")
async def photo(request: Request, user=Depends(require_user)) -> dict:
    path = await _run(_monitor(request).take_photo("selfie"))
    return {"path": str(path), "name": path.name}


@router.post("/recording")
async def recording(payload: RecordingRequest, request: Request, user=Depends(require_user)) -> dict:
    monitor = _monitor(request)
    if payload.action == "start":
        monitor.start_recording(payload.seconds)
    else:
        monitor.stop_recording()
    return monitor.status()


@router.get("/media")
async def list_media(request: Request, user=Depends(require_user)) -> list[dict]:
    root = _monitor(request).root
    items = []
    for kind in ("photos", "alerts", "recordings"):
        for file in sorted((root / kind).rglob("*"), reverse=True)[:100] if (root / kind).exists() else []:
            if file.is_file():
                items.append({"kind": kind, "path": file.relative_to(root).as_posix(), "bytes": file.stat().st_size})
    return items


@router.get("/media/{media_path:path}")
async def media(media_path: str, request: Request, user=Depends(require_user)) -> FileResponse:
    root = _monitor(request).root.resolve()
    target = (root / media_path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target)


@router.post("/frames/{device_id}", status_code=202)
async def device_frame(device_id: str, request: Request) -> dict:
    """JPEG frames pushed by the ESP32-S3 when its USB host reads a UVC camera."""
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""

    def authorised() -> bool:
        with request.app.state.db_factory() as db:
            device = db.get(Device, device_id)
            if device is not None:
                return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), device.credential_hash)
        return bool(settings.app_env != "production" and settings.device_token and hmac.compare_digest(token, settings.device_token))

    if not token or not await asyncio.to_thread(authorised):
        raise HTTPException(401, "device authentication failed")
    body = await request.body()
    if not body or len(body) > MAX_FRAME_BYTES:
        raise HTTPException(413, "frame is empty or too large")
    frame = engine.decode_jpeg(body)
    if frame is None:
        raise HTTPException(400, "frame is not a valid JPEG")
    _monitor(request).push_remote(frame)
    return {"accepted": True}
