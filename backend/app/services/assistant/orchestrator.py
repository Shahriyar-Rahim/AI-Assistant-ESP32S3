from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select

from app.models import (
    AuditLog,
    CalendarEvent,
    Device,
    ProviderConfig,
    Reminder,
    Todo,
    VoiceEnrollment,
)
from app.services.ai.providers import PROVIDERS, ProviderRequestError, ProviderUnavailable
from app.services.assistant.actions import (
    ACTION_REGISTRY,
    ActionContext,
    IntentError,
    deterministic_intent,
    execute_action,
)
from app.services.assistant.personalization import (
    PersonalizationUnavailable,
    learned_instructions,
    record_interaction,
)
from app.services.audio.verification import ReplayGuard, verify_command_audio
from app.services.biometric.anti_spoof import AntiSpoofUnavailable
from app.services.biometric.speaker import BiometricUnavailable
from app.services.stt.local_whisper import LocalSpeechUnavailable, transcribe_pcm
from app.services.stt.whisper_api import SpeechServiceError, transcribe_wav
from app.services.tts.openai_api import TTSServiceError, synthesize_pcm

logger = logging.getLogger(__name__)
_REPLAY_GUARDS: dict[str, ReplayGuard] = {}
MAX_COMMAND_BYTES = 16_000 * 2 * 20
TTS_CHUNK_BYTES = 320 * 2


def _wav(pcm: bytes) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(pcm)
    return output.getvalue()


def _audit(application, device_id: str, action: str, result: str, metadata: dict[str, Any]) -> None:
    with application.state.db_factory() as db:
        db.add(AuditLog(
            event_type="voice.pipeline",
            source="device",
            device_id=device_id,
            action=action,
            result=result,
            metadata_json=metadata,
        ))
        db.commit()


async def _transcribe(application, pcm: bytes) -> tuple[str, dict[str, object]]:
    settings = application.state.settings
    language = settings.stt_language
    if settings.stt_provider == "local":
        return await transcribe_pcm(pcm, language=language)
    if settings.stt_provider == "openai":
        return await transcribe_wav(_wav(pcm), language=language, model="whisper-1")
    raise SpeechServiceError(f"unsupported configured STT provider: {settings.stt_provider}")


async def _ai_fallback(
    application,
    transcript: str,
    user_id: str,
) -> tuple[str, tuple[str, dict[str, Any]] | None]:
    with application.state.db_factory() as db:
        active = db.execute(select(ProviderConfig).where(ProviderConfig.active.is_(True))).scalar_one_or_none()
        provider_name = active.provider if active else application.state.settings.default_ai_provider
        model = active.model if active else application.state.settings.ai_model
        system_prompt = active.settings.get("system_prompt", "You are a helpful voice assistant.") if active else "You are a helpful voice assistant."
    provider = PROVIDERS.get(provider_name)
    if provider is None or not model:
        return "I could not match that request to a safe available action.", None
    action_schemas = ACTION_REGISTRY.schemas()
    personalization = learned_instructions(application.state.db_factory, user_id=user_id)
    prompt = (
        "Resolve the user request. Return one JSON object only: "
        '{"kind":"action","name":"<registered action>","arguments":{...}} '
        'or {"kind":"reply","text":"..."}. Never invent action names, credentials, or arguments. '
        f"Registered actions and exact argument schemas: {json.dumps(action_schemas, separators=(',', ':'))}"
    )
    try:
        result = await provider.complete(
            [
                {"role": "system", "content": f"{system_prompt}\n{personalization}\n{prompt}"},
                {"role": "user", "content": transcript},
            ],
            model,
        )
    except (ProviderUnavailable, ProviderRequestError) as exc:
        logger.warning("Voice AI resolution failed: %s", exc)
        return "The assistant service is not available right now.", None
    try:
        response = json.loads(result.text)
    except json.JSONDecodeError:
        return result.text.strip() or "I could not resolve that request.", None
    if not isinstance(response, dict) or response.get("kind") not in {"action", "reply"}:
        return "The assistant returned an invalid response.", None
    if response["kind"] == "reply":
        reply = response.get("text")
        return (reply[:1000] if isinstance(reply, str) else "I could not resolve that request."), None
    name = response.get("name")
    arguments = response.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict) or name not in ACTION_REGISTRY.names():
        return "I cannot perform that action.", None
    try:
        ACTION_REGISTRY.validate(name, arguments)
    except IntentError:
        return "I could not safely validate that action.", None
    return "", (name, arguments)


async def _resolve(application, device_id: str, user_id: str, transcript: str, websocket=None) -> str:
    intent = deterministic_intent(transcript)
    reply = ""
    if intent is None:
        reply, intent = await _ai_fallback(application, transcript, user_id)
    if intent is not None:
        action, arguments = intent
        try:
            result = await execute_action(
                ActionContext(application=application, device_id=device_id, user_id=user_id),
                action,
                arguments,
            )
            display = result.get("display")
            if isinstance(display, dict) and websocket is not None:
                await _send_json(websocket, device_id, "display.state", display)
            reply = result.get("speech", "Done.")
        except IntentError as exc:
            reply = str(exc)
        except Exception:
            logger.exception("Voice action %s failed for device %s", action, device_id)
            reply = "I could not complete that action."
    return reply[:1000]


def _daily_briefing(application, user_id: str) -> str:
    now = datetime.now(timezone.utc)
    owner_scope = or_(Reminder.created_by == user_id, Reminder.created_by.is_(None))
    with application.state.db_factory() as db:
        reminder_count = db.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.completed.is_(False),
                owner_scope,
            )
        ) or 0
        reminders = db.execute(
            select(Reminder)
            .where(Reminder.completed.is_(False), owner_scope)
            .order_by(Reminder.due_at)
            .limit(3)
        ).scalars().all()
        pending_todos = db.scalar(
            select(func.count()).select_from(Todo).where(
                Todo.completed.is_(False),
                or_(Todo.created_by == user_id, Todo.created_by.is_(None)),
            )
        ) or 0
        upcoming_events = db.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.completed.is_(False),
                CalendarEvent.start_at >= now,
                or_(CalendarEvent.created_by == user_id, CalendarEvent.created_by.is_(None)),
            )
            .order_by(CalendarEvent.start_at)
            .limit(2)
        ).scalars().all()

    parts = ["Here is your briefing."]
    if reminders:
        parts.append(f"You have {reminder_count} active reminders.")
        first = reminders[0]
        due_at = first.due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        parts.append(f"Next is {first.title}, at {due_at.astimezone(timezone.utc).strftime('%A %H:%M UTC')}.")
    else:
        parts.append("You have no active reminders.")
    parts.append(f"You have {pending_todos} pending to-do items.")
    if upcoming_events:
        event = upcoming_events[0]
        start_at = event.start_at
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        parts.append(
            f"Your next calendar item is {event.title}, "
            f"{start_at.astimezone(timezone.utc).strftime('%A %H:%M UTC')}."
        )
    else:
        parts.append("There are no upcoming calendar events.")
    return " ".join(parts)


async def _send_json(websocket, device_id: str, message_type: str, payload: dict[str, Any]) -> None:
    await websocket.send_json({
        "protocol_version": 1,
        "message_id": str(uuid4()),
        "device_id": device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": message_type,
        "payload": payload,
    })


async def _speak(application, websocket, device_id: str, text: str) -> None:
    if not text:
        return
    await _send_json(websocket, device_id, "assistant.response", {"text": text})
    try:
        pcm = await synthesize_pcm(text, "gpt-4o-mini-tts", "alloy")
    except (TTSServiceError, ValueError) as exc:
        logger.warning("Voice response TTS unavailable for device %s: %s", device_id, exc)
        await _send_json(websocket, device_id, "device.error", {
            "code": "tts_unavailable",
            "detail": str(exc),
        })
        return
    if not pcm or len(pcm) % 2:
        raise ValueError("TTS adapter returned invalid PCM16 data")
    end_at = time.monotonic() + (len(pcm) / (16_000 * 2)) + 0.75
    application.state.audio_echo_until[device_id] = end_at
    playback_id = str(uuid4())
    await _send_json(websocket, device_id, "tts.start", {
        "playback_id": playback_id,
        "sample_rate": 16_000,
        "channels": 1,
        "encoding": "pcm_s16le",
        "samples": len(pcm) // 2,
    })
    for sequence, offset in enumerate(range(0, len(pcm), TTS_CHUNK_BYTES)):
        chunk = pcm[offset:offset + TTS_CHUNK_BYTES]
        if len(chunk) % 2:
            raise ValueError("TTS chunk split a PCM16 sample")
        await websocket.send_bytes(chunk)
        await _send_json(websocket, device_id, "tts.chunk", {
            "playback_id": playback_id,
            "sequence": sequence,
            "sample_offset": offset // 2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    await _send_json(websocket, device_id, "tts.end", {
        "playback_id": playback_id,
        "chunks": (len(pcm) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES,
    })
    _audit(application, device_id, "tts.playback", "success", {
        "playback_id": playback_id,
        "duration_ms": round(len(pcm) / 32),
    })


async def speak_wake_greeting(application, websocket, device_id: str) -> None:
    await _speak(
        application,
        websocket,
        device_id,
        "Hi, I'm Buddy. Good to hear you. I'm listening now. What would you like to know?",
    )


async def process_command_audio(application, websocket, device_id: str, pcm: bytes) -> None:
    if len(pcm) > MAX_COMMAND_BYTES:
        await _send_json(websocket, device_id, "device.error", {"code": "command_audio_too_large"})
        return
    settings = application.state.settings
    guard = _REPLAY_GUARDS.setdefault(device_id, ReplayGuard())
    await _send_json(websocket, device_id, "assistant.response", {"state": "verifying"})
    verification = await asyncio.to_thread(
        verify_command_audio,
        application.state.db_factory,
        device_id=device_id,
        pcm=pcm,
        threshold=settings.biometric_threshold,
        replay_guard=guard,
        anti_spoof_model_path=settings.anti_spoof_model_path,
        anti_spoof_threshold=settings.anti_spoof_threshold,
    )
    if not verification.accepted or not verification.user_id:
        _audit(application, device_id, "voice.verification", "rejected", {
            "reason": verification.reason,
            "confidence": verification.confidence,
        })
        await _send_json(websocket, device_id, "biometric.result", {
            "accepted": False,
            "reason": verification.reason,
            "confidence": verification.confidence,
        })
        return
    await _send_json(websocket, device_id, "biometric.result", {
        "accepted": True,
        "confidence": verification.confidence,
    })
    try:
        transcript, stt_metadata = await _transcribe(application, pcm)
    except (SpeechServiceError, LocalSpeechUnavailable, ValueError) as exc:
        _audit(application, device_id, "voice.transcription", "failed", {"reason": str(exc)})
        await _send_json(websocket, device_id, "device.error", {
            "code": "stt_unavailable_or_failed",
            "detail": str(exc),
        })
        return
    wake_phrase = settings.wake_word.strip()
    if wake_phrase and transcript.casefold().startswith(wake_phrase.casefold()):
        transcript = transcript[len(wake_phrase):].lstrip(" ,.!?")
    if not transcript:
        await _send_json(websocket, device_id, "device.error", {"code": "empty_transcript"})
        return
    _audit(application, device_id, "voice.transcription", "success", {
        "provider": stt_metadata["provider"],
        "model": stt_metadata["model"],
        "language": stt_metadata["language"],
        "transcript_characters": len(transcript),
        "user_id": verification.user_id,
    })
    await _send_json(websocket, device_id, "audio.transcription", {
        "text": transcript,
        "language": stt_metadata["language"],
    })
    try:
        await asyncio.to_thread(
            record_interaction,
            application.state.db_factory,
            settings,
            user_id=verification.user_id,
            pcm=pcm,
            transcript=transcript,
            language=str(stt_metadata["language"]),
        )
    except PersonalizationUnavailable as exc:
        logger.error("Personalized voice history was not stored: %s", exc)
        await _send_json(websocket, device_id, "device.error", {
            "code": "personalization_storage_unavailable",
        })
    reply = await _resolve(application, device_id, verification.user_id, transcript, websocket)
    wake_at = application.state.wake_briefings.pop(device_id, None)
    if wake_at is not None and time.monotonic() - wake_at <= 120:
        reply = f"{_daily_briefing(application, verification.user_id)} {reply}"
    _audit(application, device_id, "voice.response", "success", {"user_id": verification.user_id})
    await _speak(application, websocket, device_id, reply)


def is_echo_guarded(application, device_id: str) -> bool:
    return application.state.audio_echo_until.get(device_id, 0.0) > time.monotonic()
