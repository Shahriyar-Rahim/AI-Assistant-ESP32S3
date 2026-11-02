from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.models import VoiceInteraction, VoiceLearningProfile


class PersonalizationUnavailable(RuntimeError):
    pass


def _cipher(settings):
    key = settings.voice_encryption_key
    if not key:
        raise PersonalizationUnavailable("VOICE_ENCRYPTION_KEY is required for encrypted voice retention")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except (ImportError, ValueError, TypeError) as exc:
        raise PersonalizationUnavailable("VOICE_ENCRYPTION_KEY must be a valid Fernet key") from exc


def record_interaction(
    db_factory,
    settings,
    *,
    user_id: str,
    pcm: bytes,
    transcript: str,
    language: str,
) -> bool:
    with db_factory() as db:
        profile = db.get(VoiceLearningProfile, user_id)
        if profile is None or not profile.enabled:
            return False
        cipher = _cipher(settings)
        encrypted_audio = cipher.encrypt(pcm).decode()
        encrypted_transcript = cipher.encrypt(transcript.encode()).decode()
        db.add(VoiceInteraction(
            user_id=user_id,
            encrypted_audio=encrypted_audio,
            encrypted_transcript=encrypted_transcript,
            language=language[:16],
        ))
        words = len(transcript.split())
        count = profile.interaction_count + 1
        profile.mean_words = profile.mean_words + (words - profile.mean_words) / count
        profile.interaction_count = count
        language_counts = dict(profile.language_counts)
        language_counts[language[:16]] = language_counts.get(language[:16], 0) + 1
        profile.language_counts = language_counts
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.personalization_audio_retention_days,
        )
        db.execute(delete(VoiceInteraction).where(
            VoiceInteraction.user_id == user_id,
            VoiceInteraction.created_at < cutoff,
        ))
        db.commit()
        return True


def learned_instructions(db_factory, *, user_id: str) -> str:
    with db_factory() as db:
        profile = db.get(VoiceLearningProfile, user_id)
        if profile is None or not profile.enabled or profile.interaction_count == 0:
            return ""
        language = max(profile.language_counts, key=profile.language_counts.get, default="")
        instructions = [
            "Adapt delivery using the user's learned interaction patterns, without inferring sensitive traits.",
        ]
        if profile.mean_words < 8:
            instructions.append("The user tends to make brief requests; answer directly and concisely.")
        elif profile.mean_words >= 20:
            instructions.append("The user tends to give detailed requests; provide a structured, sufficiently detailed answer.")
        if language == "bn":
            instructions.append("The user's most frequent spoken language is Bengali; prefer Bengali unless the current request indicates otherwise.")
        elif language == "en":
            instructions.append("The user's most frequent spoken language is English; prefer English unless the current request indicates otherwise.")
        return " ".join(instructions)


async def voice_retention_loop(application) -> None:
    import asyncio

    while True:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=application.state.settings.personalization_audio_retention_days,
        )
        with application.state.db_factory() as db:
            db.execute(delete(VoiceInteraction).where(VoiceInteraction.created_at < cutoff))
            db.commit()
        await asyncio.sleep(3600)
