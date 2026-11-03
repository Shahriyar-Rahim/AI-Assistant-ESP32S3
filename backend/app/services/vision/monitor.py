"""Room security monitor: camera source, face presence state machine, Telegram alerts and away recording."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from app.services.vision import engine, store
from app.services.vision.telegram import TelegramClient

logger = logging.getLogger(__name__)

RECORD_INTERVAL = 0.2
RECORD_FPS = 5.0
REMOTE_FRAME_MAX_AGE = 6.0
STRANGER_CONFIRM_TICKS = 2
ALERT_COOLDOWN = 120.0
STARTUP_GRACE = 20.0  # the owner is not known to be absent until the camera has watched for a while
STRANGER_LOG_COOLDOWN = 600.0
INTRUDER_RECORD_SECONDS = 45.0
GREETING_COOLDOWN = 300.0
LEARN_COOLDOWN = 30.0
LEARN_SCORE_CEILING = 0.8


class VisionError(RuntimeError):
    pass


class VisionMonitor:
    def __init__(self, application) -> None:
        self.application = application
        self.settings = application.state.settings
        self.engine = engine.FaceEngine(low_memory=self.settings.camera_low_memory)
        self.gallery = engine.FaceGallery()
        self.telegram = TelegramClient(self.settings.telegram_bot_token, self.settings.telegram_chat_id)
        self.root = Path(self.settings.vision_dir).expanduser().resolve()
        self.user_id: str | None = None
        self.enabled = False
        self.people: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._analysis: asyncio.Task | None = None
        self._cap = None
        self._cap_lock = threading.Lock()
        self._remote: tuple[np.ndarray, float] | None = None
        self._started = time.monotonic()
        self.owner_present = False
        self.last_owner: float | None = None
        self.left_at: float | None = None
        self.last_greeting = 0.0
        self.last_frame_at: float | None = None
        self._streak: dict[str, int] = {}
        self._alerted: dict[str, float] = {}
        self._stranger_logged: dict[str, float] = {}
        self._touched: dict[str, float] = {}
        self._learned: dict[str, float] = {}
        self._intruder_until = 0.0
        self._manual_until: float | None = 0.0
        self._writer = None
        self._writer_path: Path | None = None
        self._writer_reason = ""
        self._writer_started = 0.0
        self.recordings_saved = 0

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.user_id = await asyncio.to_thread(store.admin_user_id, self.application.state.db_factory)
        await self.reload()
        for folder in ("photos", "alerts", "recordings"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        if self.settings.camera_enabled:
            await self.set_enabled(True)

    async def reload(self) -> None:
        factory = self.application.state.db_factory
        self.people = await asyncio.to_thread(store.identities, factory)
        pairs = await asyncio.to_thread(store.load_embeddings, factory)
        await asyncio.to_thread(self.gallery.load, pairs)

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled and (self._task is None or self._task.done()):
            self._started = time.monotonic()
            self._task = asyncio.create_task(self._loop(), name="vision-monitor")
        elif not enabled:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            self._close_writer()
            self._release_camera()
        await self._log("monitor_on" if enabled else "monitor_off", "Camera security " + ("enabled" if enabled else "disabled"))

    async def stop(self) -> None:
        await self.set_enabled(False)

    def uid(self, fallback: str | None = None) -> str | None:
        return self.user_id or fallback

    # ---------- camera ----------
    def push_remote(self, frame: np.ndarray) -> None:
        self._remote = (frame, time.monotonic())

    def remote_fresh(self) -> bool:
        return self._remote is not None and time.monotonic() - self._remote[1] < REMOTE_FRAME_MAX_AGE

    def _read_local(self) -> np.ndarray | None:
        with self._cap_lock:
            if self._cap is None:
                capture = cv2.VideoCapture(self.settings.camera_index)
                if not capture.isOpened():
                    capture.release()
                    return None
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self._cap = capture
            ok, frame = self._cap.read()
            if not ok:
                self._cap.release()
                self._cap = None
                return None
            return frame

    def _release_camera(self) -> None:
        with self._cap_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None

    async def grab(self) -> np.ndarray | None:
        source = self.settings.camera_source
        if source in {"device", "auto"} and self.remote_fresh():
            return self._remote[0]
        if source in {"local", "auto"}:
            return await asyncio.to_thread(self._read_local)
        return None

    async def capture(self, settle: bool = False) -> np.ndarray:
        if settle and self.settings.camera_source != "device":
            for _ in range(4):
                await grab_discard(self)
        frame = await self.grab()
        if frame is None:
            raise VisionError("I can't see through the camera. Check that it is plugged in and not used by another app.")
        self.last_frame_at = time.monotonic()
        if not self.enabled and not self._writer:
            self._release_camera()
        return frame

    # ---------- events and alerts ----------
    async def _log(self, kind: str, detail: str = "", identity_id: str | None = None,
                   media_path: str | None = None, alerted: bool = False) -> None:
        await asyncio.to_thread(store.log_event, self.application.state.db_factory, self.user_id, kind, detail,
                                identity_id, media_path, alerted)
        logger.info("Vision event %s: %s", kind, detail)

    async def announce(self, text: str) -> None:
        from app.services.assistant import orchestrator

        for device_id, websocket in list(self.application.state.device_sockets.items()):
            try:
                await orchestrator._speak(self.application, websocket, device_id, text)
            except Exception:
                logger.exception("Could not announce on %s", device_id)

    def _save_image(self, folder: str, frame: np.ndarray, label: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.root / folder / f"{stamp}_{label}.jpg"
        path.write_bytes(engine.encode_jpeg(frame))
        return path

    async def take_photo(self, label: str = "photo", send: bool = False) -> Path:
        frame = await self.capture(settle=True)
        path = await asyncio.to_thread(self._save_image, "photos", frame, label)
        await self._log("photo", f"Saved {label}", media_path=str(path))
        if send:
            await self.telegram.send_photo(path, "Photo from your room")
        return path

    # ---------- recording ----------
    def start_recording(self, seconds: float | None) -> None:
        self._manual_until = float("inf") if seconds is None else time.monotonic() + seconds
        if not self.enabled:
            self.application.state.vision_task = asyncio.create_task(self.set_enabled(True))

    def stop_recording(self) -> bool:
        active = self._writer is not None
        self._manual_until = 0.0
        self._intruder_until = 0.0
        return active

    @property
    def recording(self) -> bool:
        return self._writer is not None

    def _desired_reason(self, now: float) -> str:
        if now < self._intruder_until:
            return "intruder"
        if self._manual_until is None or now < (self._manual_until or 0.0):
            return "manual"
        owner_known = any(person["role"] == "owner" for person in self.people.values())
        away_for = now - (self.left_at if self.left_at is not None else self._started)
        if owner_known and not self.owner_present and away_for >= self.settings.camera_away_record_after_seconds:
            return "away"
        return ""

    def _close_writer(self) -> None:
        if self._writer is None:
            return
        self._writer.release()
        path, reason, started = self._writer_path, self._writer_reason, self._writer_started
        self._writer = None
        self._writer_path = None
        if path is not None and time.monotonic() - started >= 3 and path.exists():
            self.recordings_saved += 1
            asyncio.get_running_loop().create_task(self._finish_recording(path, reason))
        elif path is not None and path.exists():
            path.unlink()

    async def _finish_recording(self, path: Path, reason: str) -> None:
        await self._log("recording_saved", f"{reason} recording saved", media_path=str(path))
        if reason == "intruder":
            await self.telegram.send_video(path, "Clip of the person in your room")

    def _write(self, frame: np.ndarray, reason: str, now: float) -> None:
        rotate = self._writer is not None and (
            reason != self._writer_reason or now - self._writer_started >= self.settings.camera_segment_seconds
        )
        if rotate:
            self._close_writer()
        if self._writer is None:
            height, width = frame.shape[:2]
            day = self.root / "recordings" / datetime.now().strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            path = day / f"{datetime.now().strftime('%H%M%S')}_{reason}.mp4"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), RECORD_FPS, (width, height))
            if not writer.isOpened():
                logger.error("Could not open video writer for %s", path)
                self._manual_until = 0.0
                return
            self._writer, self._writer_path, self._writer_reason, self._writer_started = writer, path, reason, now
        self._writer.write(frame)

    # ---------- main loop ----------
    async def _loop(self) -> None:
        last_analysis = 0.0
        missed = 0
        while self.enabled:
            now = time.monotonic()
            try:
                frame = await self.grab()
                if frame is None:
                    missed += 1
                    if missed in {1, 30}:
                        await self._log("camera_unavailable", "No frames from the camera")
                    await asyncio.sleep(2.0)
                    continue
                missed = 0
                self.last_frame_at = now
                reason = self._desired_reason(now)
                if reason:
                    self._write(frame, reason, now)
                elif self._writer is not None:
                    self._close_writer()
                if now - last_analysis >= self.settings.camera_interval_seconds and (self._analysis is None or self._analysis.done()):
                    last_analysis = now
                    self._analysis = asyncio.create_task(self._analyse(frame.copy()))
                await asyncio.sleep(RECORD_INTERVAL if self._writer else self.settings.camera_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Vision loop error")
                await asyncio.sleep(2.0)

    async def _analyse(self, frame: np.ndarray) -> None:
        try:
            faces = await asyncio.to_thread(self.engine.faces, frame)
            matches = [(vector, *self.gallery.match(vector)) for _, vector in faces]
            await self._process(frame, matches)
        except Exception:
            logger.exception("Vision analysis error")

    async def _process(self, frame: np.ndarray, matches: list[tuple[np.ndarray, str | None, float]]) -> None:
        factory = self.application.state.db_factory
        now = time.monotonic()
        seen: dict[str, dict] = {}
        for vector, identity_id, score in matches:
            if identity_id is None:
                name = await asyncio.to_thread(store.next_stranger_name, factory)
                identity_id = await asyncio.to_thread(store.create_identity, factory, self.user_id, name, "stranger", vector)
                self.gallery.add(identity_id, vector)
                self.people[identity_id] = {"id": identity_id, "name": name, "role": "stranger"}
                await self._log("new_face", f"{name} seen for the first time", identity_id)
            else:
                if now - self._touched.get(identity_id, 0.0) > 10:
                    self._touched[identity_id] = now
                    await asyncio.to_thread(store.touch, factory, identity_id)
                if score < LEARN_SCORE_CEILING and now - self._learned.get(identity_id, 0.0) > LEARN_COOLDOWN:
                    self._learned[identity_id] = now
                    self.gallery.add(identity_id, vector)
                    await asyncio.to_thread(store.add_embedding, factory, identity_id, vector)
            seen[identity_id] = self.people.get(identity_id, {"name": "Someone", "role": "stranger"})

        owners = [item for item in seen.values() if item["role"] == "owner"]
        if owners:
            self.last_owner = now
            if not self.owner_present:
                await self._owner_arrived(owners[0]["name"], now)
        elif self.owner_present and self.last_owner is not None and now - self.last_owner > self.settings.camera_owner_grace_seconds:
            self.owner_present = False
            self.left_at = now
            await self._log("owner_left", "The owner left the room")

        others = {identity_id: item for identity_id, item in seen.items() if item["role"] != "owner"}
        for identity_id in list(self._streak):
            if identity_id not in others:
                self._streak.pop(identity_id)
        for identity_id, person in others.items():
            if self.owner_present:
                if now - self._stranger_logged.get(identity_id, 0.0) > STRANGER_LOG_COOLDOWN:
                    self._stranger_logged[identity_id] = now
                    await self._log("visitor_with_owner", f"{person['name']} is here with the owner; face remembered", identity_id)
                continue
            if not any(item["role"] == "owner" for item in self.people.values()):
                if now - self._stranger_logged.get(identity_id, 0.0) > STRANGER_LOG_COOLDOWN:
                    self._stranger_logged[identity_id] = now
                    await self._log("unidentified_person", f"{person['name']} seen; no owner is enrolled yet, so no alert was sent (say \"remember my face\")", identity_id)
                continue
            if self._started and now - self._started < STARTUP_GRACE and self.last_owner is None:
                continue
            self._streak[identity_id] = self._streak.get(identity_id, 0) + 1
            self._intruder_until = max(self._intruder_until, now + INTRUDER_RECORD_SECONDS) if self._streak[identity_id] >= STRANGER_CONFIRM_TICKS else self._intruder_until
            if self._streak[identity_id] >= STRANGER_CONFIRM_TICKS and now - self._alerted.get(identity_id, 0.0) > ALERT_COOLDOWN:
                self._alerted[identity_id] = now
                await self._alert(frame, identity_id, person)

    async def _owner_arrived(self, name: str, now: float) -> None:
        self.owner_present = True
        away_since = self.left_at
        self.left_at = None
        await self._log("owner_arrived", f"{name} is in the room")
        if now - self.last_greeting < GREETING_COOLDOWN:
            return
        self.last_greeting = now
        text = f"Welcome back, {name}."
        if away_since is not None:
            alerts = await asyncio.to_thread(
                store.recent_events, self.application.state.db_factory, 100,
                timedelta(seconds=now - away_since + 5))
            visitors = sum(1 for event in alerts if event["kind"] == "intruder_alert")
            if visitors:
                text += f" While you were away, I noticed {visitors} visitor{'s' if visitors != 1 else ''}. I sent the details to Telegram."
        await self.announce(text)

    async def _alert(self, frame: np.ndarray, identity_id: str, person: dict) -> None:
        path = await asyncio.to_thread(self._save_image, "alerts", frame, person["name"].replace(" ", "_"))
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = f"Alert: {person['name']} is in your room and you are not there. {when}"
        sent = await self.telegram.send_photo(path, caption) if self.telegram.configured else False
        detail = caption + ("" if sent else " (Telegram not sent: " + ("failed" if self.telegram.configured else "not configured") + ")")
        await self._log("intruder_alert", detail, identity_id, str(path), alerted=sent)

    # ---------- on-request features ----------
    async def enroll(self, name: str, role: str, samples: int = 5) -> str:
        vectors: list[np.ndarray] = []
        deadline = time.monotonic() + 12
        while len(vectors) < samples and time.monotonic() < deadline:
            frame = await self.capture(settle=not vectors)
            faces = await asyncio.to_thread(self.engine.faces, frame)
            if faces:
                vectors.append(faces[0][1])
            await asyncio.sleep(0.5)
        if not vectors:
            raise VisionError("I couldn't see a face. Look at the camera and try again.")
        factory = self.application.state.db_factory
        existing = await asyncio.to_thread(store.find_identity, factory, name)
        if existing is not None:
            identity_id = existing.id
            await asyncio.to_thread(store.set_identity, factory, identity_id, None, role)
            for vector in vectors:
                await asyncio.to_thread(store.add_embedding, factory, identity_id, vector)
        else:
            identity_id = await asyncio.to_thread(store.create_identity, factory, self.user_id, name, role, vectors[0])
            for vector in vectors[1:]:
                await asyncio.to_thread(store.add_embedding, factory, identity_id, vector)
        # Merge any auto-created stranger identity that is really this person.
        for vector in vectors:
            other, _ = self.gallery.match(vector)
            if other and other != identity_id and self.people.get(other, {}).get("role") == "stranger":
                await asyncio.to_thread(store.delete_identity, factory, other)
        await self.reload()
        await self._log("face_enrolled", f"{name} remembered as {role} ({len(vectors)} samples)", identity_id)
        if role == "owner":
            self.owner_present = True
            self.last_owner = time.monotonic()
        return identity_id

    async def who_is_here(self) -> list[str]:
        frame = await self.capture(settle=True)
        faces = await asyncio.to_thread(self.engine.faces, frame)
        names = []
        for _, vector in faces:
            identity_id, _ = self.gallery.match(vector)
            names.append(self.people.get(identity_id, {}).get("name", "someone I don't know") if identity_id else "someone I don't know")
        return names

    async def remember_object(self, label: str) -> str:
        frame = await self.capture(settle=True)
        try:
            descriptor = await asyncio.to_thread(engine.describe_object, frame)
        except ValueError as error:
            raise VisionError(str(error)) from error
        path = await asyncio.to_thread(self._save_image, "photos", frame, f"object_{label.replace(' ', '_')}")
        memory_id = await asyncio.to_thread(
            store.add_memory, self.application.state.db_factory, self.user_id, "object", label, "", descriptor,
            {"reference_photo": str(path)})
        await self._log("object_remembered", f"Remembered object: {label}", media_path=str(path))
        return memory_id

    async def identify_object(self) -> tuple[str | None, int]:
        frame = await self.capture(settle=True)
        stored = await asyncio.to_thread(store.object_descriptors, self.application.state.db_factory, self.user_id)
        if not stored:
            return None, 0
        labels = {memory_id: label for memory_id, label, _ in stored}
        found, count = await asyncio.to_thread(engine.match_object, frame, [(m, d) for m, _, d in stored])
        return (labels[found] if found else None), count

    async def read_paper(self, label: str = "") -> str:
        frame = await self.capture(settle=True)
        text = await asyncio.to_thread(engine.read_text, frame)
        if not text:
            raise VisionError("I couldn't read any writing. Hold the paper flat, closer to the camera, in good light.")
        title = label or text.splitlines()[0][:60]
        path = await asyncio.to_thread(self._save_image, "photos", frame, "paper")
        await asyncio.to_thread(store.add_memory, self.application.state.db_factory, self.user_id, "text", title, text,
                                None, {"photo": str(path)})
        await self._log("text_remembered", f"Read {len(text)} characters: {title}", media_path=str(path))
        return text

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "source": self.settings.camera_source,
            "camera_index": self.settings.camera_index,
            "remote_frames": self.remote_fresh(),
            "camera_active": self._cap is not None or self.remote_fresh(),
            "last_frame_age_seconds": round(time.monotonic() - self.last_frame_at, 1) if self.last_frame_at else None,
            "owner_enrolled": any(person["role"] == "owner" for person in self.people.values()),
            "owner_present": self.owner_present,
            "recording": self._writer_reason if self._writer else None,
            "recordings_saved": self.recordings_saved,
            "low_memory_mode": self.settings.camera_low_memory,
            "recognizer_loaded": self.engine.recognizer_loaded,
            "faces_remembered": len(self.people),
            "embeddings": self.gallery.count(),
            "telegram_configured": self.telegram.configured,
            "storage_dir": str(self.root),
        }


async def grab_discard(monitor: VisionMonitor) -> None:
    await monitor.grab()
    await asyncio.sleep(0.05)
