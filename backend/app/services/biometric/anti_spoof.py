from __future__ import annotations

from functools import lru_cache
from pathlib import Path


class AntiSpoofUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=2)
def _session(model_path: str):
    path = Path(model_path)
    if not path.is_file():
        raise AntiSpoofUnavailable(f"anti-spoof model does not exist: {path}")
    try:
        import onnxruntime

        return onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except ImportError as exc:
        raise AntiSpoofUnavailable("install ONNX Runtime for anti-spoof inference") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise AntiSpoofUnavailable("anti-spoof model could not be loaded") from exc


def bona_fide_score(pcm: bytes, model_path: str, bona_fide_class: int = 1) -> float:
    if not pcm or len(pcm) % 2:
        raise ValueError("anti-spoof input must be non-empty PCM16")
    try:
        import numpy as np
    except ImportError as exc:
        raise AntiSpoofUnavailable("install NumPy for anti-spoof inference") from exc
    session = _session(model_path)
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise AntiSpoofUnavailable("anti-spoof ONNX model must accept exactly one waveform input")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    shape = inputs[0].shape
    if len(shape) not in {2, 3}:
        raise AntiSpoofUnavailable("anti-spoof model input must have [batch, samples] or [batch, channel, samples] shape")
    expected_samples = shape[-1]
    if isinstance(expected_samples, int):
        if expected_samples <= 0:
            raise AntiSpoofUnavailable("anti-spoof model declares an invalid sample dimension")
        if samples.size < expected_samples:
            samples = np.pad(samples, (0, expected_samples - samples.size))
        elif samples.size > expected_samples:
            samples = samples[:expected_samples]
    tensor = samples[np.newaxis, :]
    if len(shape) == 3:
        tensor = tensor[:, np.newaxis, :]
    try:
        outputs = session.run(None, {inputs[0].name: tensor})
    except (RuntimeError, ValueError) as exc:
        raise AntiSpoofUnavailable("anti-spoof inference failed") from exc
    if not outputs:
        raise AntiSpoofUnavailable("anti-spoof model returned no scores")
    scores = np.asarray(outputs[0], dtype=np.float64).reshape(-1)
    if scores.size < 2 or not np.isfinite(scores).all():
        raise AntiSpoofUnavailable("anti-spoof model must return finite class scores")
    if not 0 <= bona_fide_class < scores.size:
        raise AntiSpoofUnavailable("configured bona-fide class index is outside model output")
    if np.all(scores >= 0) and np.isclose(float(scores.sum()), 1.0, atol=1e-3):
        probabilities = scores
    else:
        shifted = scores - scores.max()
        exponentials = np.exp(shifted)
        probabilities = exponentials / exponentials.sum()
    return float(probabilities[bona_fide_class])
