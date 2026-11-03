from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.api.calendar import _utc_iso
from app.database.session import Base, create_database
from app.models import CalendarEvent, Notification, Reminder
from app.services.automation.health_checks import validate_endpoint_url
from app.services.integrations.vault import decrypt_credentials, encrypt_credentials
from app.workers.calendar import process_due_notifications


def test_integration_credentials_are_encrypted_and_require_key() -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        database_url="sqlite://",
        device_token="",
        simulator_token="",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        integration_encryption_key=key,
    )
    plaintext = {"access_token": "private-token", "refresh_token": "refresh-token"}
    ciphertext = encrypt_credentials(plaintext, settings)
    assert "private-token" not in ciphertext
    assert decrypt_credentials(ciphertext, settings) == plaintext


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://user:pass@example.com/health",
    "http://example.com/health?token=secret",
    "https://example.com/health#fragment",
])
def test_health_monitor_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_endpoint_url(url)


def test_health_monitor_accepts_public_http_endpoint_format() -> None:
    assert validate_endpoint_url("https://example.org/health") == "https://example.org/health"


def test_database_datetime_serialization_preserves_utc_offset() -> None:
    stored = datetime(2025, 1, 1, 12, 0)
    assert _utc_iso(stored) == "2025-01-01T12:00:00+00:00"
    offset = datetime(2025, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _utc_iso(offset) == "2025-01-01T12:00:00+00:00"


def test_due_calendar_items_create_notifications_without_audio_storage(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'scheduler.db'}",
        device_token="",
        simulator_token="",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
    )
    engine, session_factory = create_database(settings)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        db.add(Reminder(title="Water plants", due_at=now - timedelta(seconds=1), timezone_name="UTC"))
        db.add(CalendarEvent(title="Stand-up", kind="meeting", start_at=now - timedelta(seconds=1),
                             timezone_name="UTC", description=""))
        db.add(CalendarEvent(title="Wake-up", kind="alarm", start_at=now - timedelta(seconds=1),
                             timezone_name="UTC", description=""))
        db.commit()
    application = SimpleNamespace(state=SimpleNamespace(db_factory=session_factory, settings=settings))
    pending = process_due_notifications(application)
    assert len(pending) == 3
    with session_factory() as db:
        assert db.query(Notification).count() == 3
        assert {row.title for row in db.query(Notification).all()} == {"Water plants", "Stand-up", "Wake-up"}
        alarm = next(row for row in db.query(Notification).all() if row.title == "Wake-up")
        assert alarm.payload == {"kind": "alarm", "timezone_name": "UTC"}
    engine.dispose()
