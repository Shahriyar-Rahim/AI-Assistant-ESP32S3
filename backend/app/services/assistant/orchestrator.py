from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
    User,
    VoiceEnrollment,
)
from app.services.ai.providers import PROVIDERS, ProviderRequestError, ProviderUnavailable
from app.services.assistant.actions import (
    ACTION_REGISTRY,
    ActionContext,
    IntentError,
    GAME_ALIASES,
    apply_voice_game_move,
    deterministic_intent,
    execute_action,
)
from app.services import command_repair, game_history, knowledge, local_brain
from app.services.speech_text import clean_for_speech
from app.services.assistant.presenter import go_home_after_reply, route_alarm_followup, route_calendar_followup, route_dashboard_request, route_note_followup, route_reminder_followup, route_todo_followup, run_boot_briefing, run_wake_greeting, show_stored_page
from app.services.assistant.device_games import route_device_game
from app.services.assistant.setup_commands import route_setup_command
from app.services.vision import commands as vision_commands
from app.services.assistant.personalization import (
    PersonalizationUnavailable,
    learned_instructions,
    record_interaction,
)
from app.services.assistant import private_memory
from app.services.audio.verification import ReplayGuard, verify_command_audio
from app.services.biometric.anti_spoof import AntiSpoofUnavailable
from app.services.biometric.speaker import BiometricUnavailable
from app.services.stt.local_whisper import LocalSpeechUnavailable, transcribe_pcm
from app.services.stt.whisper_api import SpeechServiceError, transcribe_wav
from app.services.tts.gemini import synthesize_gemini_pcm
from app.services.tts.local import synthesize_local_pcm
from app.services.tts.openai_api import TTSServiceError, synthesize_pcm

logger = logging.getLogger(__name__)
_REPLAY_GUARDS: dict[str, ReplayGuard] = {}
MAX_COMMAND_BYTES = 16_000 * 2 * 20
TTS_CHUNK_BYTES = 320 * 2
TTS_LEAD_SECONDS = 0.5
TTS_SAMPLE_RATE = 16_000


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


_NOISE_TRANSCRIPTS = {
    "you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay", "ok",
    "hmm", "uh", "um", "oh", "ah", "so", "the", "i",
}


def _is_noise_transcript(transcript: str) -> bool:
    cleaned = re.sub(r"[^a-z' ]", " ", transcript.casefold()).strip()
    cleaned = " ".join(cleaned.split())
    if transcript.strip().startswith(("[", "(", "*")) and transcript.strip().endswith(("]", ")", "*")):
        return True
    return len(cleaned) < 3 or cleaned in _NOISE_TRANSCRIPTS or bool(re.fullmatch(r"(?:clap ?|click ?|tap ?|knock ?)+", cleaned))


async def _transcribe(application, pcm: bytes) -> tuple[str, dict[str, object]]:
    settings = application.state.settings
    language = settings.stt_language
    if settings.stt_provider == "local":
        return await transcribe_pcm(pcm, language=language)
    if settings.stt_provider == "openai":
        try:
            return await transcribe_wav(_wav(pcm), language=language, model="whisper-1")
        except SpeechServiceError as exc:
            logger.warning("OpenAI STT failed (%s); falling back to local Whisper", exc)
            return await transcribe_pcm(pcm, language=language)
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
    ollama_model = getattr(application.state.settings, "ollama_fallback_model", "")
    if (provider is None or not model) and not ollama_model:
        return "I could not match that request to a safe available action.", None
    action_schemas = ACTION_REGISTRY.schemas()
    personalization = learned_instructions(application.state.db_factory, user_id=user_id)
    game_context = await asyncio.to_thread(game_history.prompt_context, application.state.db_factory, user_id)
    if game_context:
        personalization = f"{personalization}\n{game_context}".strip()
    prompt = (
        "Resolve the user request. Return one JSON object only: "
        '{"kind":"action","name":"<registered action>","arguments":{...}} '
        'or {"kind":"reply","text":"..."}. Never invent action names, credentials, or arguments. '
        f"Registered actions and exact argument schemas: {json.dumps(action_schemas, separators=(',', ':'))}"
        f" Current UTC date and time: {datetime.now(timezone.utc).isoformat()}."
    )
    messages = [
        {"role": "system", "content": f"{system_prompt}\n{personalization}\n{prompt}\n"
         "Replies are spoken aloud on a small speaker: write plain sentences, at most three, with no markdown, "
         "asterisks, bullet points, emoji, code or URLs."},
        {"role": "user", "content": transcript},
    ]
    fallback_model = getattr(application.state.settings, "ai_fallback_model", "")
    result = None
    attempts = []
    if provider is not None and model:
        attempts.extend((provider_name, provider, item) for item in dict.fromkeys(
            item for item in (model, fallback_model if provider_name == "gemini" else "") if item
        ))
    if ollama_model and (provider_name != "ollama" or model != ollama_model):
        attempts.append(("ollama", PROVIDERS["ollama"], ollama_model))
    for attempt_provider_name, attempt_provider, attempt_model in attempts:
        try:
            result = await attempt_provider.complete(messages, attempt_model)
            break
        except (ProviderUnavailable, ProviderRequestError) as exc:
            logger.warning("Voice AI resolution failed with %s/%s: %s", attempt_provider_name, attempt_model, exc)
    if result is None:
        return "The assistant service is not available right now.", None
    try:
        response = json.loads(result.text)
    except json.JSONDecodeError:
        return clean_for_speech(result.text) or "I could not resolve that request.", None
    if not isinstance(response, dict) or response.get("kind") not in {"action", "reply"}:
        return "The assistant returned an invalid response.", None
    if response["kind"] == "reply":
        reply = response.get("text")
        return (clean_for_speech(reply, 1000) if isinstance(reply, str) else "") or "I could not resolve that request.", None
    name = response.get("name")
    arguments = response.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict) or name not in ACTION_REGISTRY.names():
        return "I cannot perform that action.", None
    try:
        ACTION_REGISTRY.validate(name, arguments)
    except IntentError:
        return "I could not safely validate that action.", None
    return "", (name, arguments)


_GAME_STATS = re.compile(
    r"\b(?:how am i doing|how did i do|how good am i|how many games|my game (?:stats|statistics|history|score|scores|record)|"
    r"game (?:stats|statistics|history)|win rate|which game do i (?:play|win)|what do you know about my games|"
    r"my (?:wins|losses|streak)|have i (?:won|lost|played))\b.*"
)


_AI_ERRORS = frozenset({
    "The assistant service is not available right now.",
    "I could not match that request to a safe available action.",
    "The assistant returned an invalid response.",
    "I cannot perform that action.",
    "I could not safely validate that action.",
    "I could not resolve that request.",
})


async def _local_or_cloud(application, transcript: str, user_id: str):
    """Returns (cloud_reply, cloud_intent, local_reply); local_reply is set when something local answered."""
    factory = application.state.db_factory
    settings = application.state.settings
    fact = await asyncio.to_thread(local_brain.fact_command, factory, user_id, transcript)
    if fact:
        return "", None, fact
    name = await asyncio.to_thread(local_brain.user_name, factory, user_id)
    chat = local_brain.small_talk(transcript, name)
    if chat:
        return "", None, chat
    exact = knowledge.calculate(transcript) or knowledge.convert_units(transcript)
    if exact:
        return "", None, exact
    known = knowledge.lookup_fact(transcript)
    if known:
        return "", None, known
    if getattr(settings, "local_brain_first", True):
        hit = await asyncio.to_thread(
            local_brain.find, factory, user_id, transcript, ("reply",), local_brain.LOCAL_FIRST_THRESHOLD)
        if hit:
            await asyncio.to_thread(local_brain.mark_used, factory, hit[0].id)
            return "", None, str(hit[0].response["text"])
    reply, intent = await _ai_fallback(application, transcript, user_id)
    if reply in _AI_ERRORS and intent is None:
        hit = await asyncio.to_thread(
            local_brain.find, factory, user_id, transcript, ("reply",), local_brain.OFFLINE_THRESHOLD)
        if hit:
            await asyncio.to_thread(local_brain.mark_used, factory, hit[0].id)
            return "", None, str(hit[0].response["text"])
        loose = knowledge.lookup_fact(transcript, 0.5)
        if loose:
            return "", None, loose
        online = await knowledge.web_answer(transcript)
        if online:
            await asyncio.to_thread(local_brain.learn_reply, factory, user_id, transcript, online, "web")
            return "", None, online
        return (
            _NOT_UNDERSTOOD + " I can still do math, unit conversions, "
            "simple facts, games, alarms, reminders and tasks. Please try the question again in a moment.", None, None)
    if intent is not None:
        await asyncio.to_thread(local_brain.learn_action, factory, user_id, transcript, intent[0], intent[1])
    elif reply:
        await asyncio.to_thread(local_brain.learn_reply, factory, user_id, transcript, reply)
    return reply, intent, None


def _is_game_stats_question(lowered: str) -> bool:
    if not _GAME_STATS.search(lowered):
        return False
    if re.search(r"\bhow am i doing\b|\bhow did i do\b|\bhow good am i\b", lowered):
        return bool(re.search(r"\b(?:game|games|play|playing|tic|hangman|trivia|rock|number|word)\b", lowered))
    return True


def _mentioned_game(lowered: str) -> str | None:
    for spoken, game in {**GAME_ALIASES, "number guessing": "number_guessing"}.items():
        if spoken in lowered:
            return game
    return None


def _device_timezone(application, device_id: str):
    name = None
    with application.state.db_factory() as db:
        device = db.get(Device, device_id)
        if device is not None:
            location = device.config.get("location") or {}
            name = location.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("Device %s has invalid timezone %r; using server timezone", device_id, name)
    server_zone = os.environ.get("TZ", "").lstrip(":")
    if not server_zone:
        try:
            server_zone = str(Path("/etc/localtime").resolve().relative_to("/usr/share/zoneinfo"))
        except ValueError:
            server_zone = ""
    if server_zone:
        try:
            return ZoneInfo(server_zone)
        except (ValueError, ZoneInfoNotFoundError):
            logger.warning("Server timezone %r is not a valid IANA timezone", server_zone)
    return datetime.now().astimezone().tzinfo


_NOT_UNDERSTOOD = "My cloud brain is busy and I could not find that online."
_YES_WORDS = re.compile(r"^(?:yes|yeah|yep|yup|correct|right|that one|do it|sure|ok|okay)\b")
SUGGESTION_SECONDS = 20.0


def _volume_number(text: str) -> int | None:
    if text.isdecimal():
        return int(text)
    units = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
             "eighteen": 18, "nineteen": 19}
    tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
            "seventy": 70, "eighty": 80, "ninety": 90}
    words = text.replace("-", " ").split()
    if len(words) == 1:
        return units.get(words[0], tens.get(words[0]))
    if len(words) == 2 and words[0] == "one" and words[1] == "hundred":
        return 100
    if len(words) == 2 and words[0] in tens and words[1] in units and 1 <= units[words[1]] <= 9:
        return tens[words[0]] + units[words[1]]
    return None


async def _resolve(application, device_id: str, user_id: str, transcript: str, websocket=None) -> str:
    if websocket is not None:
        followup = await route_alarm_followup(application, device_id, user_id, transcript)
        if followup is not None:
            if followup.startswith("Okay, alarm set"):
                from app.services.assistant.presenter import _sync_created_schedule
                if not await _sync_created_schedule(application, device_id, websocket):
                    followup += " The device schedule was not sent."
            await show_stored_page(application, websocket, device_id, "alarms", user_id)
            return followup
        followup = await route_calendar_followup(application, device_id, user_id, transcript)
        if followup is not None:
            await show_stored_page(application, websocket, device_id, "calendar", user_id)
            return followup
        followup = await route_reminder_followup(application, device_id, user_id, transcript)
        if followup is not None:
            await show_stored_page(application, websocket, device_id, "reminders", user_id)
            return followup
        followup = await route_note_followup(application, device_id, user_id, transcript)
        if followup is not None:
            await show_stored_page(application, websocket, device_id, "notes", user_id)
            return followup
        followup = await route_todo_followup(application, device_id, user_id, transcript)
        if followup is not None:
            await show_stored_page(application, websocket, device_id, "todos", user_id)
            return followup
    heard = transcript
    pending = application.state.pending_suggestions.pop(device_id, None)
    game_letter = (application.state.device_games.get(device_id)
                   and re.fullmatch(r"[a-zA-Z][.!?]?", transcript.strip()))
    if not game_letter and pending is not None and pending[1] > time.monotonic() and _YES_WORDS.match(transcript.casefold().strip()):
        transcript = pending[0]
        await asyncio.to_thread(command_repair.learn_alias, application.state.db_factory, user_id, pending[2], transcript)
        logger.info("Learned that %r means %r", pending[2], transcript)
    elif not game_letter:
        transcript, how = await asyncio.to_thread(command_repair.repair, application.state.db_factory, user_id, heard)
        if how:
            logger.info("Repaired heard command %r -> %r (%s)", heard, transcript, how)
            if how == "sound-alike":
                await asyncio.to_thread(command_repair.learn_alias, application.state.db_factory, user_id, heard, transcript)
    reply = await _resolve_command(application, device_id, user_id, transcript, websocket)
    if reply.startswith(_NOT_UNDERSTOOD) and heard == transcript:
        suggestion = command_repair.suggestion_prompt(heard)
        if suggestion is not None:
            application.state.pending_suggestions[device_id] = (
                suggestion[1], time.monotonic() + SUGGESTION_SECONDS, heard)
            application.state.relisten[device_id] = True
            return suggestion[0]
    if reply.startswith(_NOT_UNDERSTOOD) or reply.rstrip().endswith("?"):
        # A clarification or miss should be answerable without repeating the wake word.
        application.state.relisten[device_id] = True
    return reply


async def _resolve_command(application, device_id: str, user_id: str, transcript: str, websocket=None) -> str:
    setup_reply = await route_setup_command(application, websocket, device_id, transcript)
    if setup_reply is not None:
        return setup_reply
    vision_reply = await vision_commands.route(application, device_id, user_id, transcript)
    if vision_reply is not None:
        return vision_reply
    game_reply = await route_device_game(application, websocket, device_id, transcript, user_id)
    if game_reply is not None:
        return game_reply
    lowered = transcript.casefold()
    volume_request = re.fullmatch(
        r"\s*(?:set|change)\s+(?:the\s+)?volume\s+(?:to\s+)?(?:level\s+)?([a-z0-9 -]+?)(?:\s*(?:percent|%))?\s*[.!?]*\s*",
        lowered,
    )
    if volume_request:
        volume = _volume_number(volume_request.group(1).strip())
        if volume is None or not 0 <= volume <= 100:
            return "Volume must be between zero and one hundred percent."
        from fastapi import HTTPException
        from app.api.devices import set_device_config

        def is_admin() -> bool:
            with application.state.db_factory() as db:
                user = db.get(User, user_id)
                return user is not None and user.active and user.role == "admin"

        if not await asyncio.to_thread(is_admin):
            return "Only an administrator can change the device volume."
        try:
            result = await set_device_config(application, device_id, {"volume": volume}, user_id)
        except HTTPException as exc:
            logger.warning("Cannot set volume for %s: %s", device_id, exc.detail)
            return "I couldn't set the device volume."
        if result["delivery"] != "sent":
            return f"I saved volume at {volume} percent. It will apply when the device reconnects."
        return f"I sent the volume setting of {volume} percent to the device."
    if _is_game_stats_question(lowered):
        return await asyncio.to_thread(
            game_history.spoken_summary, application.state.db_factory, user_id, _mentioned_game(lowered),
        )
    zone = await asyncio.to_thread(_device_timezone, application, device_id)
    if re.search(r"\bwhat(?:'s| is)? the (?:current )?time\b|\bwhat time is it\b|\btell me the time\b|\bcurrent time\b", lowered):
        return datetime.now(zone).strftime("It is %I:%M %p.").replace("It is 0", "It is ")
    if re.search(r"\bwhat(?:'s| is)? (?:the )?(?:date|day)\b|\btoday's date\b", lowered):
        return datetime.now(zone).strftime("Today is %A, %B %d.")
    dashboard_reply = await route_dashboard_request(application, websocket, device_id, user_id, transcript)
    if dashboard_reply is not None:
        return dashboard_reply[:1000]
    intent = deterministic_intent(transcript)
    reply = ""
    if intent is None:
        game_result = apply_voice_game_move(
            ActionContext(application=application, device_id=device_id, user_id=user_id),
            transcript,
        )
        if game_result is not None:
            if websocket is not None:
                if game_result["display"] is not None:
                    await _send_json(websocket, device_id, "game.state", game_result["display"])
                if game_result.get("sound_effect"):
                    await _send_json(websocket, device_id, "sound.effect", {
                        "effect": game_result["sound_effect"],
                    })
            return str(game_result["speech"])[:1000]
        reply, intent, local = await _local_or_cloud(application, transcript, user_id)
        if local is not None:
            return local[:1000]
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
                message_type = "game.state" if result.get("game") else "display.state"
                await _send_json(websocket, device_id, message_type, display)
            sound_effect = result.get("sound_effect")
            if isinstance(sound_effect, str) and websocket is not None:
                await _send_json(websocket, device_id, "sound.effect", {"effect": sound_effect})
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
    text = clean_for_speech(text, 1000)
    if not text:
        return
    await _send_json(websocket, device_id, "assistant.response", {"text": text})
    settings = application.state.settings
    if settings.tts_provider == "local":
        try:
            pcm = await synthesize_local_pcm(text, settings.stt_language)
        except TTSServiceError as local_error:
            logger.warning("Local voice response TTS unavailable for device %s: %s", device_id, local_error)
            await _send_json(websocket, device_id, "device.error", {
                "code": "tts_unavailable",
                "detail": str(local_error),
            })
            return
    else:
        try:
            if settings.tts_provider == "gemini":
                pcm = await synthesize_gemini_pcm(text, settings.tts_model, settings.tts_voice)
            else:
                pcm = await synthesize_pcm(text, settings.tts_model, settings.tts_voice)
        except (TTSServiceError, ValueError) as cloud_error:
            try:
                pcm = await synthesize_local_pcm(text, settings.stt_language)
            except TTSServiceError as local_error:
                logger.warning(
                    "Voice response TTS unavailable for device %s (cloud: %s; local: %s)",
                    device_id,
                    cloud_error,
                    local_error,
                )
                await _send_json(websocket, device_id, "device.error", {
                    "code": "tts_unavailable",
                    "detail": str(local_error),
                })
                return
            logger.warning(
                "Cloud TTS unavailable for device %s; using local speech fallback: %s",
                device_id,
                cloud_error,
            )
    if not pcm or len(pcm) % 2:
        raise ValueError("TTS adapter returned invalid PCM16 data")
    end_at = time.monotonic() + (len(pcm) / (TTS_SAMPLE_RATE * 2)) + 0.75
    application.state.audio_echo_until[device_id] = end_at
    playback_id = str(uuid4())
    await _send_json(websocket, device_id, "tts.start", {
        "playback_id": playback_id,
        "sample_rate": TTS_SAMPLE_RATE,
        "channels": 1,
        "encoding": "pcm_s16le",
        "samples": len(pcm) // 2,
    })
    stream_started = time.monotonic()
    sent_seconds = 0.0
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
        sent_seconds += len(chunk) / (TTS_SAMPLE_RATE * 2)
        # Stay TTS_LEAD_SECONDS ahead of real-time playback so network jitter cannot starve the speaker.
        await asyncio.sleep(max(0.0, stream_started + sent_seconds - TTS_LEAD_SECONDS - time.monotonic()))
    await _send_json(websocket, device_id, "tts.end", {
        "playback_id": playback_id,
        "chunks": (len(pcm) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES,
    })
    _audit(application, device_id, "tts.playback", "success", {
        "playback_id": playback_id,
        "duration_ms": round(len(pcm) / 32),
    })


async def speak_wake_greeting(application, websocket, device_id: str) -> None:
    await run_wake_greeting(application, websocket, device_id)


async def speak_online_greeting(application, websocket, device_id: str) -> None:
    await run_boot_briefing(application, websocket, device_id)


async def process_command_audio(application, websocket, device_id: str, pcm: bytes) -> None:
    if len(pcm) > MAX_COMMAND_BYTES:
        await _send_json(websocket, device_id, "device.error", {"code": "command_audio_too_large"})
        return
    settings = application.state.settings
    guard = _REPLAY_GUARDS.setdefault(device_id, ReplayGuard())
    await _send_json(websocket, device_id, "animation.state", {"state": "thinking"})
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
        require_enrollment=settings.biometric_mode == "enforce",
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
        if verification.reason == "duration_out_of_range":
            response = "I didn't catch enough speech. Please say your question clearly after I finish speaking."
        elif verification.reason == "audio_quality_rejected":
            response = "I couldn't hear that clearly. Please move closer to the microphone and try again."
        elif verification.reason in {"no_voice_enrollment", "speaker_not_verified", "presentation_attack_suspected"}:
            response = "I couldn't verify your voice. Please enroll your voice in the dashboard, then try again."
        else:
            response = "I couldn't process that voice command. Please try again."
        await _speak(application, websocket, device_id, response)
        return
    await _send_json(websocket, device_id, "biometric.result", {
        "accepted": True,
        "confidence": verification.confidence,
    })
    try:
        transcript, stt_metadata = await _transcribe(application, pcm)
    except ValueError as exc:
        _audit(application, device_id, "voice.transcription", "failed", {"reason": str(exc)})
        await _send_json(websocket, device_id, "device.error", {
            "code": "stt_unavailable_or_failed",
            "detail": str(exc),
        })
        if application.state.device_games.get(device_id):
            await _speak(application, websocket, device_id, "I didn't catch that. Please try your guess again.")
        else:
            await _send_json(websocket, device_id, "animation.state", {"state": "idle"})
        return
    except (SpeechServiceError, LocalSpeechUnavailable) as exc:
        _audit(application, device_id, "voice.transcription", "failed", {"reason": str(exc)})
        await _send_json(websocket, device_id, "device.error", {
            "code": "stt_unavailable_or_failed",
            "detail": str(exc),
        })
        if application.state.device_games.get(device_id):
            await _speak(application, websocket, device_id, "Speech recognition is unavailable. Please check the server.")
        else:
            await _send_json(websocket, device_id, "animation.state", {"state": "idle"})
        return
    wake_phrase = settings.wake_word.strip()
    if wake_phrase and transcript.casefold().startswith(wake_phrase.casefold()):
        transcript = transcript[len(wake_phrase):].lstrip(" ,.!?")
    game_active = bool(application.state.device_games.get(device_id))
    letter_guess = game_active and bool(re.fullmatch(r"[a-zA-Z][.!?]?", transcript.strip()))
    pending_list = application.state.pending_todo_lists.get(device_id)
    pending_note = application.state.pending_notes.get(device_id)
    pending_reminder = application.state.pending_reminders.get(device_id)
    pending_alarm = application.state.pending_alarms.get(device_id)
    pending_event = application.state.pending_calendar_events.get(device_id)
    pending_confirmation = (
        (pending_list is not None and pending_list[0] == verification.user_id and pending_list[-1] > time.monotonic())
        or (pending_note is not None and pending_note[0] == verification.user_id and pending_note[-1] > time.monotonic())
        or (pending_reminder is not None and pending_reminder[0] == verification.user_id and pending_reminder[-1] > time.monotonic())
        or (pending_alarm is not None and pending_alarm[0] == verification.user_id and pending_alarm[-1] > time.monotonic())
        or (pending_event is not None and pending_event[0] == verification.user_id and pending_event[-1] > time.monotonic())
    ) and bool(re.fullmatch(r"(?:no|ok|okay)[.!?]?", transcript.strip(), re.IGNORECASE))
    if not transcript or (_is_noise_transcript(transcript) and not letter_guess and not pending_confirmation):
        await _send_json(websocket, device_id, "device.error", {"code": "empty_transcript"})
        if game_active:
            await _speak(application, websocket, device_id, "I didn't catch that. Please try your guess again.")
        else:
            await _send_json(websocket, device_id, "animation.state", {"state": "idle"})
            go_home_after_reply(application, websocket, device_id, hold=2.0)
        return
    logger.info("Command received from %s (game active: %s)", device_id,
                bool(application.state.device_games.get(device_id)))
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
    reply = await asyncio.to_thread(
        private_memory.route, application.state.db_factory, settings, verification.user_id, transcript,
        verified=verification.reason == "verified",
    )
    if reply == "I couldn't find that in your private facts." and verification.reason == "verified":
        semantic = await private_memory.semantic_recall(
            application.state.db_factory, settings, verification.user_id, transcript, verified=True,
        )
        if semantic is not None:
            reply = semantic
    if reply is None:
        reply = await _resolve(application, device_id, verification.user_id, transcript, websocket)
    application.state.wake_briefings.pop(device_id, None)
    _audit(application, device_id, "voice.response", "success", {"user_id": verification.user_id})
    await _speak(application, websocket, device_id, reply)
    go_home_after_reply(application, websocket, device_id)


def is_echo_guarded(application, device_id: str) -> bool:
    return application.state.audio_echo_until.get(device_id, 0.0) > time.monotonic()
