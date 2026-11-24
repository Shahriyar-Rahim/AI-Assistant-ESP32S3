from __future__ import annotations

from functools import lru_cache
import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
MIN_DYNAMIC_SAMPLES = 64_000


class AntiSpoofUnavailable(RuntimeError):
    pass


def default_bona_fide_class() -> int:
    raw = os.getenv("ANTI_SPOOF_BONA_FIDE_CLASS", "1").strip()
    try:
        return int(raw)
    except ValueError:
        return 1


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
    except Exception as exc:
        raise AntiSpoofUnavailable("anti-spoof model could not be loaded") from exc


def trim_silence(
    pcm: bytes,
    sample_rate: int = 16_000,
    frame_ms: int = 20,
    pad_frames: int = 5,
    min_active_ms: int = 600,
) -> bytes:
    """Strips leading and trailing low-energy frames while keeping a small speech margin."""
    if not pcm or len(pcm) % 2:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2")
    frame_len = sample_rate * frame_ms // 1000
    if samples.size < frame_len * 4:
        return pcm

    usable = (samples.size // frame_len) * frame_len
    frames = samples[:usable].reshape(-1, frame_len).astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1))

    peak_rms = float(np.max(rms))
    if peak_rms < 150.0:
        return pcm

    # ESP32 INMP441 idle noise floor is ~250-400 RMS, so use 35% of peak RMS
    threshold = max(450.0, peak_rms * 0.35)
    active = np.flatnonzero(rms >= threshold)
    if active.size == 0:
        return pcm

    start_frame = max(0, int(active[0]) - pad_frames)
    end_frame = min(frames.shape[0], int(active[-1]) + 1 + pad_frames)

    if (end_frame - start_frame) * frame_ms < min_active_ms:
        return pcm

    return samples[start_frame * frame_len : end_frame * frame_len].tobytes()


def bona_fide_score(pcm: bytes, model_path: str, bona_fide_class: int | None = None) -> float:
    if not pcm or len(pcm) % 2:
        raise ValueError("anti-spoof input must be non-empty PCM16")

    if model_path.strip().lower() in {"", "none", "disabled", "off"}:
        return 1.0

    if bona_fide_class is None:
        bona_fide_class = default_bona_fide_class()

    pcm = trim_silence(pcm, min_active_ms=600)

    session = _session(model_path)
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise AntiSpoofUnavailable("anti-spoof ONNX model must accept exactly one waveform input")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size > 0:
        samples = samples - float(np.mean(samples))
        peak = float(np.max(np.abs(samples)))
        if peak > 1e-4:
            samples = (samples / peak) * 0.9
    shape = inputs[0].shape
    if len(shape) not in {2, 3}:
        raise AntiSpoofUnavailable("anti-spoof model input must have [batch, samples] or [batch, channel, samples] shape")
    expected_samples = shape[-1]
    if isinstance(expected_samples, int):
        if expected_samples <= 0:
            raise AntiSpoofUnavailable("anti-spoof model declares an invalid sample dimension")
        if samples.size < expected_samples:
            repeats = (expected_samples + samples.size - 1) // samples.size
            samples = np.tile(samples, repeats)[:expected_samples]
        elif samples.size > expected_samples:
            samples = samples[:expected_samples]
    elif samples.size < MIN_DYNAMIC_SAMPLES:
        repeats = (MIN_DYNAMIC_SAMPLES + samples.size - 1) // samples.size
        samples = np.tile(samples, repeats)[:MIN_DYNAMIC_SAMPLES]
    tensor = samples[np.newaxis, :]
    if len(shape) == 3:
        tensor = tensor[:, np.newaxis, :]
    try:
        outputs = session.run(None, {inputs[0].name: tensor})
    except Exception as exc:
        raise AntiSpoofUnavailable(f"anti-spoof inference failed: {exc}") from exc
    if not outputs:
        raise AntiSpoofUnavailable("anti-spoof model returned no scores")

    logit_tensor = next(
        (out for out in outputs if np.asarray(out).reshape(-1).size == 2),
        outputs[-1],
    )
    scores = np.asarray(logit_tensor, dtype=np.float64).reshape(-1)
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
    score = float(probabilities[bona_fide_class])
    logger.warning(
        "Anti-spoof raw logits=%s probs=%s bona_fide_class=%d score=%.4f",
        scores.tolist(),
        probabilities.tolist(),
        bona_fide_class,
        score,
    )
    return score
