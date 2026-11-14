"""Builds the upcoming alarm/reminder schedule that the firmware stores and fires on its own clock."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from datetime import datetime as _dt
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import select

from app.models import AuditLog, CalendarEvent, Reminder

logger = logging.getLogger(__name__)

MAX_ITEMS = 32
HORIZON_DAYS = 7
MAX_PER_SOURCE = 3


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _occurrences(start: datetime, rule: str | None, zone: str, now: datetime, horizon: datetime) -> list[datetime]:
    start = _utc(start)
    if not rule:
        return [start] if now < start <= horizon else []
    tz = ZoneInfo(zone)
    try:
        found = rrulestr(rule, dtstart=start.astimezone(tz)).between(
            now.astimezone(tz), horizon.astimezone(tz), inc=False)
    except (ValueError, TypeError):
        return []
    return [item.astimezone(timezone.utc) for item in found[:MAX_PER_SOURCE]]


def _entry(prefix: str, source_id: str, title: str, kind: str, at: datetime) -> dict[str, object]:
    epoch = int(at.timestamp())
    digest = hashlib.sha1(f"{prefix}:{source_id}:{epoch}".encode()).hexdigest()[:16]
    return {"id": digest, "title": title[:39], "kind": kind[:11], "at": epoch}


def build_schedule(db_factory, now: datetime | None = None) -> list[dict[str, object]]:
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=HORIZON_DAYS)
    items: list[dict[str, object]] = []
    with db_factory() as db:
        for reminder in db.execute(select(Reminder).where(Reminder.completed.is_(False))).scalars():
            for at in _occurrences(reminder.due_at, reminder.recurrence_rule, reminder.timezone_name, now, horizon):
                items.append(_entry("r", reminder.id, reminder.title, "reminder", at))
        for event in db.execute(select(CalendarEvent).where(CalendarEvent.completed.is_(False))).scalars():
            kind = event.kind if event.kind in {"alarm", "timer"} else "event"
            for at in _occurrences(event.start_at, event.recurrence_rule, event.timezone_name, now, horizon):
                items.append(_entry("e", event.id, event.title, kind, at))
    items.sort(key=lambda item: item["at"])
    return items[:MAX_ITEMS]


def schedule_hash(items: list[dict[str, object]]) -> str:
    return hashlib.sha1(json.dumps(items, sort_keys=True).encode()).hexdigest()


def sync_message(device_id: str, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "message_id": f"schedule-{schedule_hash(items)[:12]}",
        "device_id": device_id,
        "timestamp": _dt.now(timezone.utc).isoformat(),
        "type": "schedule.sync",
        "payload": {"items": items, "server_time": int(_dt.now(timezone.utc).timestamp())},
    }


async def push_schedule(application, device_id: str, websocket) -> None:
    import asyncio

    items = await asyncio.to_thread(build_schedule, application.state.db_factory)
    await websocket.send_json(sync_message(device_id, items))
    application.state.schedule_hashes[device_id] = schedule_hash(items)


async def push_changed_schedules(application) -> None:
    """Pushes a new schedule to every connected capable device whose schedule changed."""
    import asyncio

    targets = [
        (device_id, websocket) for device_id, websocket in tuple(application.state.device_sockets.items())
        if "local_schedule" in application.state.device_capabilities.get(device_id, ())
    ]
    if not targets:
        return
    items = await asyncio.to_thread(build_schedule, application.state.db_factory)
    digest = schedule_hash(items)
    for device_id, websocket in targets:
        if application.state.schedule_hashes.get(device_id) == digest:
            continue
        try:
            await websocket.send_json(sync_message(device_id, items))
            application.state.schedule_hashes[device_id] = digest
        except Exception:
            logger.warning("Could not sync schedule to %s", device_id, exc_info=True)


def record_fired(db_factory, device_id: str, payload: dict) -> None:
    with db_factory() as db:
        db.add(AuditLog(
            event_type="schedule.fired", source="device", device_id=device_id,
            action="device fired a local alarm or reminder", result="success",
            metadata_json={"id": str(payload.get("id", ""))[:40], "title": str(payload.get("title", ""))[:80],
                           "kind": str(payload.get("kind", ""))[:12]},
        ))
        db.commit()
