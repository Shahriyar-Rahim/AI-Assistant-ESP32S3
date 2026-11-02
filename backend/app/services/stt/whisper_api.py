from __future__ import annotations

import os
import wave
from io import BytesIO

import httpx


class SpeechServiceError(RuntimeError):
    pass


def validate_wav(audio: bytes, max_bytes: int, max_seconds: int = 30) -> None:
    if not audio or len(audio) > max_bytes:
        raise ValueError(f"WAV upload must be between 1 and {max_bytes} bytes")
    try:
        with wave.open(BytesIO(audio), "rb") as wav:
            if wav.getnchannels() not in (1, 2):
                raise ValueError("WAV audio must be mono or stereo")
            if wav.getsampwidth() not in (1, 2, 3, 4):
                raise ValueError("unsupported WAV sample width")
            if wav.getframerate() < 8_000 or wav.getframerate() > 96_000:
                raise ValueError("WAV sample rate must be between 8000 and 96000 Hz")
            if wav.getnframes() == 0 or wav.getnframes() > max_seconds * wav.getframerate():
                raise ValueError(f"WAV duration must be between 1 and {max_seconds} seconds")
    except (wave.Error, EOFError) as exc:
        raise ValueError("audio upload must be a valid PCM WAV file") from exc


async def transcribe_wav(audio: bytes, language: str, model: str) -> tuple[str, dict[str, object]]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SpeechServiceError("OPENAI_API_KEY is required for Whisper transcription")
    allowed_languages = {"en", "bn"}
    if language not in allowed_languages:
        raise ValueError("language must be 'en' or 'bn'")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": model, "language": language, "response_format": "json"},
                files={"file": ("audio.wav", audio, "audio/wav")},
            )
        response.raise_for_status()
        payload = response.json()
        transcript = payload.get("text")
        if not isinstance(transcript, str):
            raise SpeechServiceError("Whisper returned an invalid response")
        return transcript, {"provider": "openai", "model": model, "language": language}
    except httpx.HTTPStatusError as exc:
        raise SpeechServiceError(f"Whisper API returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise SpeechServiceError("Whisper API request failed") from exc
