from __future__ import annotations

import asyncio
import os
from functools import lru_cache


class LocalSpeechUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=2)
def _load_model(model_name: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise LocalSpeechUnavailable(
            "install backend/requirements-local-whisper.txt to enable local Whisper"
        ) from exc
    try:
        return WhisperModel(model_name, device=device, compute_type=compute_type)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalSpeechUnavailable("local Whisper model could not be loaded") from exc


def pcm16le_to_wav(pcm: bytes, sample_rate: int = 16_000, channels: int = 1) -> bytes:
    if not pcm or len(pcm) % (2 * channels):
        raise ValueError("PCM audio must contain complete 16-bit samples for every channel")
    if sample_rate != 16_000 or channels != 1:
        raise ValueError("local voice pipeline accepts 16 kHz mono PCM only")
    import wave
    from io import BytesIO

    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def transcribe_pcm_sync(
    pcm: bytes,
    *,
    language: str,
    model_name: str = "base",
    device: str = "cpu",
    compute_type: str = "int8",
) -> tuple[str, dict[str, object]]:
    if language not in {"en", "bn"}:
        raise ValueError("language must be 'en' or 'bn'")
    pcm16le_to_wav(pcm)
    try:
        import numpy as np
    except ImportError as exc:
        raise LocalSpeechUnavailable("install NumPy to preprocess local Whisper audio") from exc
    waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    try:
        segments, info = _load_model(model_name, device, compute_type).transcribe(
            waveform,
            language=language,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
    except LocalSpeechUnavailable:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalSpeechUnavailable("local Whisper inference failed") from exc
    if not transcript:
        raise ValueError("speech was not recognized")
    return transcript, {
        "provider": "faster-whisper",
        "model": model_name,
        "language": getattr(info, "language", language),
    }


async def transcribe_pcm(
    pcm: bytes,
    *,
    language: str,
    model_name: str | None = None,
) -> tuple[str, dict[str, object]]:
    selected_model = model_name or os.getenv("LOCAL_WHISPER_MODEL", "base")
    device = os.getenv("LOCAL_WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("LOCAL_WHISPER_COMPUTE_TYPE", "int8")
    return await asyncio.to_thread(
        transcribe_pcm_sync,
        pcm,
        language=language,
        model_name=selected_model,
        device=device,
        compute_type=compute_type,
    )
