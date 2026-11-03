from __future__ import annotations

import pytest

from app.services.audio.framing import AudioFrame
from simulator.device_simulator import (
    OBSERVABLE_EVENTS,
    audio_packet,
    load_pcm_file,
)


def test_simulator_builds_protocol_audio_frames_and_marks_observable_scenarios() -> None:
    pcm = b"\x10\x00" * 320
    packet = audio_packet(7, pcm)
    frame = AudioFrame.decode(packet, max_bytes=4096)

    assert frame.sequence == 7
    assert frame.sample_rate == 16_000
    assert frame.sample_count == 320
    assert frame.pcm == pcm
    assert {
        "assistant.response", "biometric.result", "calendar.notification",
        "display.state", "game.state", "ota.available", "sound.effect",
        "tts.end",
    } <= OBSERVABLE_EVENTS


def test_simulator_pcm_fixture_requires_exact_20ms_pcm16_blocks(tmp_path) -> None:
    fixture = tmp_path / "voice.pcm"
    fixture.write_bytes(bytes(640))
    assert load_pcm_file(fixture) == bytes(640)

    fixture.write_bytes(bytes(639))
    with pytest.raises(ValueError, match="20 ms"):
        load_pcm_file(fixture)

    with pytest.raises(ValueError, match="320 PCM16 samples"):
        audio_packet(0, bytes(638))
