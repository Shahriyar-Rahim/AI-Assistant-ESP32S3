from __future__ import annotations

from collections import deque
import os
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, User, VoiceEnrollment
from app.services.biometric.speaker import (
    BiometricUnavailable,
    MODEL_NAME,
    compare_voice,
    create_enrollment,
)
from app.services.stt.whisper_api import validate_wav

router = APIRouter()


async def _read_sample(file: UploadFile) -> bytes:
    if file.content_type not in {"audio/wav", "audio/x-wav"}:
        raise HTTPException(415, "upload a WAV file")
    maximum = int(os.getenv("MAX_AUDIO_UPLOAD_BYTES", "5242880"))
    audio = await file.read(maximum + 1)
    await file.close()
    try:
        validate_wav(audio, maximum, max_seconds=20)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return audio


def _record(request: Request, user_id: str, action: str, result: str, metadata: dict) -> None:
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            id=str(uuid4()),
            event_type="biometric.verification",
            source="dashboard",
            action=action,
            result=result,
            metadata_json={"user_id": user_id, **metadata},
        ))
        db.commit()


@router.get("/biometric/status")
def enrollment_status(request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        enrollment = db.get(VoiceEnrollment, user.id)
        return {
            "enrolled": enrollment is not None,
            "model": enrollment.model_name if enrollment else None,
            "created_at": enrollment.created_at.isoformat() if enrollment else None,
            "threshold": request.app.state.settings.biometric_threshold,
            "anti_spoofing": "not_configured",
        }


@router.post("/biometric/enroll")
async def enroll_voice(
    request: Request,
    file: UploadFile = File(),
    user: User = Depends(require_admin),
) -> dict[str, object]:
    audio = await _read_sample(file)
    try:
        encrypted = create_enrollment(audio)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BiometricUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    with request.app.state.db_factory() as db:
        enrollment = db.get(VoiceEnrollment, user.id)
        if enrollment is None:
            enrollment = VoiceEnrollment(
                user_id=user.id,
                encrypted_embedding=encrypted,
                model_name=MODEL_NAME,
            )
            db.add(enrollment)
        else:
            enrollment.encrypted_embedding = encrypted
            enrollment.model_name = MODEL_NAME
        db.add(AuditLog(
            event_type="biometric.enrollment",
            source="dashboard",
            action="master voice enrollment",
            result="success",
            metadata_json={"user_id": user.id, "model": MODEL_NAME, "audio_persisted": False},
        ))
        db.commit()
    return {"enrolled": True, "model": MODEL_NAME, "audio_persisted": False}


@router.post("/biometric/verify")
async def verify_voice(
    request: Request,
    file: UploadFile = File(),
    user: User = Depends(require_user),
) -> dict[str, object]:
    failures: deque[float] = request.app.state.biometric_failures.setdefault(user.id, deque())
    now = time.monotonic()
    while failures and failures[0] < now - 600:
        failures.popleft()
    if len(failures) >= 5:
        raise HTTPException(429, "voice verification is temporarily rate-limited")
    audio = await _read_sample(file)
    with request.app.state.db_factory() as db:
        enrollment = db.get(VoiceEnrollment, user.id)
        if enrollment is None:
            raise HTTPException(409, "enroll a master voice before verification")
        encrypted_embedding = enrollment.encrypted_embedding
    try:
        similarity = compare_voice(audio, encrypted_embedding)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BiometricUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    threshold = request.app.state.settings.biometric_threshold
    accepted = similarity >= threshold
    if accepted:
        failures.clear()
    else:
        failures.append(now)
    _record(
        request,
        user.id,
        "speaker verification",
        "verified" if accepted else "rejected",
        {"similarity": round(similarity, 4), "threshold": threshold, "audio_persisted": False},
    )
    return {
        "verified": accepted,
        "similarity": round(similarity, 4),
        "threshold": threshold,
        "anti_spoofing": "not_configured",
    }


@router.delete("/biometric/enroll", status_code=204)
def remove_enrollment(request: Request, user: User = Depends(require_admin)):
    with request.app.state.db_factory() as db:
        enrollment = db.get(VoiceEnrollment, user.id)
        if enrollment is None:
            raise HTTPException(404, "voice enrollment not found")
        db.delete(enrollment)
        db.add(AuditLog(
            event_type="biometric.enrollment",
            source="dashboard",
            action="master voice enrollment removed",
            result="success",
            metadata_json={"user_id": user.id},
        ))
        db.commit()
