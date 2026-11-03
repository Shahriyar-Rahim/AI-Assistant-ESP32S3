import asyncio
import io
import struct
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.services.assistant import orchestrator
from app.services.ai.providers import GeminiProvider, OllamaProvider, OpenAIProvider
from app.services.stt.whisper_api import validate_wav
from app.services.tts import local as local_tts
from app.services.tts.openai_api import TTSServiceError, resample_24k_to_16k
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


def test_tts_stream_paces_pcm_chunks_to_firmware_playback_rate(monkeypatch) -> None:
    chunks: list[bytes] = []
    delays: list[float] = []
    messages: list[str] = []

    class FakeWebSocket:
        async def send_bytes(self, data: bytes) -> None:
            chunks.append(data)

    async def fake_synthesize_pcm(text: str, model: str, voice: str) -> bytes:
        return bytes(640 * 3)

    async def fake_send_json(websocket, device_id: str, message_type: str, payload: dict) -> None:
        messages.append(message_type)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(orchestrator, "synthesize_pcm", fake_synthesize_pcm)
    monkeypatch.setattr(orchestrator, "_send_json", fake_send_json)
    monkeypatch.setattr(orchestrator, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", fake_sleep)
    application = SimpleNamespace(state=SimpleNamespace(
        audio_echo_until={},
        settings=SimpleNamespace(tts_provider="openai", tts_model="gpt-4o-mini-tts", tts_voice="alloy"),
    ))

    asyncio.run(orchestrator._speak(application, FakeWebSocket(), "device", "Hello"))

    assert len(chunks) == 3
    # Three 20 ms chunks fit inside the 0.5 s send-ahead lead, so no sleeping is required.
    assert delays == [0.0, 0.0, 0.0]
    assert messages == ["assistant.response", "tts.start", "tts.chunk", "tts.chunk", "tts.chunk", "tts.end"]


def test_tts_stream_never_runs_further_ahead_than_the_lead(monkeypatch) -> None:
    delays: list[float] = []

    class FakeWebSocket:
        async def send_bytes(self, data: bytes) -> None:
            return None

    async def fake_synthesize_pcm(text: str, model: str, voice: str) -> bytes:
        return bytes(32_000 * 2)

    async def fake_send_json(*_args, **_kwargs) -> None:
        return None

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(orchestrator, "synthesize_pcm", fake_synthesize_pcm)
    monkeypatch.setattr(orchestrator, "_send_json", fake_send_json)
    monkeypatch.setattr(orchestrator, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", fake_sleep)
    application = SimpleNamespace(state=SimpleNamespace(
        audio_echo_until={},
        settings=SimpleNamespace(tts_provider="openai", tts_model="gpt-4o-mini-tts", tts_voice="alloy"),
    ))

    asyncio.run(orchestrator._speak(application, FakeWebSocket(), "device", "Hello"))

    # The first 0.5 s (25 chunks) is sent immediately; afterwards the sender waits for playback to catch up.
    assert delays[:24] == [0.0] * 24
    assert max(delays) > 0


def test_tts_provider_failure_uses_local_speech_fallback(monkeypatch) -> None:
    messages: list[str] = []
    chunks: list[bytes] = []

    class FakeWebSocket:
        async def send_bytes(self, data: bytes) -> None:
            chunks.append(data)

    async def cloud_failure(*_args) -> bytes:
        raise TTSServiceError("provider rate limited")

    async def local_speech(text: str, language: str) -> bytes:
        assert text == "Hello."
        assert language == "en"
        return bytes(640)

    async def send_json(_websocket, _device_id: str, message_type: str, _payload: dict) -> None:
        messages.append(message_type)

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(orchestrator, "synthesize_pcm", cloud_failure)
    monkeypatch.setattr(orchestrator, "synthesize_local_pcm", local_speech)
    monkeypatch.setattr(orchestrator, "_send_json", send_json)
    monkeypatch.setattr(orchestrator, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", no_wait)
    application = SimpleNamespace(
        state=SimpleNamespace(
            audio_echo_until={},
            settings=SimpleNamespace(
                tts_provider="openai",
                tts_model="gpt-4o-mini-tts",
                tts_voice="alloy",
                stt_language="en",
            ),
        ),
    )

    asyncio.run(orchestrator._speak(application, FakeWebSocket(), "device", "Hello"))

    assert "tts.start" in messages
    assert "tts.end" in messages
    assert chunks == [bytes(640)]


def test_local_tts_configuration_skips_cloud_provider(monkeypatch) -> None:
    messages: list[str] = []
    chunks: list[bytes] = []

    class FakeWebSocket:
        async def send_bytes(self, data: bytes) -> None:
            chunks.append(data)

    async def cloud_must_not_run(*_args) -> bytes:
        raise AssertionError("configured local TTS called the cloud provider")

    async def local_speech(text: str, language: str) -> bytes:
        assert text == "Hello."
        assert language == "bn"
        return bytes(640)

    async def send_json(_websocket, _device_id: str, message_type: str, _payload: dict) -> None:
        messages.append(message_type)

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(orchestrator, "synthesize_pcm", cloud_must_not_run)
    monkeypatch.setattr(orchestrator, "synthesize_local_pcm", local_speech)
    monkeypatch.setattr(orchestrator, "_send_json", send_json)
    monkeypatch.setattr(orchestrator, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", no_wait)
    application = SimpleNamespace(
        state=SimpleNamespace(
            audio_echo_until={},
            settings=SimpleNamespace(tts_provider="local", stt_language="bn"),
        ),
    )

    asyncio.run(orchestrator._speak(application, FakeWebSocket(), "device", "Hello"))

    assert "tts.start" in messages
    assert "tts.end" in messages
    assert chunks == [bytes(640)]


def test_tts_environment_configuration_is_loaded_and_validated(monkeypatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("TTS_PROVIDER", "local")
    monkeypatch.setenv("TTS_MODEL", "custom-model")
    monkeypatch.setenv("TTS_VOICE", "nova")
    settings = Settings.from_env()
    assert (settings.tts_provider, settings.tts_model, settings.tts_voice) == ("local", "custom-model", "nova")

    monkeypatch.setenv("TTS_PROVIDER", "unsupported")
    with pytest.raises(ValueError, match="TTS_PROVIDER"):
        Settings.from_env()


def test_pcm_resampling_produces_16khz_sample_count() -> None:
    source = struct.pack("<2400h", *([1000] * 2400))
    result = resample_24k_to_16k(source)
    assert len(result) == 1600 * 2
    assert struct.unpack_from("<h", result)[0] == 1000


def test_local_tts_normalizes_espeak_wav_to_firmware_pcm(monkeypatch) -> None:
    monkeypatch.setattr(local_tts, "piper_model_path", lambda: None)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(22_050)
        stream.writeframes(struct.pack("<2205h", *([1000] * 2205)))

    monkeypatch.setattr(local_tts.shutil, "which", lambda _name: "/usr/bin/espeak-ng")

    def run_espeak(arguments, **_kwargs):
        assert arguments == ["/usr/bin/espeak-ng", "--stdout", "-v", "en-us+f3", "-p", "60", "-s", "160", "Hello"]
        return local_tts.subprocess.CompletedProcess(arguments, 0, audio.getvalue(), b"")

    monkeypatch.setattr(local_tts.subprocess, "run", run_espeak)
    result = asyncio.run(local_tts.synthesize_local_pcm("Hello", "en"))

    assert len(result) == 1600 * 2
    assert struct.unpack_from("<h", result)[0] == 1000


def test_local_tts_reports_missing_engine(monkeypatch) -> None:
    monkeypatch.setattr(local_tts, "piper_model_path", lambda: None)
    monkeypatch.setattr(local_tts.shutil, "which", lambda _name: None)
    with pytest.raises(TTSServiceError, match="eSpeak NG is not installed"):
        asyncio.run(local_tts.synthesize_local_pcm("Hello", "en"))


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


def test_gemini_tts_requires_key_and_supported_voice(monkeypatch) -> None:
    from app.services.tts.gemini import normalize_voice, synthesize_gemini_pcm
    from app.services.tts.openai_api import TTSServiceError

    assert normalize_voice("kore") == "Kore"
    assert normalize_voice("alloy") is None
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(TTSServiceError):
        asyncio.run(synthesize_gemini_pcm("hi", "gemini-2.5-flash-preview-tts", "Kore"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(ValueError):
        asyncio.run(synthesize_gemini_pcm("hi", "gemini-2.5-flash-preview-tts", "alloy"))


def test_local_tts_prefers_piper_when_voice_model_exists(monkeypatch, tmp_path) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"model")
    monkeypatch.setattr(local_tts, "piper_model_path", lambda: model)
    monkeypatch.setattr(local_tts, "_synthesize_piper", lambda text, path: struct.pack("<4h", 1, 2, 3, 4))
    result = asyncio.run(local_tts.synthesize_local_pcm("Hello", "en"))
    assert result == struct.pack("<4h", 1, 2, 3, 4)
