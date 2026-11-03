from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.api.dependencies import require_user
from app.models import AuditLog, CalendarEvent, Note, Notification, Reminder, Todo, User

router = APIRouter(dependencies=[Depends(require_user)])
EventKind = Literal["event", "meeting", "birthday", "function", "alarm"]


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    kind: EventKind = "event"
    start_at: datetime
    end_at: datetime | None = None
    timezone_name: str = Field(min_length=1, max_length=64)
    recurrence_rule: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_times(self):
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("start_at must include a UTC offset")
        if self.end_at is not None:
            if self.end_at.tzinfo is None or self.end_at.utcoffset() is None:
                raise ValueError("end_at must include a UTC offset")
            if self.end_at <= self.start_at:
                raise ValueError("end_at must be later than start_at")
        _timezone(self.timezone_name)
        if self.recurrence_rule:
            _validate_recurrence(self.recurrence_rule, self.start_at)
        return self


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    kind: EventKind | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone_name: str | None = Field(default=None, min_length=1, max_length=64)
    recurrence_rule: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_updated_values(self):
        provided = self.model_fields_set
        for field in ("title", "description", "kind", "start_at", "timezone_name"):
            if field in provided and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.start_at and (self.start_at.tzinfo is None or self.start_at.utcoffset() is None):
            raise ValueError("start_at must include a UTC offset")
        if self.end_at and (self.end_at.tzinfo is None or self.end_at.utcoffset() is None):
            raise ValueError("end_at must include a UTC offset")
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")
        if self.timezone_name:
            _timezone(self.timezone_name)
        if self.recurrence_rule and self.start_at:
            _validate_recurrence(self.recurrence_rule, self.start_at)
        return self


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: datetime
    timezone_name: str = Field(min_length=1, max_length=64)
    recurrence_rule: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_time(self):
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError("due_at must include a UTC offset")
        _timezone(self.timezone_name)
        if self.recurrence_rule:
            _validate_recurrence(self.recurrence_rule, self.due_at)
        return self


class CompletionUpdate(BaseModel):
    completed: bool


class NotificationUpdate(BaseModel):
    delivered: bool


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: datetime | None = None

    @model_validator(mode="after")
    def validate_due_at(self):
        if self.due_at is not None and (self.due_at.tzinfo is None or self.due_at.utcoffset() is None):
            raise ValueError("due_at must include a UTC offset")
        return self


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone_name must be a valid IANA timezone") from exc


def _utc_iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat()


def _validate_recurrence(rule: str, start: datetime) -> None:
    if not rule.upper().startswith("FREQ="):
        raise ValueError("recurrence_rule must be an RFC 5545 RRULE value")
    try:
        rrulestr(rule, dtstart=start)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid RFC 5545 recurrence_rule") from exc


def _event_dict(event: CalendarEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "kind": event.kind,
        "start_at": _utc_iso(event.start_at),
        "end_at": _utc_iso(event.end_at) if event.end_at else None,
        "timezone_name": event.timezone_name,
        "recurrence_rule": event.recurrence_rule,
        "completed": event.completed,
        "created_at": _utc_iso(event.created_at),
    }


def _audit(request: Request, user_id: str, action: str, metadata: dict) -> None:
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            event_type="calendar.action",
            source="dashboard",
            device_id=None,
            action=action,
            result="success",
            metadata_json={"user_id": user_id, **metadata},
        ))
        db.commit()


@router.get("/calendar/events")
def list_events(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: EventKind | None = None,
    search: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    if start and (start.tzinfo is None or start.utcoffset() is None):
        raise HTTPException(422, "start filter must include a UTC offset")
    if end and (end.tzinfo is None or end.utcoffset() is None):
        raise HTTPException(422, "end filter must include a UTC offset")
    if start and end and end <= start:
        raise HTTPException(422, "end filter must be later than start")
    with request.app.state.db_factory() as db:
        query = select(CalendarEvent)
        if start:
            query = query.where(CalendarEvent.start_at >= start.astimezone(timezone.utc))
        if end:
            query = query.where(CalendarEvent.start_at < end.astimezone(timezone.utc))
        if kind:
            query = query.where(CalendarEvent.kind == kind)
        if search:
            query = query.where(CalendarEvent.title.ilike(f"%{search}%"))
        from sqlalchemy import func

        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        events = db.execute(query.order_by(CalendarEvent.start_at).offset(offset).limit(limit)).scalars().all()
        return {"items": [_event_dict(event) for event in events], "total": total, "offset": offset, "limit": limit}


@router.post("/calendar/events", status_code=201)
def create_event(body: EventCreate, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        event = CalendarEvent(
            title=body.title,
            description=body.description,
            kind=body.kind,
            start_at=body.start_at.astimezone(timezone.utc),
            end_at=body.end_at.astimezone(timezone.utc) if body.end_at else None,
            timezone_name=body.timezone_name,
            recurrence_rule=body.recurrence_rule,
            created_by=user.id,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        data = _event_dict(event)
    _audit(request, user.id, "event.created", {"event_id": data["id"], "kind": body.kind})
    return data


@router.patch("/calendar/events/{event_id}")
def update_event(
    event_id: str,
    body: EventUpdate,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        event = db.get(CalendarEvent, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        values = body.model_dump(exclude_unset=True)
        if body.recurrence_rule and body.start_at is None:
            _validate_recurrence(body.recurrence_rule, event.start_at)
        for field in ("title", "description", "kind", "timezone_name", "recurrence_rule"):
            if field in values:
                setattr(event, field, values[field])
        if body.start_at is not None:
            event.start_at = body.start_at.astimezone(timezone.utc)
        if "end_at" in values:
            event.end_at = body.end_at.astimezone(timezone.utc) if body.end_at else None
        if event.end_at is not None and event.end_at <= event.start_at:
            raise HTTPException(422, "end_at must be later than start_at")
        db.commit()
        db.refresh(event)
        data = _event_dict(event)
    _audit(request, user.id, "event.updated", {"event_id": event_id})
    return data


@router.delete("/calendar/events/{event_id}", status_code=204)
def delete_event(event_id: str, request: Request, user: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        event = db.get(CalendarEvent, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        db.delete(event)
        db.commit()
    _audit(request, user.id, "event.deleted", {"event_id": event_id})
    return Response(status_code=204)


@router.get("/reminders")
def list_reminders(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    if start and (start.tzinfo is None or start.utcoffset() is None):
        raise HTTPException(422, "start filter must include a UTC offset")
    if end and (end.tzinfo is None or end.utcoffset() is None):
        raise HTTPException(422, "end filter must include a UTC offset")
    with request.app.state.db_factory() as db:
        query = select(Reminder)
        if start:
            query = query.where(Reminder.due_at >= start.astimezone(timezone.utc))
        if end:
            query = query.where(Reminder.due_at < end.astimezone(timezone.utc))
        rows = db.execute(query.order_by(Reminder.due_at)).scalars().all()
        return {
            "items": [{
                "id": row.id, "title": row.title, "due_at": _utc_iso(row.due_at),
                "timezone_name": row.timezone_name, "recurrence_rule": row.recurrence_rule,
                "completed": row.completed, "event_id": row.event_id,
            } for row in rows[offset:offset + limit]],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
        }


@router.post("/reminders", status_code=201)
def create_reminder(body: ReminderCreate, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        reminder = Reminder(
            title=body.title,
            due_at=body.due_at.astimezone(timezone.utc),
            timezone_name=body.timezone_name,
            recurrence_rule=body.recurrence_rule,
            created_by=user.id,
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)
        response = {
            "id": reminder.id, "title": reminder.title, "due_at": _utc_iso(reminder.due_at),
            "timezone_name": reminder.timezone_name, "recurrence_rule": reminder.recurrence_rule,
            "completed": reminder.completed,
        }
    _audit(request, user.id, "reminder.created", {"reminder_id": response["id"]})
    return response


@router.patch("/reminders/{reminder_id}")
def complete_reminder(
    reminder_id: str,
    body: CompletionUpdate,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        reminder = db.get(Reminder, reminder_id)
        if reminder is None:
            raise HTTPException(404, "reminder not found")
        reminder.completed = body.completed
        db.commit()
        result = {"id": reminder.id, "completed": reminder.completed}
    _audit(request, user.id, "reminder.completion_changed", {"reminder_id": reminder_id, "completed": body.completed})
    return result


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: str, request: Request, user: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        reminder = db.get(Reminder, reminder_id)
        if reminder is None:
            raise HTTPException(404, "reminder not found")
        db.delete(reminder)
        db.commit()
    _audit(request, user.id, "reminder.deleted", {"reminder_id": reminder_id})
    return Response(status_code=204)


@router.post("/notes", status_code=201)
def create_note(body: NoteCreate, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        note = Note(body=body.body, created_by=user.id)
        db.add(note)
        db.commit()
        db.refresh(note)
        result = {"id": note.id, "body": note.body, "created_at": _utc_iso(note.created_at)}
    _audit(request, user.id, "note.created", {"note_id": result["id"]})
    return result


@router.get("/notes")
def list_notes(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        rows = db.execute(select(Note).order_by(Note.created_at.desc()).offset(offset).limit(limit)).scalars().all()
        return {"items": [{"id": row.id, "body": row.body, "created_at": _utc_iso(row.created_at)} for row in rows]}


@router.get("/todos")
def list_todos(
    request: Request,
    completed: bool | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        query = select(Todo)
        if completed is not None:
            query = query.where(Todo.completed.is_(completed))
        rows = db.execute(
            query.order_by(Todo.completed, Todo.due_at.is_(None), Todo.due_at, Todo.created_at.desc())
            .offset(offset).limit(limit)
        ).scalars().all()
        return {"items": [{
            "id": row.id,
            "title": row.title,
            "due_at": _utc_iso(row.due_at) if row.due_at else None,
            "completed": row.completed,
            "created_at": _utc_iso(row.created_at),
        } for row in rows]}


@router.post("/todos", status_code=201)
def create_todo(body: TodoCreate, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        todo = Todo(
            title=body.title,
            due_at=body.due_at.astimezone(timezone.utc) if body.due_at else None,
            created_by=user.id,
        )
        db.add(todo)
        db.commit()
        db.refresh(todo)
        result = {
            "id": todo.id,
            "title": todo.title,
            "due_at": _utc_iso(todo.due_at) if todo.due_at else None,
            "completed": todo.completed,
            "created_at": _utc_iso(todo.created_at),
        }
    _audit(request, user.id, "todo.created", {"todo_id": result["id"]})
    return result


@router.patch("/todos/{todo_id}")
def update_todo(
    todo_id: str,
    body: CompletionUpdate,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        todo = db.get(Todo, todo_id)
        if todo is None:
            raise HTTPException(404, "todo not found")
        todo.completed = body.completed
        db.commit()
        result = {"id": todo.id, "completed": todo.completed}
    _audit(request, user.id, "todo.completion_changed", {
        "todo_id": todo_id,
        "completed": body.completed,
    })
    return result


@router.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: str, request: Request, user: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        todo = db.get(Todo, todo_id)
        if todo is None:
            raise HTTPException(404, "todo not found")
        db.delete(todo)
        db.commit()
    _audit(request, user.id, "todo.deleted", {"todo_id": todo_id})
    return Response(status_code=204)


@router.get("/notifications")
def list_notifications(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        rows = db.execute(
            select(Notification).order_by(Notification.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all()
        total = db.query(Notification).count()
        return {
            "items": [{
                "id": row.id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "title": row.title,
                "payload": row.payload,
                "delivered": row.delivered,
                "created_at": _utc_iso(row.created_at),
            } for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }


@router.patch("/notifications/{notification_id}")
def mark_notification(
    notification_id: str,
    body: NotificationUpdate,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        notification = db.get(Notification, notification_id)
        if notification is None:
            raise HTTPException(404, "notification not found")
        notification.delivered = body.delivered
        db.commit()
        result = {"id": notification.id, "delivered": notification.delivered}
    _audit(request, user.id, "notification.delivery_state_updated", result)
    return result


@router.get("/calendar/export.csv")
def export_calendar_csv(request: Request, _: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        events = db.execute(select(CalendarEvent).order_by(CalendarEvent.start_at)).scalars().all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "title", "description", "kind", "start_at", "end_at", "timezone", "recurrence_rule", "completed"])
        for event in events:
            writer.writerow([
                _csv_safe(event.id), _csv_safe(event.title), _csv_safe(event.description), _csv_safe(event.kind),
                _utc_iso(event.start_at), _utc_iso(event.end_at) if event.end_at else "",
                _csv_safe(event.timezone_name), _csv_safe(event.recurrence_rule or ""), event.completed,
            ])
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=calendar.csv"})


def _csv_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
