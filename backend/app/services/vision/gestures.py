"""Local hand-gesture detection and conservative action debouncing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

GESTURE_STATES = {"Thumb_Up": "happy", "Victory": "excited", "Open_Palm": "curious"}
MIN_SCORE = 0.8


class GestureUnavailable(RuntimeError):
    pass


class GestureRecognizer:
    def __init__(self, model_path: str) -> None:
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise GestureUnavailable("gesture model file is missing; configure GESTURE_MODEL_PATH")
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision
        except ImportError as exc:
            raise GestureUnavailable("install backend/requirements-gestures.txt on a supported Python runtime") from exc
        self._mp = mp
        try:
            options = vision.GestureRecognizerOptions(
                base_options=python.BaseOptions(model_asset_path=str(path)),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=2,
            )
            self._recognizer = vision.GestureRecognizer.create_from_options(options)
        except (ValueError, RuntimeError, OSError) as exc:
            raise GestureUnavailable("gesture recognizer could not load its model") from exc

    def detect(self, frame: np.ndarray) -> str | None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._recognizer.recognize(image)
        if len(result.gestures) != 1 or not result.gestures[0]:
            return None
        category = result.gestures[0][0]
        if category.score < MIN_SCORE or category.category_name not in GESTURE_STATES:
            return None
        return category.category_name

    def close(self) -> None:
        self._recognizer.close()


@dataclass
class GestureGate:
    current: str | None = None
    identity: str | None = None
    count: int = 0
    latched: bool = False

    def update(self, identity: str | None, gesture: str | None) -> str | None:
        if identity is None or gesture not in GESTURE_STATES:
            self.current = self.identity = None
            self.count = 0
            self.latched = False
            return None
        if gesture != self.current or identity != self.identity:
            self.current, self.identity, self.count, self.latched = gesture, identity, 1, False
        else:
            self.count += 1
        if self.count >= 3 and not self.latched:
            self.latched = True
            return GESTURE_STATES[gesture]
        return None
