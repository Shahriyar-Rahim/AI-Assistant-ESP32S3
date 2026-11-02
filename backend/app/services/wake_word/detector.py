from __future__ import annotations

from pathlib import Path
import re
from typing import Callable


class WakeWordUnavailable(RuntimeError):
    pass


class WakeWordDetector:
    def __init__(
        self,
        model_path: str,
        threshold: float,
        *,
        wake_phrase: str = "hey buddy",
        inference: Callable[[object], dict[str, float]] | None = None,
    ) -> None:
        if not 0.1 <= threshold <= 0.99:
            raise ValueError("wake word threshold must be between 0.1 and 0.99")
        if not wake_phrase.strip():
            raise ValueError("wake phrase cannot be empty")
        self.model_path = model_path
        self.threshold = threshold
        self.wake_phrase = wake_phrase
        self._wake_label = re.sub(r"[^a-z0-9]", "", wake_phrase.casefold())
        self._inference = inference or self._load_model(model_path)
        self._buffer = bytearray()

    @staticmethod
    def _load_model(model_path: str) -> Callable[[object], dict[str, float]]:
        path = Path(model_path)
        if not path.is_file():
            raise WakeWordUnavailable(f"configured wake-word model does not exist: {path}")
        try:
            import numpy as np
            from openwakeword.model import Model
            model = Model(wakeword_models=[str(path)], inference_framework="onnx")
        except ImportError as exc:
            raise WakeWordUnavailable("install backend/requirements-wakeword.txt") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise WakeWordUnavailable("wake-word model could not be loaded") from exc

        def infer(pcm: object) -> dict[str, float]:
            samples = np.frombuffer(pcm, dtype="<i2")
            return {str(name): float(score) for name, score in model.predict(samples).items()}

        return infer

    def process(self, pcm_s16le: bytes) -> tuple[bool, float | None]:
        if len(pcm_s16le) % 2:
            raise ValueError("PCM data must contain whole 16-bit samples")
        self._buffer.extend(pcm_s16le)
        window_bytes = 1280 * 2
        detected = False
        strongest: float | None = None
        while len(self._buffer) >= window_bytes:
            window = bytes(self._buffer[:window_bytes])
            del self._buffer[:window_bytes]
            scores = self._inference(window)
            if not scores:
                continue
            matching_scores = [
                score for label, score in scores.items()
                if re.sub(r"[^a-z0-9]", "", str(label).casefold()).startswith(self._wake_label)
            ]
            if not matching_scores:
                continue
            score = max(matching_scores)
            strongest = score if strongest is None else max(strongest, score)
            detected = detected or score >= self.threshold
        return detected, strongest
