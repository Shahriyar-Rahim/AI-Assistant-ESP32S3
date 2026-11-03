import asyncio
import time
from types import SimpleNamespace

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.database.session import Base
from app.services.vision import commands, engine, store
from app.services.vision.monitor import VisionMonitor


class FakeTelegram:
    configured = True

    def __init__(self):
        self.photos, self.videos, self.messages = [], [], []

    async def send_photo(self, path, caption=""):
        self.photos.append(caption)
        return True

    async def send_video(self, path, caption=""):
        self.videos.append(caption)
        return True

    async def send_message(self, text):
        self.messages.append(text)
        return True


def vec(seed: int) -> np.ndarray:
    value = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return value / np.linalg.norm(value)


def make_monitor(tmp_path):
    db_engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(db_engine, expire_on_commit=False)
    settings = SimpleNamespace(
        telegram_bot_token="t", telegram_chat_id="c", vision_dir=str(tmp_path), camera_source="local", camera_index=0,
        camera_interval_seconds=0.7, camera_owner_grace_seconds=45.0, camera_away_record_after_seconds=60.0,
        camera_segment_seconds=300, camera_enabled=False, camera_low_memory=True)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db_factory=factory, device_sockets={}))
    monitor = VisionMonitor(app)
    monitor.telegram = FakeTelegram()
    monitor.spoken = []

    async def announce(text):
        monitor.spoken.append(text)

    monitor.announce = announce
    return monitor, factory


FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


async def see(monitor, *vectors):
    await monitor._process(FRAME, [(v, *monitor.gallery.match(v)) for v in vectors])


def kinds(factory):
    return [event["kind"] for event in store.recent_events(factory, 100)]


def test_gallery_matches_and_is_unbounded():
    gallery = engine.FaceGallery()
    for seed in range(300):
        gallery.add(f"p{seed}", vec(seed))
    assert gallery.match(vec(7))[0] == "p7"
    assert gallery.match(vec(9999))[0] is None
    gallery.remove("p7")
    assert gallery.match(vec(7))[0] is None and gallery.count() == 299


def test_stranger_with_owner_is_remembered_silently(tmp_path):
    monitor, factory = make_monitor(tmp_path)

    async def scenario():
        owner = store.create_identity(factory, None, "Sam", "owner", vec(1))
        await monitor.reload()
        await see(monitor, vec(1))
        assert monitor.owner_present and "Welcome back, Sam." in monitor.spoken[0]
        await see(monitor, vec(1), vec(2))
        await see(monitor, vec(1), vec(2))

    asyncio.run(scenario())
    assert monitor.telegram.photos == []
    assert "new_face" in kinds(factory) and "visitor_with_owner" in kinds(factory)
    assert len([p for p in store.identities(factory).values() if p["role"] == "stranger"]) == 1


def test_stranger_alone_triggers_one_telegram_alert_and_recording(tmp_path):
    monitor, factory = make_monitor(tmp_path)
    for folder in ("alerts", "photos", "recordings"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    async def scenario():
        owner = store.create_identity(factory, None, "Sam", "owner", vec(1))
        await monitor.reload()
        monitor.owner_present = False
        monitor.last_owner = time.monotonic() - 999
        for _ in range(4):
            await see(monitor, vec(5))
        return owner

    asyncio.run(scenario())
    assert len(monitor.telegram.photos) == 1 and "you are not there" in monitor.telegram.photos[0]
    assert "intruder_alert" in kinds(factory)
    assert monitor._desired_reason(time.monotonic()) == "intruder"
    assert list((tmp_path / "alerts").glob("*.jpg"))


def test_away_recording_only_after_owner_known(tmp_path):
    monitor, factory = make_monitor(tmp_path)
    monitor._started = time.monotonic() - 500
    assert monitor._desired_reason(time.monotonic()) == ""
    store.create_identity(factory, None, "Sam", "owner", vec(1))
    asyncio.run(monitor.reload())
    assert monitor._desired_reason(time.monotonic()) == "away"
    monitor.owner_present = True
    assert monitor._desired_reason(time.monotonic()) == ""


def test_home_memory_and_where_commands(tmp_path):
    monitor, factory = make_monitor(tmp_path)
    app = SimpleNamespace(state=SimpleNamespace(vision=monitor, db_factory=factory))
    monitor.user_id = None

    async def say(text):
        return await commands.route(app, "dev", None, text)

    assert "passport is in the top drawer" in asyncio.run(say("Remember my passport is in the top drawer."))
    assert "top drawer" in asyncio.run(say("Where is my passport?"))
    assert asyncio.run(say("Where is Bangladesh")) is None
    assert asyncio.run(say("what is the capital of France")) is None


def test_object_memory_matches_same_picture_not_another():
    rng = np.random.default_rng(3)
    base = (rng.random((480, 640)) * 255).astype(np.uint8)
    picture = np.dstack([base] * 3)
    other = np.dstack([(np.random.default_rng(4).random((480, 640)) * 255).astype(np.uint8)] * 3)
    descriptor = engine.describe_object(picture)
    assert engine.match_object(picture, [("a", descriptor)])[0] == "a"
    assert engine.match_object(other, [("a", descriptor)])[0] is None


def test_ocr_reads_written_text():
    import cv2

    image = np.full((200, 700, 3), 255, np.uint8)
    cv2.putText(image, "Buy milk at 5 pm", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3)
    assert "milk" in engine.read_text(image).lower()


def test_recognizer_released_when_no_face_in_view():
    import numpy as np
    from app.services.vision.engine import FaceEngine

    face_engine = FaceEngine(idle_release_seconds=0.0)
    blank = np.zeros((240, 320, 3), dtype=np.uint8)
    assert face_engine.faces(blank) == []
    assert not face_engine.recognizer_loaded
    face_engine._load_recognizer()
    assert face_engine.recognizer_loaded
    face_engine.faces(blank)
    assert not face_engine.recognizer_loaded
