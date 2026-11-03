import pytest

from app.services.wake_word.transcript import matches_wake_phrase


@pytest.mark.parametrize("text", [
    "Hey Buddy", "hey, buddy.", "Hey buddy what time is it", "hi buddy", "Hey Body", "heybuddy", "Buddy", "a buddy",
])
def test_wake_phrase_variants_match(text):
    assert matches_wake_phrase(text)


@pytest.mark.parametrize("text", ["", "what time is it", "hello there everyone", "the weather is nice today", "my friend is a bad guy"])
def test_unrelated_speech_does_not_match(text):
    assert not matches_wake_phrase(text)


def test_adaptive_vad_detects_speech_over_loud_room_noise() -> None:
    from app.services.audio.vad import VadGate

    fixed = VadGate(450, 900)
    adaptive = VadGate(450, 900, adaptive=True)
    events = {"fixed": [], "adaptive": []}
    levels = [900] * 400 + [4500] * 30 + [900] * 80  # 20 ms frames: noise, speech, noise
    for level in levels:
        for name, gate in (("fixed", fixed), ("adaptive", adaptive)):
            transition = gate.update(level, 20)
            if transition:
                events[name].append(transition)
    assert events["fixed"] == ["speech_started"]  # stuck active: never sees the speech start or end
    assert events["adaptive"] == ["speech_started", "speech_ended"] or events["adaptive"][-2:] == ["speech_started", "speech_ended"]


def test_vad_reset_clears_stuck_state() -> None:
    from app.services.audio.vad import VadGate

    gate = VadGate(450, 900)
    gate.update(1000, 20)
    gate.reset()
    assert gate.update(1000, 20) == "speech_started"
