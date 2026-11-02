from __future__ import annotations

import os
import struct
from array import array

import httpx


class TTSServiceError(RuntimeError):
    pass


def resample_24k_to_16k(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM16 audio must contain whole samples")
    samples = array("h")
    samples.frombytes(pcm)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        samples.byteswap()
    if not samples:
        return b""
    output_count = (len(samples) * 2 + 2) // 3
    converted = array("h")
    for output_index in range(output_count):
        source_position_num = output_index * 3
        left_index = source_position_num // 2
        fraction_num = source_position_num % 2
        right_index = min(left_index + 1, len(samples) - 1)
        value = (samples[left_index] * (2 - fraction_num) + samples[right_index] * fraction_num) // 2
        converted.append(max(-32768, min(32767, value)))
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        converted.byteswap()
    return converted.tobytes()


async def synthesize_pcm(text: str, model: str, voice: str) -> bytes:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise TTSServiceError("OPENAI_API_KEY is required for OpenAI TTS")
    if voice not in {"alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse"}:
        raise ValueError("unsupported OpenAI voice")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "voice": voice, "input": text, "response_format": "pcm"},
            )
        response.raise_for_status()
        result = resample_24k_to_16k(response.content)
        if not result:
            raise TTSServiceError("TTS provider returned empty audio")
        return result
    except httpx.HTTPStatusError as exc:
        raise TTSServiceError(f"TTS API returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise TTSServiceError("TTS API request failed") from exc
