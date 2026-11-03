from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from io import BytesIO
import shutil
import subprocess
import wave

from app.services.tts.openai_api import TTSServiceError, resample_pcm

logger = logging.getLogger(__name__)
_VOICES = {"en": "en-us+f3", "bn": "bn+f3"}


def _synthesize(text: str, voice: str) -> bytes:
    executable = shutil.which("espeak-ng")
    if executable is None:
        raise TTSServiceError("eSpeak NG is not installed for local TTS fallback")
    try:
        result = subprocess.run(
            [executable, "--stdout", "-v", voice, "-p", "60", "-s", "160", text],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TTSServiceError("Local TTS process failed") from exc
    if result.returncode != 0:
        raise TTSServiceError("eSpeak NG could not synthesize speech")
    try:
        with wave.open(BytesIO(result.stdout), "rb") as audio:
            if audio.getcomptype() != "NONE" or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise TTSServiceError("eSpeak NG returned an unsupported audio format")
            return resample_pcm(audio.readframes(audio.getnframes()), audio.getframerate(), 16_000)
    except (EOFError, wave.Error, ValueError) as exc:
        raise TTSServiceError("eSpeak NG returned invalid WAV audio") from exc


def piper_model_path() -> Path | None:
    configured = os.getenv("PIPER_VOICE_MODEL", "").strip()
    default = Path(__file__).resolve().parents[3] / "voices" / "en_US-amy-medium.onnx"
    path = Path(configured).expanduser() if configured else default
    return path if path.is_file() else None


@lru_cache(maxsize=1)
def _load_piper(model: str):
    from piper import PiperVoice

    return PiperVoice.load(model)


def _synthesize_piper(text: str, model: str) -> bytes:
    try:
        voice = _load_piper(model)
        chunks = list(voice.synthesize(text))
    except Exception as exc:
        raise TTSServiceError("Piper TTS failed") from exc
    if not chunks:
        raise TTSServiceError("Piper TTS returned no audio")
    rate = chunks[0].sample_rate
    pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
    return resample_pcm(pcm, rate, 16_000)


async def synthesize_local_pcm(text: str, language: str) -> bytes:
    if not text.strip():
        raise ValueError("TTS text cannot be empty")
    model = piper_model_path() if language == "en" else None
    if model is not None:
        try:
            return await asyncio.to_thread(_synthesize_piper, text, str(model))
        except TTSServiceError as exc:
            logger.warning("Piper unavailable, using eSpeak NG: %s", exc)
    voice = _VOICES.get(language)
    if voice is None:
        raise ValueError("unsupported local TTS language")
    return await asyncio.to_thread(_synthesize, text, voice)
