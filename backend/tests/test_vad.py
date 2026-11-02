import pytest

from app.services.audio.vad import VadGate


def test_vad_detects_speech_then_end_after_configured_silence() -> None:
    vad = VadGate(threshold=400, end_silence_ms=500)
    assert vad.update(100, 20) is None
    assert vad.update(700, 20) == "speech_started"
    assert vad.update(0, 200) is None
    assert vad.update(0, 300) == "speech_ended"
    assert vad.active is False


def test_vad_rejects_invalid_duration() -> None:
    with pytest.raises(ValueError):
        VadGate(100, 500).update(200, 0)
