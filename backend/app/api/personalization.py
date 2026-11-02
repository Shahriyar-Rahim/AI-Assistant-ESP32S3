from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.api.dependencies import require_user
from app.models import AuditLog, User, VoiceInteraction, VoiceLearningProfile
from app.services.assistant.personalization import PersonalizationUnavailable, _cipher

router = APIRouter()


class PersonalizationUpdate(BaseModel):
    enabled: bool


@router.get("/assistant/personalization")
def personalization_status(
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        profile = db.get(VoiceLearningProfile, user.id)
        stored_count = db.scalar(
            select(func.count()).select_from(VoiceInteraction).where(VoiceInteraction.user_id == user.id)
        ) or 0
    return {
        "enabled": profile.enabled if profile else False,
        "interaction_count": profile.interaction_count if profile else 0,
        "stored_audio_count": stored_count,
        "audio_retention_days": request.app.state.settings.personalization_audio_retention_days,
        "encryption_configured": bool(request.app.state.settings.voice_encryption_key),
    }


@router.put("/assistant/personalization")
def update_personalization(
    body: PersonalizationUpdate,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    if body.enabled:
        try:
            _cipher(request.app.state.settings)
        except PersonalizationUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    with request.app.state.db_factory() as db:
        profile = db.get(VoiceLearningProfile, user.id)
        if profile is None:
            profile = VoiceLearningProfile(user_id=user.id)
            db.add(profile)
        profile.enabled = body.enabled
        profile.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            event_type="voice.personalization",
            source="dashboard",
            action="voice personalization setting changed",
            result="success",
            metadata_json={"user_id": user.id, "enabled": body.enabled},
        ))
        db.commit()
    return {"enabled": body.enabled}


@router.delete("/assistant/personalization/history", status_code=204)
def clear_personalization_history(
    request: Request,
    user: User = Depends(require_user),
) -> Response:
    with request.app.state.db_factory() as db:
        db.execute(delete(VoiceInteraction).where(VoiceInteraction.user_id == user.id))
        old_transcriptions = db.execute(
            select(AuditLog).where(AuditLog.event_type == "voice.transcription")
        ).scalars().all()
        for entry in old_transcriptions:
            if entry.metadata_json.get("user_id") == user.id and "transcript" in entry.metadata_json:
                entry.metadata_json = {
                    key: value for key, value in entry.metadata_json.items() if key != "transcript"
                }
        profile = db.get(VoiceLearningProfile, user.id)
        if profile is not None:
            profile.interaction_count = 0
            profile.mean_words = 0
            profile.language_counts = {}
            profile.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            event_type="voice.personalization",
            source="dashboard",
            action="voice learning history deleted",
            result="success",
            metadata_json={"user_id": user.id},
        ))
        db.commit()
    return Response(status_code=204)
