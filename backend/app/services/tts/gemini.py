from __future__ import annotations

import base64
import os

import httpx

from app.services.tts.openai_api import TTSServiceError, resample_pcm

GEMINI_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})
_GEMINI_RATE = 24_000


def normalize_voice(voice: str) -> str | None:
    lookup = {name.lower(): name for name in GEMINI_VOICES}
    return lookup.get(voice.strip().lower())


async def synthesize_gemini_pcm(text: str, model: str, voice: str) -> bytes:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise TTSServiceError("GEMINI_API_KEY is required for Gemini TTS")
    voice_name = normalize_voice(voice)
    if voice_name is None:
        raise ValueError("unsupported Gemini voice")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}},
                    },
                },
            )
        response.raise_for_status()
        data = response.json()
        encoded = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        pcm = base64.b64decode(encoded)
    except httpx.HTTPStatusError as exc:
        raise TTSServiceError(f"Gemini TTS returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise TTSServiceError("Gemini TTS request failed") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TTSServiceError("Gemini TTS returned an invalid response") from exc
    if not pcm or len(pcm) % 2:
        raise TTSServiceError("Gemini TTS returned invalid audio")
    return resample_pcm(pcm, _GEMINI_RATE, 16_000)
