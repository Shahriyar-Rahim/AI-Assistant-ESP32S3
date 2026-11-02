import asyncio
import io
import struct
import wave

import httpx
import pytest

from app.services.ai.providers import GeminiProvider, OllamaProvider, OpenAIProvider
from app.services.stt.whisper_api import validate_wav
from app.services.tts.openai_api import resample_24k_to_16k
from app.services.wake_word.detector import WakeWordDetector


def test_wake_detector_collects_multiple_pcm_frames_into_model_windows() -> None:
    detector = WakeWordDetector(
        "test-model.onnx",
        0.6,
        wake_phrase="hey buddy",
        inference=lambda _: {"hey_buddy": 0.8, "alexa": 0.99},
    )
    for _ in range(3):
        assert detector.process(bytes(320 * 2)) == (False, None)
    assert detector.process(bytes(320 * 2)) == (True, 0.8)


def test_wake_detector_ignores_other_wake_labels() -> None:
    detector = WakeWordDetector(
        "test-model.onnx",
        0.6,
        wake_phrase="hey buddy",
        inference=lambda _: {"alexa": 0.99, "hey_assistant": 0.95},
    )
    assert detector.process(bytes(1280 * 2)) == (False, None)


def test_pcm_resampling_produces_16khz_sample_count() -> None:
    source = struct.pack("<2400h", *([1000] * 2400))
    result = resample_24k_to_16k(source)
    assert len(result) == 1600 * 2
    assert struct.unpack_from("<h", result)[0] == 1000


def test_wav_validator_checks_pcm_header_and_duration() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(bytes(16_000 * 2))
    validate_wav(buffer.getvalue(), max_bytes=100_000)
    with pytest.raises(ValueError, match="valid PCM WAV"):
        validate_wav(b"not wav", max_bytes=100_000)


def test_provider_adapters_parse_provider_usage(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-a-real-credential")

    async def openai_request(self, method, url, **kwargs):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        })

    monkeypatch.setattr(OpenAIProvider, "_request", openai_request)
    openai_result = asyncio.run(OpenAIProvider().complete([{"role": "user", "content": "hi"}], "test-model"))
    assert openai_result.text == "hello" and openai_result.usage["completion_tokens"] == 3

    async def gemini_request(self, method, url, **kwargs):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "namaste"}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 5},
        })

    monkeypatch.setattr(GeminiProvider, "_request", gemini_request)
    gemini_result = asyncio.run(GeminiProvider().complete([{"role": "user", "content": "hi"}], "test-model"))
    assert gemini_result.text == "namaste" and gemini_result.usage["prompt_tokens"] == 4

    async def ollama_request(self, method, url, **kwargs):
        return httpx.Response(200, json={
            "message": {"content": "local"},
            "prompt_eval_count": 6,
            "eval_count": 7,
        })

    monkeypatch.setattr(OllamaProvider, "_request", ollama_request)
    ollama_result = asyncio.run(OllamaProvider().complete([{"role": "user", "content": "hi"}], "test-model"))
    assert ollama_result.text == "local" and ollama_result.usage["completion_tokens"] == 7
