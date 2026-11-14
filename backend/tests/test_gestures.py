import asyncio

import numpy as np

from app.services.assistant import orchestrator
from app.services.vision.gestures import GestureGate, GestureRecognizer, GestureUnavailable
from backend.tests.test_vision import FRAME, make_monitor, vec


def test_gate_requires_three_matching_samples_and_releases():
    gate = GestureGate()
    assert gate.update("owner", "Thumb_Up") is None
    assert gate.update("owner", "Thumb_Up") is None
    assert gate.update("owner", "Thumb_Up") == "happy"
    assert gate.update("owner", "Thumb_Up") is None
    assert gate.update(None, None) is None
    assert gate.update("owner", "Victory") is None
    assert gate.update("other", "Victory") is None


def test_missing_model_is_explicit(tmp_path):
    try:
        GestureRecognizer(str(tmp_path / "missing.task"))
    except GestureUnavailable as exc:
        assert "GESTURE_MODEL_PATH" in str(exc)
    else:
        raise AssertionError("missing model was accepted")


def test_owner_only_gesture_dispatch(tmp_path, monkeypatch):
    monitor, _ = make_monitor(tmp_path)
    monitor.people = {"owner": {"role": "owner"}, "stranger": {"role": "stranger"}}
    monitor.application.state.device_sockets = {"device": object()}
    sent = []

    class Detector:
        def detect(self, frame: np.ndarray) -> str:
            return "Thumb_Up"

    async def send(_socket, device, kind, payload):
        sent.append((device, kind, payload))

    async def log(*_args):
        pass

    monkeypatch.setattr(orchestrator, "_send_json", send)
    monitor._log = log
    monitor.gesture_recognizer = Detector()
    monitor.enabled = True

    async def scenario():
        owner = (vec(1), "owner", 0.9)
        stranger = (vec(2), "stranger", 0.9)
        await monitor._process_gesture(FRAME, [owner, stranger])
        await monitor._process_gesture(FRAME, [stranger])
        for _ in range(3):
            await monitor._process_gesture(FRAME, [owner])
        await monitor._process_gesture(FRAME, [owner])

    asyncio.run(scenario())
    assert sent == [("device", "animation.state", {"state": "happy"})]
