from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any

import httpx
from sqlalchemy import func, or_, select

from app.models import CalendarEvent, GameSession, Note, Notification, Reminder, Todo

DISPLAY_PAGES = {
    "home", "weather", "reminders", "notes", "todos", "alarms",
    "games", "calendar", "notifications", "news",
}

PAGE_EMOJI = {
    "home": "🙂",
    "weather": "☀️",
    "reminders": "⏰",
    "notes": "📝",
    "todos": "✅",
    "alarms": "⏰",
    "games": "🎮",
    "calendar": "📅",
    "notifications": "🔔",
    "news": "📰",
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


def _clock(value: datetime, zone: tzinfo, pattern: str = "%H:%M") -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone).strftime(pattern)


def build_display_payload(
    db_factory,
    page: str = "home",
    weather: dict[str, Any] | None = None,
    *,
    tz: tzinfo | None = None,
    content: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    if page not in DISPLAY_PAGES:
        raise ValueError(f"unsupported display page: {page}")

    now = datetime.now(timezone.utc)
    zone = tz or timezone.utc
    def owner(column):
        return True if user_id is None else or_(column == user_id, column.is_(None))

    with db_factory() as db:
        counts = {
            "todos": db.scalar(select(func.count()).select_from(Todo).where(Todo.completed.is_(False), owner(Todo.created_by))) or 0,
            "reminders": db.scalar(select(func.count()).select_from(Reminder).where(Reminder.completed.is_(False), owner(Reminder.created_by))) or 0,
            "notes": db.scalar(select(func.count()).select_from(Note).where(owner(Note.created_by))) or 0,
            "notifications": db.scalar(
                select(func.count()).select_from(Notification).where(Notification.delivered.is_(False))
            ) or 0,
            "alarms": db.scalar(
                select(func.count()).select_from(CalendarEvent).where(
                    CalendarEvent.kind == "alarm",
                    CalendarEvent.completed.is_(False),
                    CalendarEvent.start_at >= now,
                    owner(CalendarEvent.created_by),
                )
            ) or 0,
            "games": db.scalar(
                select(func.count()).select_from(GameSession).where(GameSession.status == "active")
            ) or 0,
        }

        if page == "todos":
            rows = db.execute(
                select(Todo).where(Todo.completed.is_(False), owner(Todo.created_by))
                .order_by(Todo.due_at.is_(None), Todo.due_at, Todo.created_at.desc()).limit(3)
            ).scalars().all()
            items = [row.title for row in rows]
            title = "TODO LIST"
            detail = f"{counts['todos']} PENDING"
        elif page == "reminders":
            rows = db.execute(
                select(Reminder).where(Reminder.completed.is_(False), owner(Reminder.created_by))
                .order_by(Reminder.due_at).limit(3)
            ).scalars().all()
            items = [f"{row.title} {_clock(row.due_at, zone)}" for row in rows]
            title = "REMINDERS"
            detail = f"{counts['reminders']} ACTIVE"
        elif page == "notes":
            rows = db.execute(select(Note).where(owner(Note.created_by)).order_by(Note.created_at.desc()).limit(3)).scalars().all()
            items = [row.body.replace("\n", " ") for row in rows]
            title = "NOTES"
            detail = f"{counts['notes']} SAVED"
        elif page == "alarms":
            rows = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.kind == "alarm",
                    CalendarEvent.completed.is_(False),
                    CalendarEvent.start_at >= now,
                    owner(CalendarEvent.created_by),
                ).order_by(CalendarEvent.start_at).limit(3)
            ).scalars().all()
            items = [f"{row.title} {_clock(row.start_at, zone, '%d/%m %H:%M')}" for row in rows]
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
        elif page == "calendar":
            rows = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.completed.is_(False),
                    CalendarEvent.start_at >= now,
                    CalendarEvent.kind.not_in(("alarm", "timer")),
                    owner(CalendarEvent.created_by),
                ).order_by(CalendarEvent.start_at).limit(4)
            ).scalars().all()
            items = [
                f"{_clock(row.start_at, zone, '%d/%m %H:%M')} {row.title}"
                for row in rows
            ]
            title = "CALENDAR"
            detail = f"{len(rows)} UPCOMING"
        elif page == "games":
            rows = db.execute(
                select(GameSession).where(GameSession.status == "active")
                .order_by(GameSession.updated_at.desc()).limit(3)
            ).scalars().all()
            items = [row.game.replace("_", " ") for row in rows]
            title = "GAMES"
            detail = f"{counts['games']} ACTIVE"
        elif page == "news":
            title = "NEWS"
            detail = "LATEST HEADLINES"
            items = ["Ask: news about sports"]
        elif page == "weather":
            title = "WEATHER"
            detail = "CURRENT CONDITIONS"
            items = [str(weather.get("summary", "SET LOCATION IN DASHBOARD"))] if weather else [
                "Set location in dashboard"
            ]
        else:
            upcoming = db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.completed.is_(False), CalendarEvent.start_at >= now,
                    owner(CalendarEvent.created_by),
                ).order_by(CalendarEvent.start_at).limit(2)
            ).scalars().all()
            title = "ASSISTANT"
            detail = "LIVE OVERVIEW"
            items = [
                f"TODO {counts['todos']} REM {counts['reminders']}",
                f"NOTE {counts['notes']} ALERT {counts['notifications']}",
                *[f"{event.title} {_clock(event.start_at, zone)}" for event in upcoming],
            ]

    if content:
        title = content.get("title", title)
        detail = content.get("detail", detail)
        items = content.get("items", items)
    return {
        "state": "dashboard",
        "page": page,
        "title": str(title)[:39],
        "detail": str(detail)[:47],
        "items": [str(item)[:96] for item in items[:8]],
        "counts": counts,
        "emoji": PAGE_EMOJI[page],
        "weather": weather,
    }


WMO_DESCRIPTIONS = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


async def fetch_weather_detail(latitude: float, longitude: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 1,
            },
        )
    response.raise_for_status()
    body = response.json()
    current = body.get("current")
    if not isinstance(current, dict) or "temperature_2m" not in current:
        raise ValueError("weather service returned incomplete current conditions")
    daily = body.get("daily") or {}
    first = lambda key: (daily.get(key) or [None])[0]
    return {
        "temperature_c": current["temperature_2m"],
        "feels_like_c": current.get("apparent_temperature"),
        "humidity_percent": current.get("relative_humidity_2m"),
        "wind_kmh": current.get("wind_speed_10m"),
        "description": WMO_DESCRIPTIONS.get(current.get("weather_code"), "current conditions"),
        "high_c": first("temperature_2m_max"),
        "low_c": first("temperature_2m_min"),
        "rain_chance_percent": first("precipitation_probability_max"),
    }
