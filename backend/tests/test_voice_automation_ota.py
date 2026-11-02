from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.assistant.actions import ACTION_REGISTRY, IntentError, deterministic_intent
from app.services.audio.verification import ReplayGuard
from app.services.stt.local_whisper import pcm16le_to_wav


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'features.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
        firmware_storage_dir=str(tmp_path / "firmware"),
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    result = client.post("/api/v1/auth/login", json={
        "username": "admin",
        "password": "test-password-that-is-long",
    })
    assert result.status_code == 200
    client.headers["Authorization"] = f"Bearer {result.json()['access_token']}"
    return client


def test_local_stt_audio_preprocessing_and_action_allowlist() -> None:
    wav_data = pcm16le_to_wav(b"\x01\x00" * 160)
    assert wav_data[:4] == b"RIFF"
    assert wav_data[24:28] == b"\x80\x3e\x00\x00"
    assert deterministic_intent("Play tic tac toe") == ("game.start", {"game": "tic_tac_toe"})
    assert deterministic_intent("What's on my calendar?") == ("calendar.list", {})
    assert deterministic_intent("Show my reminders on the display") == (
        "display.show_page",
        {"page": "reminders"},
    )
    assert deterministic_intent("Open the to-do page") == ("display.show_page", {"page": "todos"})
    assert deterministic_intent("Show weather page") == ("display.show_page", {"page": "weather"})
    assert deterministic_intent("Remind me to call Sam at 2026-11-02T09:30:00+06:00") == (
        "reminder.create",
        {"title": "call Sam", "due_at": "2026-11-02T09:30:00+06:00", "timezone_name": "UTC"},
    )
    args, _ = ACTION_REGISTRY.validate("game.start", {"game": "hangman"})
    assert args.model_dump() == {"game": "hangman"}
    try:
        ACTION_REGISTRY.validate("system.shell", {"command": "id"})
    except IntentError:
        pass
    else:
        raise AssertionError("unregistered action was accepted")


def test_replay_guard_rejects_repeated_device_audio() -> None:
    guard = ReplayGuard(ttl_seconds=20)
    assert guard.accept("device-a", b"sample", now=100)
    assert not guard.accept("device-a", b"sample", now=101)
    assert guard.accept("device-b", b"sample", now=101)
    assert guard.accept("device-a", b"sample", now=121)


def test_display_automation_rules_and_ota_release_api(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/devices/register", json={"device_id": "edge-1", "name": "Assistant"})
        assert created.status_code == 201
        display = client.put("/api/v1/devices/edge-1/display", json={
            "state": "listening",
            "emoji": "🎙️",
            "animation": "pulse",
            "animation_duration_ms": 450,
            "emoji_rules": {"happy": "😊"},
        })
        assert display.status_code == 200
        assert display.json()["revision"] == 1
        assert client.get("/api/v1/devices/edge-1/display").json()["configuration"]["animation"] == "pulse"

        rule = client.post("/api/v1/automation/rules", json={
            "name": "Cold weather alert",
            "trigger_type": "weather",
            "conditions": {
                "latitude": 23.8,
                "longitude": 90.4,
                "metric": "temperature_2m",
                "operator": "lte",
                "threshold": 10,
            },
            "action": {"kind": "notify", "title": "It is cold"},
            "cooldown_seconds": 1800,
            "enabled": True,
        })
        assert rule.status_code == 201
        assert client.get("/api/v1/automation/rules").json()["rules"][0]["id"] == rule.json()["id"]

        upload = client.post(
            "/api/v1/ota/releases",
            data={"version": "0.2.0", "channel": "beta", "hardware_target": "esp32-s3-n16r8"},
            files={"firmware": ("image.bin", b"\xe9" + bytes(range(255)) * 8, "application/octet-stream")},
        )
        assert upload.status_code == 201, upload.text
        release = upload.json()
        assert len(release["sha256"]) == 64
        assert release["size_bytes"] == 1 + 255 * 8
        assert client.get("/api/v1/ota/releases?channel=beta").json()["releases"][0]["id"] == release["id"]
        deployment = client.post(f"/api/v1/ota/releases/{release['id']}/deploy/edge-1")
        assert deployment.status_code == 200
        assert deployment.json()["delivery"] == "stored_until_reconnect"
        assert deployment.json()["release"]["sha256"] == release["sha256"]

        telemetry = client.get("/api/v1/monitoring/telemetry")
        assert telemetry.status_code == 200
        assert telemetry.json()["samples"] == []


def test_ota_upload_rejects_non_esp_image_and_invalid_display(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.post(
            "/api/v1/ota/releases",
            data={"version": "0.1.1", "channel": "development"},
            files={"firmware": ("bad.bin", b"not-an-esp-image", "application/octet-stream")},
        ).status_code == 422
        assert client.put("/api/v1/devices/missing/display", json={"state": "unknown"}).status_code == 422


def test_telemetry_websocket_authenticates_without_query_string(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = client.headers["Authorization"].removeprefix("Bearer ")
        with client.websocket_connect("/api/v1/monitoring/stream") as websocket:
            websocket.send_json({"access_token": token})
