from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from app.services.tts.gemini import normalize_voice


@dataclass(frozen=True)
class Settings:
    database_url: str
    device_token: str
    simulator_token: str
    allowed_origins: tuple[str, ...]
    max_audio_frame_bytes: int
    vad_rms_threshold: int
    vad_end_silence_ms: int
    auth_signing_secret: str = ""
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    access_token_minutes: int = 60
    app_env: str = "development"
    default_ai_provider: str = ""
    ai_model: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    voice_encryption_key: str = ""
    biometric_threshold: float = 0.75
    wake_word: str = "hey buddy"
    wake_word_model_path: str = ""
    wake_word_threshold: float = 0.5
    audit_retention_days: int = 365
    integration_encryption_key: str = ""
    github_token: str = ""
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = ""
    stt_provider: str = "openai"
    stt_language: str = "en"
    tts_provider: str = "openai"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    anti_spoof_model_path: str = ""
    anti_spoof_threshold: float = 0.8
    biometric_mode: str = "enforce"
    redis_url: str = ""
    rate_limit_per_minute: int = 120
    allowed_hosts: tuple[str, ...] = ("*",)
    firmware_storage_dir: str = "./firmware-releases"
    personalization_audio_retention_days: int = 30
    boot_briefing: bool = True
    wake_weather: bool = True
    local_brain_first: bool = True
    ai_fallback_model: str = "gemini-3.1-flash-lite"
    ollama_fallback_model: str = ""
    private_memory_model: str = ""
    private_memory_ollama_url: str = "http://127.0.0.1:11434"
    mem0_enabled: bool = False
    mem0_embedding_model: str = "all-minilm"
    mem0_embedding_dimensions: int = 384
    news_default_country: str = "US"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    camera_enabled: bool = False
    camera_source: str = "local"  # local (USB camera on this PC) | device (frames pushed by the ESP32) | auto
    camera_index: int = 0
    camera_interval_seconds: float = 0.7
    camera_owner_grace_seconds: float = 45.0
    camera_away_record_after_seconds: float = 60.0
    camera_segment_seconds: int = 300
    camera_low_memory: bool = True
    gesture_enabled: bool = False
    gesture_model_path: str = ""
    vision_dir: str = "data/vision"

    @classmethod
    def from_env(cls) -> "Settings":
        # .env wins over stale shell/IDE-exported values; tests set the environment explicitly.
        load_dotenv(override="PYTEST_CURRENT_TEST" not in os.environ)
        max_frame = int(os.getenv("MAX_AUDIO_FRAME_BYTES", "4096"))
        threshold = int(os.getenv("VAD_RMS_THRESHOLD", "450"))
        end_silence = int(os.getenv("VAD_END_SILENCE_MS", "900"))
        access_token_minutes = int(os.getenv("ACCESS_TOKEN_MINUTES", "60"))
        biometric_threshold = float(os.getenv("BIOMETRIC_THRESHOLD", "0.75"))
        wake_threshold = float(os.getenv("WAKE_WORD_THRESHOLD", "0.5"))
        retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
        stt_provider = os.getenv("STT_PROVIDER", "openai").strip().lower()
        stt_language = os.getenv("STT_LANGUAGE", "en").strip().lower()
        tts_provider = os.getenv("TTS_PROVIDER", "openai").strip().lower()
        tts_model = os.getenv("TTS_MODEL", "gpt-4o-mini-tts").strip()
        tts_voice = os.getenv("TTS_VOICE", "alloy").strip()
        anti_spoof_threshold = float(os.getenv("ANTI_SPOOF_THRESHOLD", "0.8"))
        biometric_mode = os.getenv("BIOMETRIC_MODE", "enforce").strip().lower()
        if biometric_mode not in {"enforce", "enrolled_only"}:
            raise ValueError("BIOMETRIC_MODE must be 'enforce' or 'enrolled_only'")
        rate_limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
        personalization_retention = int(os.getenv("PERSONALIZATION_AUDIO_RETENTION_DAYS", "30"))
        if max_frame < 27 or max_frame > 1_048_576:
            raise ValueError("MAX_AUDIO_FRAME_BYTES must be between 27 and 1048576")
        if not 0 <= threshold <= 32767:
            raise ValueError("VAD_RMS_THRESHOLD must be between 0 and 32767")
        if not 100 <= end_silence <= 10_000:
            raise ValueError("VAD_END_SILENCE_MS must be between 100 and 10000")
        if not 5 <= access_token_minutes <= 1440:
            raise ValueError("ACCESS_TOKEN_MINUTES must be between 5 and 1440")
        if not 0.1 <= biometric_threshold <= 0.99:
            raise ValueError("BIOMETRIC_THRESHOLD must be between 0.1 and 0.99")
        if not 0.1 <= wake_threshold <= 0.99:
            raise ValueError("WAKE_WORD_THRESHOLD must be between 0.1 and 0.99")
        if not 30 <= retention_days <= 3650:
            raise ValueError("AUDIT_RETENTION_DAYS must be between 30 and 3650")
        if stt_provider not in {"openai", "local"}:
            raise ValueError("STT_PROVIDER must be 'openai' or 'local'")
        if stt_language not in {"en", "bn"}:
            raise ValueError("STT_LANGUAGE must be 'en' or 'bn'")
        if tts_provider == "piper":
            tts_provider = "local"
        if tts_provider not in {"openai", "gemini", "local"}:
            raise ValueError("TTS_PROVIDER must be 'openai', 'gemini', 'piper' or 'local'")
        if not tts_model:
            raise ValueError("TTS_MODEL cannot be empty")
        if tts_provider == "gemini":
            if normalize_voice(tts_voice) is None:
                raise ValueError("TTS_VOICE must be a supported Gemini voice")
        elif tts_provider != "local" and tts_voice.lower() not in {
            "alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse",
        }:
            raise ValueError("TTS_VOICE must be a supported OpenAI voice")
        if tts_provider == "openai":
            tts_voice = tts_voice.lower()
        if not 0.5 <= anti_spoof_threshold <= 0.999:
            raise ValueError("ANTI_SPOOF_THRESHOLD must be between 0.5 and 0.999")
        if not 1 <= rate_limit <= 100_000:
            raise ValueError("RATE_LIMIT_PER_MINUTE must be between 1 and 100000")
        if not 1 <= personalization_retention <= 90:
            raise ValueError("PERSONALIZATION_AUDIO_RETENTION_DAYS must be between 1 and 90")
        origins = tuple(
            item.strip()
            for item in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
            if item.strip()
        )
        flag = lambda name: os.getenv(name, "true").strip().casefold() not in {"0", "false", "no", "off"}
        return cls(
            boot_briefing=flag("BOOT_BRIEFING"),
            wake_weather=flag("WAKE_WEATHER"),
            local_brain_first=flag("LOCAL_BRAIN_FIRST"),
            ai_fallback_model=os.getenv("AI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip(),
            ollama_fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL", "").strip(),
            private_memory_model=os.getenv("PRIVATE_MEMORY_MODEL", "").strip(),
            private_memory_ollama_url=os.getenv("PRIVATE_MEMORY_OLLAMA_URL", "http://127.0.0.1:11434").strip(),
            mem0_enabled=os.getenv("MEM0_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"},
            mem0_embedding_model=os.getenv("MEM0_EMBEDDING_MODEL", "all-minilm").strip(),
            mem0_embedding_dimensions=int(os.getenv("MEM0_EMBEDDING_DIMENSIONS", "384")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            camera_enabled=os.getenv("CAMERA_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"},
            camera_source=os.getenv("CAMERA_SOURCE", "local").strip().lower(),
            camera_index=int(os.getenv("CAMERA_INDEX", "0")),
            camera_interval_seconds=float(os.getenv("CAMERA_INTERVAL_SECONDS", "0.7")),
            camera_owner_grace_seconds=float(os.getenv("CAMERA_OWNER_GRACE_SECONDS", "45")),
            camera_away_record_after_seconds=float(os.getenv("CAMERA_AWAY_RECORD_AFTER_SECONDS", "60")),
            camera_segment_seconds=int(os.getenv("CAMERA_SEGMENT_SECONDS", "300")),
            camera_low_memory=os.getenv("CAMERA_LOW_MEMORY", "true").strip().casefold() in {"1", "true", "yes", "on"},
            gesture_enabled=os.getenv("GESTURE_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"},
            gesture_model_path=os.getenv("GESTURE_MODEL_PATH", "").strip(),
            vision_dir=os.getenv("VISION_DIR", "data/vision").strip(),
            news_default_country=os.getenv("NEWS_DEFAULT_COUNTRY", "US").strip().upper()[:2] or "US",
            database_url=os.getenv("DATABASE_URL", "sqlite:///./assistant.db"),
            device_token=os.getenv("DEVICE_TOKEN", ""),
            simulator_token=os.getenv("SIMULATOR_TOKEN", ""),
            allowed_origins=origins,
            max_audio_frame_bytes=max_frame,
            vad_rms_threshold=threshold,
            vad_end_silence_ms=end_silence,
            auth_signing_secret=os.getenv("AUTH_SIGNING_SECRET", ""),
            bootstrap_admin_username=os.getenv("BOOTSTRAP_ADMIN_USERNAME", ""),
            bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD", ""),
            access_token_minutes=access_token_minutes,
            app_env=os.getenv("APP_ENV", "development"),
            default_ai_provider=os.getenv("AI_PROVIDER", ""),
            ai_model=os.getenv("AI_MODEL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            voice_encryption_key=os.getenv("VOICE_ENCRYPTION_KEY", ""),
            biometric_threshold=biometric_threshold,
            wake_word=os.getenv("WAKE_WORD", "hey buddy"),
            wake_word_model_path=os.getenv("WAKE_WORD_MODEL_PATH", ""),
            wake_word_threshold=wake_threshold,
            audit_retention_days=retention_days,
            integration_encryption_key=os.getenv("INTEGRATION_ENCRYPTION_KEY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            gmail_client_id=os.getenv("GMAIL_CLIENT_ID", ""),
            gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", ""),
            gmail_redirect_uri=os.getenv("GMAIL_REDIRECT_URI", ""),
            linkedin_client_id=os.getenv("LINKEDIN_CLIENT_ID", ""),
            linkedin_client_secret=os.getenv("LINKEDIN_CLIENT_SECRET", ""),
            linkedin_redirect_uri=os.getenv("LINKEDIN_REDIRECT_URI", ""),
            stt_provider=stt_provider,
            stt_language=stt_language,
            tts_provider=tts_provider,
            tts_model=tts_model,
            tts_voice=tts_voice,
            anti_spoof_model_path=os.getenv("ANTI_SPOOF_MODEL_PATH", ""),
            anti_spoof_threshold=anti_spoof_threshold,
            biometric_mode=biometric_mode,
            redis_url=os.getenv("REDIS_URL", ""),
            rate_limit_per_minute=rate_limit,
            allowed_hosts=tuple(
                item.strip() for item in os.getenv("ALLOWED_HOSTS", "*").split(",") if item.strip()
            ),
            firmware_storage_dir=os.getenv("FIRMWARE_STORAGE_DIR", "./firmware-releases"),
            personalization_audio_retention_days=personalization_retention,
        )
