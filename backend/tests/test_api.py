import struct
import asyncio
from collections import defaultdict
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app
from app.services.audio.framing import AudioFrame, encode_frame
from app.websocket.device import _publish_telemetry, telemetry_broadcast_loop


def test_health_endpoint(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=("http://localhost:5173",),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_websocket_requires_auth_and_device_hello(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
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
    with TestClient(create_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws/devices/test/audio"):
                pass
        with client.websocket_connect(
            "/api/v1/ws/devices/test/audio",
            headers={"Authorization": "Bearer device-test"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "hello-1",
                "device_id": "test",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {"firmware": "test"},
            })
            assert websocket.receive_json()["payload"]["connected"] is True


def test_firmware_boot_hello_triggers_one_boot_greeting(tmp_path, monkeypatch) -> None:
    import app.websocket.device as device_websocket

    greeted = threading.Event()

    async def fake_greeting(*_args) -> None:
        greeted.set()

    monkeypatch.setattr(device_websocket, "speak_online_greeting", fake_greeting)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'boot-greeting.db'}",
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
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect(
            "/api/v1/ws/devices/esp32/audio",
            headers={"Authorization": "Bearer device-test"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "boot-123",
                "device_id": "esp32",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {},
            })
            assert websocket.receive_json()["type"] == "device.status"
            assert greeted.wait(timeout=1)


def test_physical_hello_restores_active_game_after_reconnect(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'reconnect-game.db'}",
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
    app = create_app(settings)
    with TestClient(app) as client:
        def connect(active):
            with client.websocket_connect(
                "/api/v1/ws/devices/esp32/audio",
                headers={"Authorization": "Bearer device-test"},
            ) as websocket:
                websocket.send_json({
                    "protocol_version": 1,
                    "message_id": "boot-reconnect",
                    "device_id": "esp32",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "device.hello",
                    "payload": {"game_active": active},
                })
                assert websocket.receive_json()["type"] == "device.status"
                assert bool(app.state.device_games.get("esp32")) is active

        connect(True)
        connect(False)


def test_missing_wake_word_model_keeps_audio_stream_available(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'missing-wake-model.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
        wake_word_model_path=str(tmp_path / "missing.onnx"),
    )
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect(
            "/api/v1/ws/devices/esp32/audio",
            headers={"Authorization": "Bearer device-test"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "hello",
                "device_id": "esp32",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {},
            })
            assert websocket.receive_json()["payload"]["code"] == "wake_word_unavailable"
            assert websocket.receive_json()["payload"]["connected"] is True
            websocket.receive_json()
            websocket.send_bytes(encode_frame(AudioFrame(
                0, 1, 16_000, 320, 1, bytes(640),
            )))
            assert websocket.receive_json()["type"] == "audio.ack"


def test_authenticated_websocket_streams_pcm_vad_and_ping(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stream.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=400,
        vad_end_silence_ms=40,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
    )
    application = create_app(settings)
    with TestClient(application) as client:
        with client.websocket_connect(
            "/api/v1/ws/devices/test/audio",
            headers={"Authorization": "Bearer sim-test"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "hello",
                "device_id": "test",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {"origin": "SIMULATED"},
            })
            assert websocket.receive_json()["payload"]["simulated"] is True
            login = client.post("/api/v1/auth/login", json={
                "username": "admin",
                "password": "test-password-that-is-long",
            })
            assert login.status_code == 200
            token = login.json()["access_token"]
            assert client.get("/api/v1/devices", headers={"Authorization": f"Bearer {token}"}).json()["devices"][0]["device_id"] == "test"

            voiced = AudioFrame(0, 1, 16_000, 320, 1, struct.pack("<320h", *([1000] * 320)))
            silent = AudioFrame(1, 2, 16_000, 320, 1, bytes(640))
            websocket.send_bytes(encode_frame(voiced))
            while True:
                received = websocket.receive_json()
                if received["type"] == "audio.vad":
                    assert received["payload"]["state"] == "speech_started"
                    break
            websocket.send_bytes(encode_frame(silent))
            websocket.send_bytes(encode_frame(AudioFrame(2, 3, 16_000, 320, 1, bytes(640))))
            while True:
                received = websocket.receive_json()
                if received["type"] == "audio.vad":
                    assert received["payload"]["state"] == "speech_ended"
                    break
            websocket.send_bytes(encode_frame(AudioFrame(3, 4, 16_000, 640, 1, bytes(1280))))
            while True:
                received = websocket.receive_json()
                if received["type"] == "audio.ack" and received["payload"]["sequence"] == 3:
                    break

            websocket.send_json({
                "protocol_version": 1,
                "message_id": "ping-1",
                "device_id": "test",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "ping",
                "payload": {},
            })
            while True:
                received = websocket.receive_json()
                if received["type"] == "pong":
                    break


def test_audio_gap_replays_missing_frame_and_acks_accepted_audio(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'audio-replay.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=400,
        vad_end_silence_ms=40,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
    )
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect(
            "/api/v1/ws/devices/test/audio",
            headers={"Authorization": "Bearer device-test"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "hello",
                "device_id": "test",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {},
            })
            assert websocket.receive_json()["type"] == "device.status"
            silent_pcm = bytes(640)

            def send_frame(sequence: int) -> None:
                websocket.send_bytes(encode_frame(AudioFrame(
                    sequence, sequence + 1, 16_000, 320, 1, silent_pcm,
                )))

            send_frame(0)
            send_frame(2)
            while True:
                message = websocket.receive_json()
                if message["type"] == "audio.nack":
                    assert message["payload"]["expected_sequence"] == 1
                    break

            send_frame(1)
            send_frame(2)
            accepted: set[int] = set()
            while accepted != {1, 2}:
                message = websocket.receive_json()
                if message["type"] == "audio.ack":
                    accepted.add(message["payload"]["sequence"])


def test_slow_telemetry_client_does_not_block_audio_telemetry_producer() -> None:
    class SlowClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_json(self, _message) -> None:
            self.started.set()
            await self.release.wait()

    async def verify() -> None:
        client = SlowClient()
        application = SimpleNamespace(state=SimpleNamespace(
            telemetry_history=defaultdict(list),
            telemetry_clients={client},
            telemetry_broadcast_queue=asyncio.Queue(maxsize=2),
        ))
        worker = asyncio.create_task(telemetry_broadcast_loop(application))
        try:
            await _publish_telemetry(application, {"device_id": "test", "sequence": 1})
            await asyncio.wait_for(client.started.wait(), timeout=0.2)
            await _publish_telemetry(application, {"device_id": "test", "sequence": 2})
            assert application.state.telemetry_history["test"][-1]["sequence"] == 2
            assert application.state.telemetry_broadcast_queue.qsize() == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(verify())


def test_slow_voice_command_does_not_block_audio_websocket(tmp_path, monkeypatch) -> None:
    import app.websocket.device as device_websocket

    processing_started = threading.Event()
    greeting_started = threading.Event()

    class Detector:
        def __init__(self, _model_path: str, _threshold: float, *, wake_phrase: str) -> None:
            assert wake_phrase == "hey buddy"
            self.detected = False

        def process(self, _pcm: bytes) -> tuple[bool, float]:
            if self.detected:
                return False, 0.0
            self.detected = True
            return True, 0.99

    async def slow_command(*_args) -> None:
        processing_started.set()
        await asyncio.sleep(1.0)

    async def no_op_greeting(*_args) -> None:
        greeting_started.set()
        return

    monkeypatch.setattr(device_websocket, "WakeWordDetector", Detector)
    monkeypatch.setattr(device_websocket, "process_command_audio", slow_command)
    monkeypatch.setattr(device_websocket, "speak_wake_greeting", no_op_greeting)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'slow-command.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=400,
        vad_end_silence_ms=40,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
        wake_word_model_path="test-model",
    )
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect(
            "/api/v1/ws/devices/test/audio",
            headers={"Authorization": f"Bearer {settings.device_token}"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "hello",
                "device_id": "test",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {},
            })
            assert websocket.receive_json()["type"] == "device.status"
            voiced = encode_frame(AudioFrame(
                0, 1, 16_000, 320, 1, struct.pack("<320h", *([1000] * 320)),
            ))
            silent = encode_frame(AudioFrame(1, 2, 16_000, 320, 1, bytes(640)))
            silent_end = encode_frame(AudioFrame(2, 3, 16_000, 320, 1, bytes(640)))
            websocket.send_bytes(voiced)
            websocket.send_bytes(silent)
            websocket.send_bytes(silent_end)

            while True:
                message = websocket.receive_json()
                if message["type"] == "audio.ack" and message["payload"]["sequence"] == 2:
                    break
            assert greeting_started.wait(timeout=0.5)
            assert processing_started.wait(timeout=0.5)

            for sequence in (3, 4, 5):
                websocket.send_bytes(encode_frame(AudioFrame(
                    sequence, sequence + 1, 16_000, 320, 1,
                    struct.pack("<320h", *([1000] * 320)),
                )))
            for sequence in (6, 7):
                websocket.send_bytes(encode_frame(AudioFrame(
                    sequence, sequence + 1, 16_000, 320, 1, bytes(640),
                )))
            started_at = time.monotonic()
            while True:
                message = websocket.receive_json()
                if message["type"] == "audio.ack" and message["payload"]["sequence"] == 7:
                    break
            assert processing_started.wait(timeout=0.5)
            assert time.monotonic() - started_at < 0.5


def test_personalization_is_encrypted_opt_in_and_deletable(tmp_path) -> None:
    from cryptography.fernet import Fernet
    from app.core.security import issue_access_token
    from app.models import AuditLog, User, VoiceInteraction
    from app.services.assistant.personalization import learned_instructions, record_interaction

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'personalization.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
        voice_encryption_key=Fernet.generate_key().decode(),
    )
    application = create_app(settings)
    with TestClient(application) as client:
        login = client.post("/api/v1/auth/login", json={
            "username": "admin",
            "password": "test-password-that-is-long",
        })
        with application.state.db_factory() as db:
            user_id = db.query(User).one().id
            db.add(AuditLog(
                event_type="voice.transcription",
                source="device",
                action="speech transcription",
                result="success",
                metadata_json={"user_id": user_id, "transcript": "old transcript"},
            ))
            db.commit()
        headers = {"Authorization": "Bearer " + issue_access_token(
            user_id,
            "admin",
            settings.auth_signing_secret,
            settings.access_token_minutes * 60,
        )}
        assert client.get("/api/v1/assistant/personalization", headers=headers).json()["enabled"] is False
        assert client.put(
            "/api/v1/assistant/personalization",
            headers=headers,
            json={"enabled": True},
        ).status_code == 200
        pcm = b"\x01\x00" * 320
        assert record_interaction(
            application.state.db_factory,
            settings,
            user_id=user_id,
            pcm=pcm,
            transcript="weather please",
            language="en",
        )
        assert "concisely" in learned_instructions(
            application.state.db_factory,
            user_id=user_id,
        )
        with application.state.db_factory() as db:
            stored = db.query(VoiceInteraction).one()
            cipher = Fernet(settings.voice_encryption_key.encode())
            assert cipher.decrypt(stored.encrypted_audio.encode()) == pcm
            assert cipher.decrypt(stored.encrypted_transcript.encode()) == b"weather please"
        assert client.get("/api/v1/assistant/personalization", headers=headers).json()["stored_audio_count"] == 1
        assert client.delete(
            "/api/v1/assistant/personalization/history",
            headers=headers,
        ).status_code == 204
        status = client.get("/api/v1/assistant/personalization", headers=headers).json()
        assert status["stored_audio_count"] == 0
        assert status["interaction_count"] == 0
        with application.state.db_factory() as db:
            old_transcription = db.query(AuditLog).filter_by(event_type="voice.transcription").one()
            assert "transcript" not in old_transcription.metadata_json


def test_wake_briefing_includes_reminders_todos_and_calendar(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from app.models import CalendarEvent, Reminder, Todo, User
    from app.services.assistant.orchestrator import _daily_briefing

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'briefing.db'}",
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
    application = create_app(settings)
    with TestClient(application):
        with application.state.db_factory() as db:
            user = db.query(User).one()
            now = datetime.now(timezone.utc)
            db.add_all([
                Reminder(title="Take medicine", due_at=now + timedelta(hours=1),
                         timezone_name="UTC", created_by=user.id),
                Todo(title="Send report", created_by=user.id),
                CalendarEvent(title="Team meeting", kind="event", start_at=now + timedelta(hours=2),
                              timezone_name="UTC", created_by=user.id),
            ])
            db.commit()
        briefing = _daily_briefing(application, user.id)
        assert "1 active reminders" in briefing
        assert "Take medicine" in briefing
        assert "1 pending to-do items" in briefing
        assert "Team meeting" in briefing


def test_personalization_cannot_be_enabled_without_encryption_key(tmp_path) -> None:
    from app.core.security import issue_access_token
    from app.models import User

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'no-personalization-key.db'}",
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
    with TestClient(create_app(settings)) as client:
        application = client.app
        with application.state.db_factory() as db:
            user_id = db.query(User).one().id
        response = client.put(
            "/api/v1/assistant/personalization",
            headers={"Authorization": "Bearer " + issue_access_token(
                user_id,
                "admin",
                settings.auth_signing_secret,
                settings.access_token_minutes * 60,
            )},
            json={"enabled": True},
        )
        assert response.status_code == 503
