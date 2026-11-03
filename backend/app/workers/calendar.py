from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from dateutil.rrule import rrulestr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from zoneinfo import ZoneInfo

from app.models import AuditLog, CalendarEvent, Notification, Reminder
from app.services.schedule_sync import push_changed_schedules
from app.services.automation.health_checks import check_endpoint, due_endpoint_ids

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def process_due_notifications(application) -> list[dict[str, object]]:
    now = datetime.now(timezone.utc)
    with application.state.db_factory() as db:
        retention_start = now.timestamp() - application.state.settings.audit_retention_days * 86400
        retention_cutoff = datetime.fromtimestamp(retention_start, timezone.utc)
        db.execute(delete(AuditLog).where(AuditLog.created_at < retention_cutoff))
        db.execute(delete(Notification).where(Notification.created_at < retention_cutoff))
        reminders = db.execute(
            select(Reminder)
            .where(Reminder.due_at <= now, Reminder.completed.is_(False))
            .order_by(Reminder.due_at)
            .limit(100)
        ).scalars().all()
        for reminder in reminders:
            due_at = _as_utc(reminder.due_at)
            _create_notification(
                db, source_type="reminder", source_id=reminder.id, occurrence=due_at,
                title=reminder.title, payload={"kind": "reminder", "timezone_name": reminder.timezone_name},
            )
            if reminder.recurrence_rule:
                timezone_info = ZoneInfo(reminder.timezone_name)
                due_local = due_at.astimezone(timezone_info)
                next_due = rrulestr(reminder.recurrence_rule, dtstart=due_local).after(now.astimezone(timezone_info), inc=False)
                if next_due is None:
                    reminder.completed = True
                else:
                    reminder.due_at = next_due.astimezone(timezone.utc)
            else:
                reminder.completed = True

        events = db.execute(
            select(CalendarEvent)
            .where(CalendarEvent.start_at <= now, CalendarEvent.completed.is_(False))
            .order_by(CalendarEvent.start_at)
            .limit(100)
        ).scalars().all()
        for event in events:
            start_at = _as_utc(event.start_at)
            occurrence = start_at
            if event.recurrence_rule:
                local_start = start_at.astimezone(ZoneInfo(event.timezone_name))
                local_now = now.astimezone(ZoneInfo(event.timezone_name))
                occurrence_local = rrulestr(event.recurrence_rule, dtstart=local_start).before(local_now, inc=True)
                if occurrence_local is None:
                    continue
                occurrence = occurrence_local.astimezone(timezone.utc)
                if (now - occurrence).total_seconds() > 60:
                    continue
            elif (now - start_at).total_seconds() > 60:
                continue
            _create_notification(
                db, source_type="calendar_event", source_id=event.id, occurrence=occurrence,
                title=event.title, payload={"kind": event.kind, "timezone_name": event.timezone_name},
            )
        db.commit()
        rows = db.execute(
            select(Notification).where(Notification.delivered.is_(False)).order_by(Notification.created_at).limit(100)
        ).scalars().all()
        return [{
            "id": row.id,
            "source_type": row.source_type,
            "title": row.title,
            "payload": row.payload,
        } for row in rows]


def _create_notification(db, *, source_type: str, source_id: str, occurrence: datetime, title: str, payload: dict) -> bool:
    existing = db.scalar(select(Notification.id).where(
        Notification.source_type == source_type,
        Notification.source_id == source_id,
        Notification.occurrence_at == occurrence,
    ))
    if existing is not None:
        return False
    notification = Notification(
        id=str(uuid4()),
        source_type=source_type,
        source_id=source_id,
        occurrence_at=occurrence,
        title=title,
        payload=payload,
    )
    db.add(notification)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        return False
    db.add(AuditLog(
        event_type="calendar.notification",
        source="scheduler",
        action="due notification created",
        result="success",
        metadata_json={"source_type": source_type, "source_id": source_id},
    ))
    return True


async def scheduler_loop(application) -> None:
    while True:
        try:
            pending = await asyncio.to_thread(process_due_notifications, application)
            await push_changed_schedules(application)
            for notification in pending:
                await broadcast_notification(application, notification)
            endpoint_ids = await asyncio.to_thread(due_endpoint_ids, application)
            if endpoint_ids:
                await asyncio.gather(
                    *(check_endpoint(application, endpoint_id) for endpoint_id in endpoint_ids),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Calendar notification worker failed")
        await asyncio.sleep(10)


async def broadcast_notification(application, notification: dict[str, object]) -> None:
    sockets = tuple(application.state.device_sockets.items())
    if not sockets:
        return
    delivered = False
    for device_id, websocket in sockets:
        if "local_schedule" in application.state.device_capabilities.get(device_id, ()):
            delivered = True  # the device fires this item from its own stored schedule
            continue
        try:
            await websocket.send_json({
                "protocol_version": 1,
                "message_id": str(notification["id"]),
                "device_id": device_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "calendar.notification",
                "payload": {
                    "notification_id": notification["id"],
                    "title": notification["title"],
                    "source_type": notification["source_type"],
                    **notification["payload"],
                },
            })
            delivered = True
        except Exception:
            logger.exception("Could not deliver calendar notification to device %s", device_id)
    if delivered:
        with application.state.db_factory() as db:
            row = db.get(Notification, notification["id"])
            if row is not None:
                row.delivered = True
                db.commit()
