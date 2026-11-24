from __future__ import annotations

from collections import deque
from io import BytesIO
import os
import time
from uuid import uuid4
import wave

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, User, VoiceEnrollment
from app.services.biometric.anti_spoof import AntiSpoofUnavailable, bona_fide_score
from app.services.biometric.speaker import (
    BiometricUnavailable,
    MODEL_NAME,
    compare_voice,
    create_enrollment,
)
from app.services.audio.verification import (
    LAST_DEVICE_WAV,
    cancel_device_recording,
    finish_device_recording,
    get_cached_device_wav,
    start_device_recording,
)
from app.services.stt.whisper_api import validate_wav

router = APIRouter()


def _anti_spoof_model_path(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "anti_spoof_model_path", None) or os.getenv(
        "ANTI_SPOOF_MODEL_PATH", "./app/models/anti_spoof.onnx"
    )


def _anti_spoof_threshold(request: Request) -> float:
    settings = getattr(request.app.state, "settings", None)
    value = getattr(settings, "anti_spoof_threshold", None)
    return float(value if value is not None else os.getenv("ANTI_SPOOF_THRESHOLD", "0.75"))


def _anti_spoof_state(request: Request) -> str:
    try:
        bona_fide_score(b"\x00\x00" * 64_000, _anti_spoof_model_path(request))
        return "configured"
    except AntiSpoofUnavailable:
        return "not_configured"


def _wav_to_pcm16(wav_bytes: bytes, target_rate: int = 16_000) -> bytes:
    import numpy as np

    with wave.open(BytesIO(wav_bytes), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("anti-spoof requires 16-bit PCM WAV audio")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        raw = wav.readframes(wav.getnframes())

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    if rate != target_rate and samples.size > 0:
        target_count = round(samples.size * target_rate / rate)
        src_pos = np.arange(samples.size, dtype=np.float64)
        dst_pos = np.arange(target_count, dtype=np.float64) * rate / target_rate
        samples = np.interp(dst_pos, src_pos, samples)
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()


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


def _save_enrollment(
    request: Request,
    user: User,
    encrypted: str,
    source: str,
    extra_meta: dict[str, object] | None = None,
) -> None:
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
            source=source,
            action="master voice enrollment",
            result="success",
            metadata_json={
                "user_id": user.id,
                "model": MODEL_NAME,
                "audio_persisted": False,
                **(extra_meta or {}),
            },
        ))
        db.commit()


@router.get("/biometric/status")
def enrollment_status(request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        enrollment = db.get(VoiceEnrollment, user.id) or db.scalars(select(VoiceEnrollment)).first()
        ready_devices = [
            dev_id for dev_id in list(LAST_DEVICE_WAV.keys()) if get_cached_device_wav(dev_id) is not None
        ]
        return {
            "enrolled": enrollment is not None,
            "model": enrollment.model_name if enrollment else None,
            "created_at": enrollment.created_at.isoformat() if enrollment else None,
            "threshold": request.app.state.settings.biometric_threshold,
            "anti_spoofing": _anti_spoof_state(request),
            "cached_devices": ready_devices,
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
    _save_enrollment(request, user, encrypted, source="dashboard")
    return {
        "enrolled": True,
        "model": MODEL_NAME,
        "audio_persisted": False,
        "anti_spoofing": _anti_spoof_state(request),
    }


@router.post("/biometric/enroll-device/{device_id}")
def enroll_from_device(
    device_id: str,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    audio = get_cached_device_wav(device_id)
    if audio is None:
        raise HTTPException(
            409,
            f"no recent 3-20s utterance cached for device '{device_id}'; say 'Hey buddy' and speak a 4-6s sentence first",
        )
    try:
        encrypted = create_enrollment(audio)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BiometricUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    _save_enrollment(request, user, encrypted, source="device", extra_meta={"device_id": device_id})
    return {
        "enrolled": True,
        "model": MODEL_NAME,
        "source_device": device_id,
        "audio_persisted": False,
        "anti_spoofing": _anti_spoof_state(request),
    }


@router.post("/biometric/record-device/{device_id}/start")
def start_record_from_device(
    device_id: str,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    start_device_recording(device_id)
    return {"recording": True, "device_id": device_id}


@router.post("/biometric/record-device/{device_id}/finish")
def finish_record_from_device(
    device_id: str,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    try:
        audio = finish_device_recording(device_id)
        encrypted = create_enrollment(audio)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BiometricUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    _save_enrollment(request, user, encrypted, source="device_recording", extra_meta={"device_id": device_id})
    return {
        "enrolled": True,
        "model": MODEL_NAME,
        "source_device": device_id,
        "audio_persisted": False,
        "anti_spoofing": _anti_spoof_state(request),
    }


@router.post("/biometric/record-device/{device_id}/cancel")
def cancel_record_from_device(
    device_id: str,
    user: User = Depends(require_admin),
) -> dict[str, object]:
    cancel_device_recording(device_id)
    return {"recording": False, "device_id": device_id}


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
        enrollment = db.get(VoiceEnrollment, user.id) or db.scalars(select(VoiceEnrollment)).first()
        if enrollment is None:
            raise HTTPException(409, "enroll a master voice before verification")
        encrypted_embedding = enrollment.encrypted_embedding

    spoof_score: float | None = None
    spoof_status = "not_configured"
    try:
        pcm = _wav_to_pcm16(audio)
        spoof_score = bona_fide_score(pcm, _anti_spoof_model_path(request))
        spoof_threshold = _anti_spoof_threshold(request)
        if spoof_score < spoof_threshold:
            failures.append(now)
            _record(
                request,
                user.id,
                "anti-spoof verification",
                "rejected",
                {
                    "bona_fide_score": round(spoof_score, 4),
                    "anti_spoof_threshold": spoof_threshold,
                    "audio_persisted": False,
                },
            )
            return {
                "verified": False,
                "similarity": 0.0,
                "threshold": request.app.state.settings.biometric_threshold,
                "anti_spoofing": "rejected",
                "bona_fide_score": round(spoof_score, 4),
            }
        spoof_status = "passed"
    except AntiSpoofUnavailable:
        spoof_status = "not_configured"
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

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
    metadata: dict[str, object] = {
        "similarity": round(similarity, 4),
        "threshold": threshold,
        "anti_spoofing": spoof_status,
        "audio_persisted": False,
    }
    if spoof_score is not None:
        metadata["bona_fide_score"] = round(spoof_score, 4)
    _record(
        request,
        user.id,
        "speaker verification",
        "verified" if accepted else "rejected",
        metadata,
    )
    response: dict[str, object] = {
        "verified": accepted,
        "similarity": round(similarity, 4),
        "threshold": threshold,
        "anti_spoofing": spoof_status,
    }
    if spoof_score is not None:
        response["bona_fide_score"] = round(spoof_score, 4)
    return response


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
