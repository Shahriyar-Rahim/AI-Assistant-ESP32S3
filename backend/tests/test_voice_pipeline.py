from __future__ import annotations

import asyncio
import struct
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models import User
from app.services.audio.verification import VerificationResult
from app.services.assistant import orchestrator


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'voice-pipeline.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
    )


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.binary_messages: list[bytes] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)


def test_verified_voice_command_reaches_action_and_synchronized_tts(tmp_path, monkeypatch) -> None:
    async def transcribe(*_args):
        return "What's on my calendar?", {
            "provider": "test",
            "model": "fixture",
            "language": "en",
        }

    async def synthesize(*_args):
        return bytes(640)

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "synthesize_pcm", synthesize)
    monkeypatch.setattr(orchestrator, "record_interaction", lambda *_args, **_kwargs: None)
    application = create_app(_settings(tmp_path))

    with TestClient(application):
        with application.state.db_factory() as db:
            user = db.query(User).filter(User.username == "admin").one()
            user_id = user.id
        monkeypatch.setattr(
            orchestrator,
            "verify_command_audio",
            lambda *_args, **_kwargs: VerificationResult(True, "verified", 0.99, user_id),
        )
        websocket = RecordingWebSocket()
        asyncio.run(orchestrator.process_command_audio(
            application,
            websocket,
            "voice-device",
            struct.pack("<48000h", *([1000] * 48_000)),
        ))

    message_types = [message["type"] for message in websocket.messages]
    assert "biometric.result" in message_types
    assert "audio.transcription" in message_types
    assert "display.state" in message_types
    assert "tts.start" in message_types
    assert "tts.end" in message_types
    assert websocket.binary_messages == [bytes(640)]


def test_anti_spoof_rejection_stops_before_transcription_and_explains_to_user(tmp_path, monkeypatch) -> None:
    import app.services.audio.verification as verification

    monkeypatch.setattr(verification, "bona_fide_score", lambda *_args: 0.01)
    spoken: list[str] = []

    async def record_speech(_application, _websocket, _device_id, text):
        spoken.append(text)

    monkeypatch.setattr(orchestrator, "_speak", record_speech)
    monkeypatch.setattr(
        orchestrator,
        "_transcribe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("rejected audio reached STT")),
    )
    application = create_app(_settings(tmp_path))
    websocket = RecordingWebSocket()
    pcm = struct.pack("<48000h", *([1000] * 48_000))

    with TestClient(application):
        asyncio.run(orchestrator.process_command_audio(
            application, websocket, f"spoof-device-{tmp_path.name}", pcm,
        ))

    result = next(message for message in websocket.messages if message["type"] == "biometric.result")
    assert result["payload"]["accepted"] is False
    assert result["payload"]["reason"] == "presentation_attack_suspected"
    assert spoken and "enroll your voice" in spoken[0]
    assert "audio.transcription" not in [message["type"] for message in websocket.messages]
    assert websocket.binary_messages == []


def test_enrolled_only_mode_accepts_until_first_enrollment_then_enforces(tmp_path: Path) -> None:
    from app.services.audio.verification import ReplayGuard, verify_command_audio

    application = create_app(_settings(tmp_path))
    pcm = struct.pack("<16000h", *([1500, -1500] * 8000))
    kwargs = dict(
        device_id="d",
        threshold=0.75,
        anti_spoof_model_path="",
        anti_spoof_threshold=0.8,
    )
    with TestClient(application):
        strict = verify_command_audio(application.state.db_factory, pcm=pcm, replay_guard=ReplayGuard(), **kwargs)
        assert not strict.accepted and strict.reason == "duration_out_of_range"

        relaxed = verify_command_audio(
            application.state.db_factory, pcm=pcm, replay_guard=ReplayGuard(), require_enrollment=False, **kwargs
        )
        assert relaxed.accepted and relaxed.reason == "enrollment_not_required" and relaxed.user_id

        guard = ReplayGuard()
        assert verify_command_audio(application.state.db_factory, pcm=pcm, replay_guard=guard, require_enrollment=False, **kwargs).accepted
        assert verify_command_audio(application.state.db_factory, pcm=pcm, replay_guard=guard, require_enrollment=False, **kwargs).reason == "replayed_audio"

        from app.models import VoiceEnrollment
        with application.state.db_factory() as db:
            db.add(VoiceEnrollment(user_id=relaxed.user_id, encrypted_embedding="x", model_name="test"))
            db.commit()
        enforced = verify_command_audio(
            application.state.db_factory, pcm=pcm, replay_guard=ReplayGuard(), require_enrollment=False, **kwargs
        )
        assert not enforced.accepted and enforced.reason == "duration_out_of_range"


def test_short_command_works_before_first_enrollment_in_onboarding_mode(tmp_path: Path, monkeypatch) -> None:
    settings = replace(_settings(tmp_path), biometric_mode="enrolled_only")
    application = create_app(settings)
    spoken: list[str] = []

    async def transcribe(_application, _pcm):
        return "what time is it", {"provider": "test", "model": "test", "language": "en"}

    async def resolve(_application, _device_id, _user_id, transcript, _websocket):
        assert transcript == "what time is it"
        return "It is noon."

    async def speak(_application, _websocket, _device_id, text):
        spoken.append(text)

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "_resolve", resolve)
    monkeypatch.setattr(orchestrator, "record_interaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_speak", speak)

    websocket = RecordingWebSocket()
    pcm = struct.pack("<16000h", *([1500, -1500] * 8000))
    with TestClient(application):
        asyncio.run(orchestrator.process_command_audio(application, websocket, "short-command-device", pcm))

    assert any(message["type"] == "audio.transcription" for message in websocket.messages)
    assert not any(
        message["type"] == "biometric.result" and not message["payload"]["accepted"]
        for message in websocket.messages
    )
    assert spoken == ["It is noon."]
