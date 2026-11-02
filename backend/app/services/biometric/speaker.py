from __future__ import annotations

import os
import wave
from functools import lru_cache
from io import BytesIO


MODEL_NAME = "resemblyzer-voice-encoder"


class BiometricUnavailable(RuntimeError):
    pass


def _fernet():
    key = os.getenv("VOICE_ENCRYPTION_KEY", "")
    if not key:
        raise BiometricUnavailable("VOICE_ENCRYPTION_KEY must be configured")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except ImportError as exc:
        raise BiometricUnavailable("install backend/requirements-voice.txt for encrypted voice embeddings") from exc
    except (ValueError, TypeError) as exc:
        raise BiometricUnavailable("VOICE_ENCRYPTION_KEY must be a valid Fernet key") from exc


@lru_cache(maxsize=1)
def _encoder():
    try:
        from resemblyzer import VoiceEncoder
        return VoiceEncoder(device="cpu", verbose=False)
    except (ImportError, OSError, RuntimeError) as exc:
        raise BiometricUnavailable(
            "install backend/requirements-voice.txt and provide the Resemblyzer model weights"
        ) from exc


def _audio_array(audio: bytes):
    try:
        import numpy as np
    except ImportError as exc:
        raise BiometricUnavailable("install backend/requirements-voice.txt for voice embedding support") from exc
    try:
        with wave.open(BytesIO(audio), "rb") as wav:
            rate, channels, width, count = wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()
            raw = wav.readframes(count)
    except (wave.Error, EOFError) as exc:
        raise ValueError("audio upload must be a valid PCM WAV file") from exc
    if channels not in (1, 2) or width not in (1, 2, 3, 4):
        raise ValueError("unsupported WAV channel count or sample width")
    if not 8_000 <= rate <= 96_000:
        raise ValueError("WAV sample rate must be between 8000 and 96000 Hz")
    if not 3 <= count / rate <= 20:
        raise ValueError("voice sample must be between 3 and 20 seconds")
    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        values = octets[:, 0].astype(np.int32) | (octets[:, 1].astype(np.int32) << 8) | (octets[:, 2].astype(np.int32) << 16)
        values = (values ^ 0x800000) - 0x800000
        samples = values.astype(np.float32) / 8388608
    else:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    clipped = float(np.mean(np.abs(samples) >= 0.999))
    if rms < 0.005 or clipped > 0.05:
        raise ValueError("voice sample audio quality is insufficient or clipped")
    target_count = round(len(samples) * 16_000 / rate)
    source_positions = np.arange(len(samples), dtype=np.float64)
    target_positions = np.arange(target_count, dtype=np.float64) * rate / 16_000
    normalized = np.interp(target_positions, source_positions, samples).astype(np.float32)
    return normalized, 16_000


def create_enrollment(audio: bytes) -> str:
    waveform, sample_rate = _audio_array(audio)
    import numpy as np
    try:
        from resemblyzer import preprocess_wav
        processed = preprocess_wav(waveform, source_sr=sample_rate)
        embedding = np.asarray(_encoder().embed_utterance(processed), dtype="<f4")
    except BiometricUnavailable:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise BiometricUnavailable("voice embedding model could not process the sample") from exc
    if embedding.ndim != 1 or embedding.size < 10 or not np.isfinite(embedding).all():
        raise BiometricUnavailable("voice embedding model returned invalid data")
    return _fernet().encrypt(embedding.tobytes()).decode()


def compare_voice(audio: bytes, encrypted_embedding: str) -> float:
    from cryptography.fernet import InvalidToken

    waveform, sample_rate = _audio_array(audio)
    import numpy as np
    try:
        from resemblyzer import preprocess_wav
        actual = np.asarray(
            _encoder().embed_utterance(preprocess_wav(waveform, source_sr=sample_rate)),
            dtype=np.float32,
        )
        reference_bytes = _fernet().decrypt(encrypted_embedding.encode())
    except InvalidToken as exc:
        raise BiometricUnavailable("stored voice embedding could not be decrypted") from exc
    reference = np.frombuffer(reference_bytes, dtype="<f4").astype(np.float32)
    if actual.shape != reference.shape or not np.isfinite(actual).all() or not np.isfinite(reference).all():
        raise BiometricUnavailable("voice embedding dimensions are invalid")
    denominator = float(np.linalg.norm(actual) * np.linalg.norm(reference))
    if denominator == 0:
        return 0.0
    return float(np.dot(actual, reference) / denominator)
