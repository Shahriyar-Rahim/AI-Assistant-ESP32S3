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


def test_game_letter_and_empty_transcript(tmp_path: Path, monkeypatch) -> None:
    application = create_app(_settings(tmp_path))
    transcripts = iter(["A", ""])
    resolved: list[str] = []
    spoken: list[str] = []

    async def transcribe(*_args):
        return next(transcripts), {"provider": "test", "model": "test", "language": "en"}

    async def resolve(_app, _device, _user, transcript, _socket):
        resolved.append(transcript)
        return "No, A is not in the word. Try another letter."

    async def speak(_app, _socket, _device, text):
        spoken.append(text)

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "_resolve", resolve)
    monkeypatch.setattr(orchestrator, "_speak", speak)
    monkeypatch.setattr(orchestrator, "record_interaction", lambda *_args, **_kwargs: None)
    websocket = RecordingWebSocket()
    with TestClient(application):
        with application.state.db_factory() as db:
            user_id = db.query(User).filter(User.username == "admin").one().id
        monkeypatch.setattr(
            orchestrator, "verify_command_audio",
            lambda *_args, **_kwargs: VerificationResult(True, "verified", 0.99, user_id),
        )
        application.state.device_games["dev"] = True
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))

    assert resolved == ["A"]
    assert "Try another letter" in spoken[0]
    assert "try your guess again" in spoken[1]
    assert application.state.device_games["dev"]


def test_unrecognized_game_speech_prompts_retry_without_ending_game(tmp_path: Path, monkeypatch) -> None:
    application = create_app(_settings(tmp_path))
    spoken: list[str] = []

    async def transcribe(*_args):
        raise ValueError("speech was not recognized")

    async def speak(_app, _socket, _device, text):
        spoken.append(text)

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "_speak", speak)
    websocket = RecordingWebSocket()
    with TestClient(application):
        with application.state.db_factory() as db:
            user_id = db.query(User).filter(User.username == "admin").one().id
        monkeypatch.setattr(
            orchestrator, "verify_command_audio",
            lambda *_args, **_kwargs: VerificationResult(True, "verified", 0.99, user_id),
        )
        application.state.device_games["dev"] = True
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))

    assert application.state.device_games["dev"]
    assert spoken == ["I didn't catch that. Please try your guess again."]
    assert not any(m["type"] == "animation.state" and m["payload"].get("state") == "idle"
                   for m in websocket.messages)


def test_private_memory_voice_requires_verified_enrollment(tmp_path: Path, monkeypatch) -> None:
    from cryptography.fernet import Fernet
    from app.models import LearnedResponse

    settings = replace(_settings(tmp_path), voice_encryption_key=Fernet.generate_key().decode())
    application = create_app(settings)
    spoken: list[str] = []
    reason = "enrollment_not_required"
    transcript = "remember that my home is near the park"
    semantic_calls = []

    async def transcribe(*_args):
        return transcript, {"provider": "test", "model": "test", "language": "en"}

    async def speak(_app, _socket, _device, text):
        spoken.append(text)

    async def semantic(_factory, _settings, current_user, text, *, verified):
        semantic_calls.append((current_user, text, verified))
        return "semantic result"

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "_speak", speak)
    monkeypatch.setattr(orchestrator, "record_interaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.private_memory, "semantic_recall", semantic)
    with TestClient(application):
        with application.state.db_factory() as db:
            user_id = db.query(User).filter(User.username == "admin").one().id
        monkeypatch.setattr(
            orchestrator, "verify_command_audio",
            lambda *_args, **_kwargs: VerificationResult(True, reason, 0.99, user_id),
        )
        websocket = RecordingWebSocket()
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))
        assert "verify" in spoken[-1]
        with application.state.db_factory() as db:
            assert db.query(LearnedResponse).filter_by(kind="private_fact").count() == 0
        reason = "verified"
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))
        assert "saved" in spoken[-1]
        with application.state.db_factory() as db:
            assert db.query(LearnedResponse).filter_by(kind="private_fact").count() == 1
        transcript = "What is my favorite fruit?"
        reason = "enrollment_not_required"
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))
        assert "verify" in spoken[-1]
        assert not semantic_calls
        reason = "verified"
        asyncio.run(orchestrator.process_command_audio(application, websocket, "dev", bytes(32000)))
        assert spoken[-1] == "semantic result"
        assert semantic_calls == [(user_id, transcript, True)]


def test_cloud_failure_uses_configured_local_ollama(tmp_path: Path, monkeypatch) -> None:
    from app.services.ai.providers import AIResult, ProviderRequestError

    calls = []

    class Cloud:
        async def complete(self, _messages, model):
            calls.append(("cloud", model))
            raise ProviderRequestError("HTTP 429")

    class Local:
        async def complete(self, messages, model):
            calls.append(("local", model))
            assert messages[-1]["content"] == "Explain gravity"
            return AIResult('{"kind":"reply","text":"Gravity pulls objects together."}', {}, model)

    settings = replace(_settings(tmp_path), default_ai_provider="gemini", ai_model="cloud-model",
                       ai_fallback_model="cloud-backup", ollama_fallback_model="local-model")
    application = create_app(settings)
    monkeypatch.setitem(orchestrator.PROVIDERS, "gemini", Cloud())
    monkeypatch.setitem(orchestrator.PROVIDERS, "ollama", Local())
    with TestClient(application):
        with application.state.db_factory() as db:
            user_id = db.query(User).filter(User.username == "admin").one().id
        reply, action = asyncio.run(orchestrator._ai_fallback(application, "Explain gravity", user_id))
    assert reply == "Gravity pulls objects together."
    assert action is None
    assert calls == [("cloud", "cloud-model"), ("cloud", "cloud-backup"), ("local", "local-model")]


def test_short_no_reaches_pending_list_followup(tmp_path: Path, monkeypatch) -> None:
    import time

    application = create_app(_settings(tmp_path))
    spoken = []

    async def transcribe(*_args):
        return "no", {"provider": "test", "model": "test", "language": "en"}

    async def speak(_app, _socket, _device, text):
        spoken.append(text)

    monkeypatch.setattr(orchestrator, "_transcribe", transcribe)
    monkeypatch.setattr(orchestrator, "_speak", speak)
    monkeypatch.setattr(orchestrator, "record_interaction", lambda *_args, **_kwargs: None)
    with TestClient(application):
        with application.state.db_factory() as db:
            user_id = db.query(User).filter(User.username == "admin").one().id
        monkeypatch.setattr(
            orchestrator, "verify_command_audio",
            lambda *_args, **_kwargs: VerificationResult(True, "verified", 0.99, user_id),
        )
        application.state.pending_todo_lists["dev"] = (user_id, "list-id", "shopping", "confirm", time.monotonic() + 60)
        asyncio.run(orchestrator.process_command_audio(application, RecordingWebSocket(), "dev", bytes(32000)))
        assert "saved" in spoken[0]
        assert "dev" not in application.state.pending_todo_lists


def test_voice_volume_saves_and_rejects_non_admin(tmp_path: Path, monkeypatch) -> None:
    from app.models import Device

    application = create_app(_settings(tmp_path))
    with TestClient(application):
        with application.state.db_factory() as db:
            admin = db.query(User).filter(User.username == "admin").one()
            admin_id = admin.id
            db.add(User(username="viewer", password_hash="test", role="user"))
            db.add(Device(id="volume-device", name="Device", credential_hash="test", config={}))
            db.commit()
            viewer_id = db.query(User).filter(User.username == "viewer").one().id
        reply = asyncio.run(orchestrator._resolve_command(
            application, "volume-device", admin_id, "set volume to level fifty five"
        ))
        assert "55 percent" in reply and "reconnect" in reply
        with application.state.db_factory() as db:
            assert db.get(Device, "volume-device").config["volume"] == 55
        denied = asyncio.run(orchestrator._resolve_command(
            application, "volume-device", viewer_id, "set volume to 70"
        ))
        assert "administrator" in denied
        assert asyncio.run(orchestrator._resolve_command(
            application, "volume-device", admin_id, "set volume to 101"
        )).startswith("Volume must be")
