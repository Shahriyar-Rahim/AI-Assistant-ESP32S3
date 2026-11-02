from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.dependencies import require_user
from app.models import AuditLog, User
from app.services.stt.whisper_api import SpeechServiceError, transcribe_wav, validate_wav
from app.services.tts.openai_api import TTSServiceError, synthesize_pcm

logger = logging.getLogger(__name__)
router = APIRouter()


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = "alloy"
    model: str = "gpt-4o-mini-tts"


@router.post("/audio/transcriptions")
async def transcribe(
    request: Request,
    file: Annotated[UploadFile, File()],
    language: str = Query(default="en"),
    user: User = Depends(require_user),
) -> dict[str, object]:
    if file.content_type not in {"audio/wav", "audio/x-wav"}:
        raise HTTPException(415, "upload a WAV file")
    max_bytes = int(os.getenv("MAX_AUDIO_UPLOAD_BYTES", "5242880"))
    audio = await file.read(max_bytes + 1)
    await file.close()
    try:
        validate_wav(audio, max_bytes=max_bytes)
        transcript, metadata = await transcribe_wav(audio, language=language, model="whisper-1")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SpeechServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            id=str(uuid4()),
            event_type="audio.transcription",
            source="dashboard",
            action="speech transcription",
            result="success",
            metadata_json={"user_id": user.id, **metadata, "transcript_characters": len(transcript)},
        ))
        db.commit()
    return {"text": transcript, "language": language}


@router.post("/audio/speech")
async def synthesize(
    body: SynthesisRequest,
    request: Request,
    user: User = Depends(require_user),
) -> Response:
    try:
        audio = await synthesize_pcm(body.text, body.model, body.voice)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except TTSServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            event_type="tts.generated",
            source="dashboard",
            action="speech synthesis",
            result="success",
            metadata_json={"user_id": user.id, "provider": "openai", "model": body.model, "voice": body.voice},
        ))
        db.commit()
    return Response(
        audio,
        media_type="application/octet-stream",
        headers={
            "X-Audio-Format": "pcm_s16le",
            "X-Audio-Sample-Rate": "16000",
            "X-Audio-Channels": "1",
            "X-Audio-Timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
