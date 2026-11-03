from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.models import AuditLog, Device, DisplayConfiguration
from app.schemas.protocol import ControlMessage
from app.services import game_history
from app.services.assistant.presenter import go_home_after_reply
from app.services.schedule_sync import push_schedule, record_fired
from app.services.audio.framing import AudioFrame, AudioFrameError
from app.services.audio.vad import VadGate
from app.services.assistant.orchestrator import (
    is_echo_guarded,
    process_command_audio,
    speak_online_greeting,
    speak_wake_greeting,
)
from app.services.wake_word.detector import WakeWordDetector, WakeWordUnavailable
from app.services.wake_word.transcript import spot_wake_phrase, transcript_wake_enabled
from app.services.stt.local_whisper import LocalSpeechUnavailable
from app.services.device_location import apply_hello_location
from app.services.device_display import DISPLAY_PAGES, build_display_payload, fetch_display_weather

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_UTTERANCE_BYTES = 16_000 * 2 * 20
WAKE_SEGMENT_MAX_BYTES = 16_000 * 2 * 4
WAKE_SEGMENT_MIN_BYTES = 16_000 * 2 // 4
WAKE_PREROLL_FRAMES = 8


async def telemetry_broadcast_loop(application) -> None:
    queue = application.state.telemetry_broadcast_queue
    while True:
        sample = await queue.get()
        try:
            clients = tuple(application.state.telemetry_clients)
            if clients:
                results = await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            client.send_json({"type": "telemetry.sample", "payload": sample}),
                            timeout=1.0,
                        )
                        for client in clients
                    ),
                    return_exceptions=True,
                )
                for client, result in zip(clients, results):
                    if isinstance(result, Exception):
                        logger.debug("Removing stalled telemetry client: %s", result)
                        application.state.telemetry_clients.discard(client)
        finally:
            queue.task_done()


def _record(db_factory, *, event_type: str, device_id: str, action: str, result: str, metadata: dict) -> None:
    with db_factory() as db:
        db.add(AuditLog(
            event_type=event_type,
            source="device",
            device_id=device_id,
            action=action,
            result=result,
            metadata_json=metadata,
        ))
        db.commit()


async def _publish_telemetry(application, sample: dict[str, object]) -> None:
    history = application.state.telemetry_history[sample["device_id"]]
    history.append(sample)
    queue = application.state.telemetry_broadcast_queue
    if queue.full():
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(sample)


@router.websocket("/devices/{device_id}/audio")
async def device_audio(websocket: WebSocket, device_id: str) -> None:
    settings = websocket.app.state.settings
    supplied = websocket.headers.get("authorization", "")
    token = supplied.removeprefix("Bearer ").strip() if supplied.startswith("Bearer ") else ""
    expected_device_token = settings.device_token
    expected_simulator_token = settings.simulator_token
    is_simulated = bool(expected_simulator_token and hmac.compare_digest(token, expected_simulator_token))

    # Run initial sync DB lookup in an async thread pool to avoid blocking the event loop during handshake
    def authenticate_device():
        with websocket.app.state.db_factory() as db:
            device_record = db.get(Device, device_id)
            device_token_valid = bool(
                device_record
                and hmac.compare_digest(
                    hashlib.sha256(token.encode()).hexdigest(),
                    device_record.credential_hash,
                )
            )
            return device_record, device_token_valid

    device_record, device_token_valid = await asyncio.to_thread(authenticate_device)
    shared_token_valid = bool(expected_device_token and hmac.compare_digest(token, expected_device_token))
    development_shared_token = settings.app_env != "production" and shared_token_valid and device_record is None

    if not token or not (is_simulated or device_token_valid or development_shared_token):
        await websocket.close(code=1008, reason="device authentication failed")
        return

    await websocket.accept()

    # Forcefully close any existing active socket for this device ID to prevent ghost connections
    existing_ws = websocket.app.state.device_sockets.get(device_id)
    if existing_ws is not None and existing_ws is not websocket:
        try:
            logger.info("Closing stale WebSocket connection for device %s", device_id)
            await existing_ws.close(code=1000, reason="superseded by new connection")
        except Exception:
            pass

    websocket.app.state.device_sockets[device_id] = websocket
    websocket.app.state.device_games.pop(device_id, None)

    expected_sequence: int | None = None
    last_seen_persisted = time.monotonic()
    last_frame_at: float | None = None
    interval_jitter_ms = 0.0
    dropped_frames = 0
    command_audio = bytearray()
    command_active = False
    wake_greeting_active = False
    awaiting_command = False
    command_started_at = 0.0
    command_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
    last_ping_at = time.monotonic()
    vad = VadGate(settings.vad_rms_threshold, settings.vad_end_silence_ms, adaptive=True)
    wake_detector = None
    command_task: asyncio.Task[None] | None = None
    wake_greeting_task: asyncio.Task[None] | None = None
    last_wake_diagnostic_at = 0.0
    wake_peak_score = 0.0
    wake_peak_rms = 0
    boot_greeting_requested = False
    transcript_wake = transcript_wake_enabled()
    wake_segment = bytearray()
    wake_preroll: list[bytes] = []
    transcript_wake_pending = False
    transcript_wake_busy = False

    async def check_wake_segment(segment: bytes) -> None:
        nonlocal transcript_wake_pending, transcript_wake_busy, transcript_wake
        try:
            matched, transcript = await spot_wake_phrase(segment, settings.wake_word)
            logger.info("Wake transcript for device %s: %r matched=%s", device_id, transcript, matched)
            if matched:
                transcript_wake_pending = True
        except LocalSpeechUnavailable as exc:
            logger.error("Transcript wake detection disabled for device %s: %s", device_id, exc)
            transcript_wake = False
        except Exception:
            logger.exception("Transcript wake detection failed for device %s", device_id)
        finally:
            transcript_wake_busy = False

    async def command_worker() -> None:
        nonlocal awaiting_command, command_started_at, command_active
        while True:
            pcm = await command_queue.get()
            try:
                await process_command_audio(websocket.app, websocket, device_id, pcm)
                if websocket.app.state.relisten.pop(device_id, False):
                    # A live game continues without requiring the wake word again.
                    vad.reset()
                    command_active = False
                    awaiting_command = True
                    command_started_at = time.monotonic()
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"listening-{uuid4()}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "animation.state",
                        "payload": {"state": "listening"},
                    })
            except WebSocketDisconnect:
                return
            except Exception:
                logger.exception("Voice command processing failed for device %s", device_id)
                try:
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"voice-error-{uuid4()}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "device.error",
                        "payload": {"code": "voice_processing_failed"},
                    })
                except (WebSocketDisconnect, RuntimeError):
                    return
            finally:
                command_queue.task_done()

    async def greet_after_wake(online: bool = False) -> None:
        nonlocal wake_greeting_active, awaiting_command, command_started_at, command_active
        try:
            speak = speak_online_greeting if online else speak_wake_greeting
            await speak(websocket.app, websocket, device_id)
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:
            logger.exception("Could not complete voice greeting for device %s", device_id)
        finally:
            wake_greeting_active = False
            awaiting_command = True
            command_started_at = time.monotonic()
            if vad.active and not command_active:
                # Speech already in progress when listening begins belongs to the command.
                command_active = True
                awaiting_command = False
            try:
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": f"listening-{uuid4()}",
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "animation.state",
                    "payload": {"state": "listening"},
                })
            except (WebSocketDisconnect, RuntimeError):
                pass

    try:
        if settings.wake_word_model_path:
            try:
                wake_detector = WakeWordDetector(
                    settings.wake_word_model_path,
                    settings.wake_word_threshold,
                    wake_phrase=settings.wake_word,
                )
            except WakeWordUnavailable as exc:
                logger.error("Wake-word detector unavailable for %s: %s", device_id, exc)
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": "wake-word-unavailable",
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "device.error",
                    "payload": {"code": "wake_word_unavailable", "detail": str(exc)},
                })
            else:
                logger.info(
                    "Wake-word detector ready for device %s (phrase=%r, threshold=%.2f)",
                    device_id,
                    settings.wake_word,
                    settings.wake_word_threshold,
                )
        else:
            logger.error(
                "Wake-word detection is disabled for device %s: WAKE_WORD_MODEL_PATH is empty",
                device_id,
            )
        first_message = await websocket.receive_text()
        hello = ControlMessage.model_validate_json(first_message)
        if hello.device_id != device_id or hello.type != "device.hello":
            await websocket.close(code=1008, reason="invalid device hello")
            return
        if not is_simulated and hello.message_id.startswith("boot-"):
            previous_boot_id = websocket.app.state.device_boot_greetings.get(device_id)
            if previous_boot_id != hello.message_id:
                websocket.app.state.device_boot_greetings[device_id] = hello.message_id
                boot_greeting_requested = True

        if not is_simulated and isinstance(hello.payload.get("location"), dict):
            location_task = asyncio.create_task(
                apply_hello_location(websocket.app, device_id, hello.payload["location"]),
            )
            location_task.add_done_callback(lambda task: task.exception() and logger.warning(
                "Could not apply device location for %s: %s", device_id, task.exception()))
        websocket.app.state.devices[device_id] = (
            datetime.now(timezone.utc).isoformat(),
            is_simulated,
        )
        websocket.app.state.device_sockets[device_id] = websocket
        capabilities = hello.payload.get("capabilities")
        websocket.app.state.device_capabilities[device_id] = (
            tuple(str(item) for item in capabilities[:8]) if isinstance(capabilities, list) and not is_simulated else ()
        )
        websocket.app.state.schedule_hashes.pop(device_id, None)

        # Record connected event in thread pool
        await asyncio.to_thread(
            _record,
            websocket.app.state.db_factory,
            event_type="device.connected",
            device_id=device_id,
            action="websocket connected",
            result="success",
            metadata={"simulated": is_simulated},
        )

        if "local_schedule" in websocket.app.state.device_capabilities.get(device_id, ()):
            await push_schedule(websocket.app, device_id, websocket)
        await websocket.send_json({
            "protocol_version": 1,
            "message_id": "server-ready",
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "device.status",
            "payload": {
                "connected": True,
                "audio_format": "pcm_s16le",
                "simulated": is_simulated,
                "config": device_record.config if device_record else {},
            },
        })

        # Fetch display config without blocking loop
        def fetch_display_config():
            with websocket.app.state.db_factory() as db:
                display_configuration = db.get(DisplayConfiguration, device_id)
                saved_page = (
                    display_configuration.configuration.get("page", "home")
                    if display_configuration
                    else "home"
                )
                return saved_page if saved_page in DISPLAY_PAGES else "home"

        page = await asyncio.to_thread(fetch_display_config)
        weather = None
        location = device_record.config.get("display_location", {}) if device_record else {}
        if page == "weather" and location.get("latitude") is not None and location.get("longitude") is not None:
            try:
                weather = await fetch_display_weather(location["latitude"], location["longitude"])
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Weather page refresh failed for device %s: %s", device_id, exc)
                weather = {"summary": "WEATHER OFFLINE"}

        await websocket.send_json({
            "protocol_version": 1,
            "message_id": str(uuid4()),
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "display.state",
            "payload": build_display_payload(websocket.app.state.db_factory, page, weather),
        })

        command_task = asyncio.create_task(command_worker())
        if boot_greeting_requested:
            logger.info("Starting one-time boot greeting for device %s", device_id)
            wake_greeting_active = True
            wake_greeting_task = asyncio.create_task(greet_after_wake(online=True))

        async with websocket.app.state.ota_pending_lock:
            pending_release = websocket.app.state.ota_pending.get(device_id)
            if pending_release is not None:
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": str(uuid4()),
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "ota.available",
                    "payload": {"release": pending_release},
                })
                if websocket.app.state.ota_pending.get(device_id) is pending_release:
                    websocket.app.state.ota_pending.pop(device_id, None)

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                logger.info(
                    "Device WebSocket disconnected for %s (code=%s)",
                    device_id,
                    message.get("code", "unknown"),
                )
                break

            if message.get("bytes") is not None:
                if device_record and device_record.config.get("microphone_enabled") is False:
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": "microphone-disabled",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "device.error",
                        "payload": {"code": "microphone_streaming_disabled"},
                    })
                    await websocket.close(code=1008, reason="microphone streaming disabled")
                    break

                frame = AudioFrame.decode(message["bytes"], settings.max_audio_frame_bytes)

                # Initialize sequence tracking on first frame
                if expected_sequence is None:
                    expected_sequence = frame.sequence

                # Diagnostic logging: print audio RMS every 50 frames (~1 sec)
                if frame.sequence % 50 == 0:
                    logger.info(
                        "Audio Stream Diagnostic - Device: %s, Seq: %d, RMS: %d, Samples: %d",
                        device_id,
                        frame.sequence,
                        frame.rms(),
                        frame.sample_count,
                    )

                if frame.sequence != expected_sequence:
                    distance = ((frame.sequence - expected_sequence + (1 << 31)) % (1 << 32)) - (1 << 31)
                    if distance > 0:
                        dropped_frames += distance
                        await websocket.send_json({
                            "protocol_version": 1,
                            "message_id": f"nack-{expected_sequence}",
                            "device_id": device_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "audio.nack",
                            "payload": {"expected_sequence": expected_sequence, "received_sequence": frame.sequence},
                        })
                        continue
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"ack-duplicate-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "audio.ack",
                        "payload": {"sequence": frame.sequence, "duplicate": True},
                    })
                    continue

                expected_sequence = (frame.sequence + 1) & 0xFFFFFFFF
                duration_ms = max(1, round(frame.sample_count * 1000 / frame.sample_rate))
                now_monotonic = time.monotonic()
                if last_frame_at is not None:
                    observed_ms = (now_monotonic - last_frame_at) * 1000
                    interval_jitter_ms = 0.9 * interval_jitter_ms + 0.1 * abs(observed_ms - duration_ms)
                last_frame_at = now_monotonic

                if is_echo_guarded(websocket.app, device_id):
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"ack-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "audio.ack",
                        "payload": {"sequence": frame.sequence},
                    })
                    continue

                transition = vad.update(frame.rms(), duration_ms)
                wake_detected = False
                wake_score = None

                if (wake_detector or transcript_wake) and not command_active and not wake_greeting_active and not awaiting_command:
                    detected, score = wake_detector.process(frame.pcm) if wake_detector else (False, None)
                    if transcript_wake:
                        if vad.active:
                            if not wake_segment:
                                for earlier in wake_preroll:
                                    wake_segment.extend(earlier)
                            wake_segment.extend(frame.pcm)
                        if wake_segment and (not vad.active or len(wake_segment) >= WAKE_SEGMENT_MAX_BYTES):
                            segment = bytes(wake_segment)
                            wake_segment.clear()
                            if len(segment) >= WAKE_SEGMENT_MIN_BYTES and not transcript_wake_busy:
                                transcript_wake_busy = True
                                asyncio.create_task(check_wake_segment(segment))
                        wake_preroll.append(frame.pcm)
                        del wake_preroll[:-WAKE_PREROLL_FRAMES]
                        if transcript_wake_pending:
                            transcript_wake_pending = False
                            detected = True
                            score = score if score is not None else 1.0
                    wake_score = score
                    wake_peak_rms = max(wake_peak_rms, frame.rms())
                    if score is not None:
                        wake_peak_score = max(wake_peak_score, score)
                    if now_monotonic - last_wake_diagnostic_at >= 5:
                        logger.info(
                            "Wake-word monitor for device %s: peak_score=%.3f threshold=%.3f peak_input_rms=%d",
                            device_id,
                            wake_peak_score,
                            settings.wake_word_threshold,
                            wake_peak_rms,
                        )
                        wake_peak_score = 0.0
                        wake_peak_rms = 0
                        last_wake_diagnostic_at = now_monotonic
                    if detected:
                        wake_detected = True
                        wake_greeting_active = True
                        wake_segment.clear()
                        command_audio.clear()
                        websocket.app.state.wake_briefings[device_id] = now_monotonic
                        await websocket.send_json({
                            "protocol_version": 1,
                            "message_id": f"wake-{frame.sequence}",
                            "device_id": device_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "audio.wake_word",
                            "payload": {"wake_word": settings.wake_word, "score": score, "sequence": frame.sequence},
                        })

                        # Offload DB audit log to thread pool to prevent blocking frame processing
                        asyncio.create_task(asyncio.to_thread(
                            _record,
                            websocket.app.state.db_factory,
                            event_type="audio.wake_word",
                            device_id=device_id,
                            action="wake word detected",
                            result="success",
                            metadata={"score": score, "sequence": frame.sequence},
                        ))
                        wake_greeting_task = asyncio.create_task(greet_after_wake())

                elif command_active and not wake_detected:
                    command_audio.extend(frame.pcm)
                    if len(command_audio) > MAX_UTTERANCE_BYTES:
                        command_audio.clear()
                        command_active = False
                        vad.reset()
                        await websocket.send_json({
                            "protocol_version": 1,
                            "message_id": f"idle-{frame.sequence}-{uuid4()}",
                            "device_id": device_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "animation.state",
                            "payload": {"state": "idle"},
                        })
                        await websocket.send_json({
                            "protocol_version": 1,
                            "message_id": f"voice-size-{frame.sequence}",
                            "device_id": device_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "device.error",
                            "payload": {"code": "command_audio_too_large"},
                        })

                if command_active and (now_monotonic - command_started_at) > 12:
                    command_audio.clear()
                    command_active = False
                    vad.reset()
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"idle-{frame.sequence}-{uuid4()}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "animation.state",
                        "payload": {"state": "idle"},
                    })
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"voice-timeout-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "device.error",
                        "payload": {"code": "command_audio_timeout"},
                    })

                websocket.app.state.devices[device_id] = (
                    datetime.now(timezone.utc).isoformat(),
                    is_simulated,
                )

                # Persist last_seen_at asynchronously every 30 seconds
                if device_record and now_monotonic - last_seen_persisted >= 30:
                    last_seen_persisted = now_monotonic
                    def update_last_seen():
                        with websocket.app.state.db_factory() as db:
                            registered = db.get(Device, device_id)
                            if registered:
                                registered.last_seen_at = datetime.now(timezone.utc)
                                db.commit()
                    asyncio.create_task(asyncio.to_thread(update_last_seen))

                if transition:
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"vad-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "audio.vad",
                        "payload": {"state": transition, "sequence": frame.sequence},
                    })
                    if transition == "speech_started" and awaiting_command:
                        command_audio.clear()
                        command_started_at = now_monotonic
                        command_active = True
                        awaiting_command = False
                    elif transition == "speech_ended" and command_active:
                        completed_audio = bytes(command_audio)
                        command_audio.clear()
                        command_active = False
                        try:
                            command_queue.put_nowait(completed_audio)
                        except asyncio.QueueFull:
                            await websocket.send_json({
                                "protocol_version": 1,
                                "message_id": f"voice-busy-{frame.sequence}",
                                "device_id": device_id,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "type": "device.error",
                                "payload": {"code": "voice_processing_busy"},
                            })

                listen_window = 120 if websocket.app.state.device_games.get(device_id) else 30
                if awaiting_command and now_monotonic - command_started_at > listen_window:
                    awaiting_command = False
                    websocket.app.state.wake_briefings.pop(device_id, None)
                    go_home_after_reply(websocket.app, websocket, device_id, hold=0.0)
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"idle-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "animation.state",
                        "payload": {"state": "idle"},
                    })
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": f"wake-timeout-{frame.sequence}",
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "assistant.response",
                        "payload": {"state": "listening_timeout"},
                    })

                await _publish_telemetry(websocket.app, {
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sequence": frame.sequence,
                    "rms": frame.rms(),
                    "sample_rate": frame.sample_rate,
                    "sample_count": frame.sample_count,
                    "estimated_jitter_ms": round(interval_jitter_ms, 2),
                    "dropped_frames": dropped_frames,
                    "processing_ms": round((time.monotonic() - now_monotonic) * 1000, 2),
                    "wake_detected": wake_detected,
                    "wake_score": wake_score,
                })

                now = time.monotonic()
                if now - last_ping_at >= 10:
                    ping_id = f"ping-{frame.sequence}"
                    websocket.app.state.pending_pings[(device_id, ping_id)] = now
                    last_ping_at = now
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": ping_id,
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "ping",
                        "payload": {},
                    })
                await websocket.send_json({
                    "protocol_version": 1,
                    "message_id": f"ack-{frame.sequence}",
                    "device_id": device_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "audio.ack",
                    "payload": {"sequence": frame.sequence},
                })
                continue

            if message.get("text") is not None:
                control = ControlMessage.model_validate_json(message["text"])
                if control.device_id != device_id:
                    raise ValueError("invalid versioned control message")
                if control.type == "ping":
                    await websocket.send_json({
                        "protocol_version": 1,
                        "message_id": control.message_id,
                        "device_id": device_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "pong",
                        "payload": {},
                    })
                elif control.type == "pong":
                    sent_at = websocket.app.state.pending_pings.pop((device_id, control.message_id), None)
                    if sent_at is not None:
                        rtt_ms = round((time.monotonic() - sent_at) * 1000, 2)
                        await _publish_telemetry(websocket.app, {
                            "device_id": device_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "network_rtt_ms": rtt_ms,
                        })
                elif control.type == "schedule.fired":
                    await asyncio.to_thread(record_fired, websocket.app.state.db_factory, device_id, control.payload)
                elif control.type == "game.history":
                    stored = await asyncio.to_thread(
                        game_history.ingest_offline, websocket.app.state.db_factory, device_id,
                        control.payload.get("records"))
                    logger.info("Device %s uploaded offline game results: %d stored", device_id, stored)
                elif control.type == "game.event":
                    payload = control.payload
                    websocket.app.state.device_games[device_id] = bool(payload.get("active"))
                    waiter = websocket.app.state.game_waiters.pop((device_id, str(payload.get("request_id", ""))), None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(payload)
                elif control.type in {
                    "device.status", "display.state", "ota.status", "ota.available", "game.state",
                    "device.error", "audio.vad", "audio.wake_word",
                }:
                    if control.type == "ota.status":
                        release_id = control.payload.get("release_id")
                        state = control.payload.get("state")
                        progress = control.payload.get("progress")
                        if (
                            isinstance(release_id, str)
                            and isinstance(state, str)
                            and len(state) <= 32
                            and (progress is None or (
                                isinstance(progress, (int, float))
                                and not isinstance(progress, bool)
                                and 0 <= progress <= 100
                            ))
                        ):
                            application = websocket.app
                            sample = {
                                "device_id": device_id,
                                "release_id": release_id,
                                "state": state,
                                "progress": progress,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                            application.state.ota_progress[(device_id, release_id)] = sample
                            await _publish_telemetry(application, sample)

                    # Offload control message audit logs to thread pool
                    asyncio.create_task(asyncio.to_thread(
                        _record,
                        websocket.app.state.db_factory,
                        event_type=control.type,
                        device_id=device_id,
                        action="device event received",
                        result="error" if control.type == "device.error" else "success",
                        metadata={"message_id": control.message_id, "payload_keys": sorted(control.payload)},
                    ))
    except WebSocketDisconnect as exc:
        logger.info("Device WebSocket disconnected for %s (code=%s)", device_id, exc.code)
    except (AudioFrameError, json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        logger.warning("Rejected invalid device stream for %s: %s", device_id, exc)
        try:
            await websocket.close(code=1003, reason="invalid device message")
        except RuntimeError:
            logger.debug("Could not close invalid device stream for %s", device_id)
    except Exception:
        logger.exception("Unexpected device WebSocket failure for %s", device_id)
        try:
            await websocket.close(code=1011, reason="internal device stream error")
        except RuntimeError:
            logger.debug("Could not close failed device stream for %s", device_id)
    finally:
        if wake_greeting_task is not None:
            wake_greeting_task.cancel()
            await asyncio.gather(wake_greeting_task, return_exceptions=True)
        if command_task is not None:
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
        websocket.app.state.device_games.pop(device_id, None)
        websocket.app.state.relisten.pop(device_id, None)
        if websocket.app.state.device_sockets.get(device_id) is websocket:
            websocket.app.state.device_capabilities.pop(device_id, None)
        if websocket.app.state.device_sockets.get(device_id) is websocket:
            websocket.app.state.devices.pop(device_id, None)
        if websocket.app.state.device_sockets.get(device_id) is websocket:
            websocket.app.state.device_sockets.pop(device_id, None)

        # Offload disconnect event audit logging
        asyncio.create_task(asyncio.to_thread(
            _record,
            websocket.app.state.db_factory,
            event_type="device.disconnected",
            device_id=device_id,
            action="websocket disconnected",
            result="closed",
            metadata={},
        ))