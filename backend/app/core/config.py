from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


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
    anti_spoof_model_path: str = ""
    anti_spoof_threshold: float = 0.8
    redis_url: str = ""
    rate_limit_per_minute: int = 120
    allowed_hosts: tuple[str, ...] = ("*",)
    firmware_storage_dir: str = "./firmware-releases"
    personalization_audio_retention_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        max_frame = int(os.getenv("MAX_AUDIO_FRAME_BYTES", "4096"))
        threshold = int(os.getenv("VAD_RMS_THRESHOLD", "450"))
        end_silence = int(os.getenv("VAD_END_SILENCE_MS", "900"))
        access_token_minutes = int(os.getenv("ACCESS_TOKEN_MINUTES", "60"))
        biometric_threshold = float(os.getenv("BIOMETRIC_THRESHOLD", "0.75"))
        wake_threshold = float(os.getenv("WAKE_WORD_THRESHOLD", "0.5"))
        retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
        stt_provider = os.getenv("STT_PROVIDER", "openai").strip().lower()
        stt_language = os.getenv("STT_LANGUAGE", "en").strip().lower()
        anti_spoof_threshold = float(os.getenv("ANTI_SPOOF_THRESHOLD", "0.8"))
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
        return cls(
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
            anti_spoof_model_path=os.getenv("ANTI_SPOOF_MODEL_PATH", ""),
            anti_spoof_threshold=anti_spoof_threshold,
            redis_url=os.getenv("REDIS_URL", ""),
            rate_limit_per_minute=rate_limit,
            allowed_hosts=tuple(
                item.strip() for item in os.getenv("ALLOWED_HOSTS", "*").split(",") if item.strip()
            ),
            firmware_storage_dir=os.getenv("FIRMWARE_STORAGE_DIR", "./firmware-releases"),
            personalization_audio_retention_days=personalization_retention,
        )
