from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select

from app.models import CalendarEvent, GameSession, Note, Notification, Reminder, Todo

DISPLAY_PAGES = {
    "home", "weather", "reminders", "notes", "todos", "alarms",
    "games", "notifications",
}


async def fetch_display_weather(latitude: float, longitude: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "timezone": "auto",
            },
        )
    response.raise_for_status()
    current = response.json().get("current")
    if not isinstance(current, dict) or "temperature_2m" not in current:
        raise ValueError("weather service returned incomplete current conditions")
    return {
        "temperature_c": current["temperature_2m"],
        "humidity_percent": current.get("relative_humidity_2m"),
        "weather_code": current.get("weather_code"),
        "summary": f"{current['temperature_2m']} C",
    }


def build_display_payload(db_factory, page: str = "home", weather: dict[str, Any] | None = None) -> dict[str, Any]:
    if page not in DISPLAY_PAGES:
        raise ValueError(f"unsupported display page: {page}")

    now = datetime.now(timezone.utc)
    with db_factory() as db:
        counts = {
            "todos": db.scalar(select(func.count()).select_from(Todo).where(Todo.completed.is_(False))) or 0,
            "reminders": db.scalar(select(func.count()).select_from(Reminder).where(Reminder.completed.is_(False))) or 0,
            "notes": db.scalar(select(func.count()).select_from(Note)) or 0,
            "notifications": db.scalar(
                select(func.count()).select_from(Notification).where(Notification.delivered.is_(False))
            ) or 0,
            "alarms": db.scalar(
                select(func.count()).select_from(CalendarEvent).where(
                    CalendarEvent.kind == "alarm",
                    CalendarEvent.completed.is_(False),
                    CalendarEvent.start_at >= now,
                )
            ) or 0,
            "games": db.scalar(
                select(func.count()).select_from(GameSession).where(GameSession.status == "active")
            ) or 0,
        }

        if page == "todos":
            rows = db.execute(
                select(Todo).where(Todo.completed.is_(False))
                .order_by(Todo.due_at.is_(None), Todo.due_at, Todo.created_at.desc()).limit(3)
            ).scalars().all()
            items = [row.title for row in rows]
            title = "TODO LIST"
            detail = f"{counts['todos']} PENDING"
        elif page == "reminders":
            rows = db.execute(
                select(Reminder).where(Reminder.completed.is_(False))
                .order_by(Reminder.due_at).limit(3)
            ).scalars().all()
            items = [f"{row.title} {row.due_at.astimezone(timezone.utc).strftime('%H:%M')}" for row in rows]
            title = "REMINDERS"
            detail = f"{counts['reminders']} ACTIVE"
        elif page == "notes":
            rows = db.execute(select(Note).order_by(Note.created_at.desc()).limit(3)).scalars().all()
            items = [row.body.replace("\n", " ") for row in rows]
            title = "NOTES"
            detail = f"{counts['notes']} SAVED"
        elif page == "alarms":
            rows = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.kind == "alarm",
                    CalendarEvent.completed.is_(False),
                    CalendarEvent.start_at >= now,
                ).order_by(CalendarEvent.start_at).limit(3)
            ).scalars().all()
            items = [f"{row.title} {row.start_at.astimezone(timezone.utc).strftime('%m-%d %H:%M')}" for row in rows]
            title = "ALARMS"
            detail = f"{counts['alarms']} UPCOMING"
        elif page == "notifications":
            rows = db.execute(
                select(Notification).where(Notification.delivered.is_(False))
                .order_by(Notification.created_at.desc()).limit(3)
            ).scalars().all()
            items = [row.title for row in rows]
            title = "ALERTS"
            detail = f"{counts['notifications']} NEW"
        elif page == "games":
            rows = db.execute(
                select(GameSession).where(GameSession.status == "active")
                .order_by(GameSession.updated_at.desc()).limit(3)
            ).scalars().all()
            items = [row.game.replace("_", " ") for row in rows]
            title = "GAMES"
            detail = f"{counts['games']} ACTIVE"
        elif page == "weather":
            title = "WEATHER"
            detail = "CURRENT CONDITIONS"
            items = [str(weather.get("summary", "SET LOCATION IN DASHBOARD"))] if weather else [
                "Set location in dashboard"
            ]
        else:
            upcoming = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.completed.is_(False), CalendarEvent.start_at >= now
                ).order_by(CalendarEvent.start_at).limit(2)
            ).scalars().all()
            title = "ASSISTANT"
            detail = "LIVE OVERVIEW"
            items = [
                f"TODO {counts['todos']} REM {counts['reminders']}",
                f"NOTE {counts['notes']} ALERT {counts['notifications']}",
                *[f"{event.title} {event.start_at.astimezone(timezone.utc).strftime('%H:%M')}" for event in upcoming],
            ]

    return {
        "state": "dashboard",
        "page": page,
        "title": title,
        "detail": detail,
        "items": [str(item)[:96] for item in items[:4]],
        "counts": counts,
        "weather": weather,
    }
