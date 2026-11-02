from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from app.models import (
    AuditLog,
    CalendarEvent,
    Device,
    GameSession,
    IntegrationAccount,
    Reminder,
)
from app.services.games import GAME_NAMES, GameMoveError, new_game
from app.services.device_display import DISPLAY_PAGES, build_display_payload
from app.services.integrations.vault import VaultUnavailable, decrypt_credentials


class IntentError(ValueError):
    pass


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_at: datetime
    timezone_name: str = Field(default="UTC", min_length=1, max_length=64)


class EventArgs(ReminderArgs):
    description: str = Field(default="", max_length=10_000)


class WeatherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class GameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game: str


class DisplayPageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: Literal["home", "weather", "reminders", "notes", "todos", "alarms", "games", "notifications"]
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def validate_coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


@dataclass(frozen=True)
class ActionContext:
    application: Any
    device_id: str
    user_id: str


ActionHandler = Callable[[ActionContext, BaseModel], Any]


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, tuple[type[BaseModel], ActionHandler]] = {}

    def register(self, name: str, schema: type[BaseModel], handler: ActionHandler) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", name) or name in self._actions:
            raise ValueError(f"invalid or duplicate action name: {name}")
        self._actions[name] = (schema, handler)

    def validate(self, name: str, arguments: dict[str, Any]) -> tuple[BaseModel, ActionHandler]:
        registered = self._actions.get(name)
        if registered is None:
            raise IntentError("requested action is not permitted")
        schema, handler = registered
        try:
            validated = schema.model_validate(arguments)
        except ValidationError as exc:
            raise IntentError("action arguments are invalid") from exc
        return validated, handler

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            name: schema.model_json_schema()
            for name, (schema, _) in self._actions.items()
        }


def _record(context: ActionContext, action: str, metadata: dict[str, Any]) -> None:
    with context.application.state.db_factory() as db:
        db.add(AuditLog(
            event_type="voice.action",
            source="device",
            device_id=context.device_id,
            action=action,
            result="success",
            metadata_json={"user_id": context.user_id, **metadata},
        ))
        db.commit()


def _list_calendar(context: ActionContext, _: BaseModel) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with context.application.state.db_factory() as db:
        events = db.execute(
            select(CalendarEvent)
            .where(CalendarEvent.start_at >= now, CalendarEvent.completed.is_(False))
            .order_by(CalendarEvent.start_at)
            .limit(10)
        ).scalars().all()
        result = [{
            "title": item.title,
            "start_at": item.start_at.isoformat(),
            "kind": item.kind,
        } for item in events]
    _record(context, "calendar.list", {"count": len(result)})
    if not result:
        return {"speech": "You have no upcoming calendar events.", "events": []}
    summary = "; ".join(
        f"{item['title']} at {datetime.fromisoformat(item['start_at']).astimezone(timezone.utc).strftime('%A %H:%M UTC')}"
        for item in result[:5]
    )
    return {"speech": f"Your upcoming events are: {summary}.", "events": result}


def _create_reminder(context: ActionContext, args: BaseModel) -> dict[str, Any]:
    values = ReminderArgs.model_validate(args.model_dump())
    if values.due_at.tzinfo is None or values.due_at.utcoffset() is None:
        raise IntentError("reminder time must include a timezone")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        ZoneInfo(values.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise IntentError("reminder timezone is invalid") from exc
    with context.application.state.db_factory() as db:
        reminder = Reminder(
            id=str(uuid4()),
            title=values.title,
            due_at=values.due_at.astimezone(timezone.utc),
            timezone_name=values.timezone_name,
            created_by=context.user_id,
        )
        db.add(reminder)
        db.commit()
        reminder_id = reminder.id
    _record(context, "reminder.created", {"reminder_id": reminder_id})
    return {
        "speech": f"I set a reminder to {values.title} for {values.due_at.isoformat()}.",
        "reminder_id": reminder_id,
    }


def _create_event(context: ActionContext, args: BaseModel) -> dict[str, Any]:
    values = EventArgs.model_validate(args.model_dump())
    if values.due_at.tzinfo is None or values.due_at.utcoffset() is None:
        raise IntentError("event time must include a timezone")
    with context.application.state.db_factory() as db:
        event = CalendarEvent(
            id=str(uuid4()),
            title=values.title,
            description=values.description,
            kind="event",
            start_at=values.due_at.astimezone(timezone.utc),
            timezone_name=values.timezone_name,
            created_by=context.user_id,
        )
        db.add(event)
        db.commit()
        event_id = event.id
    _record(context, "calendar.event_created", {"event_id": event_id})
    return {"speech": f"I added {values.title} to your calendar.", "event_id": event_id}


async def _weather_current(context: ActionContext, args: BaseModel) -> dict[str, Any]:
    values = WeatherArgs.model_validate(args.model_dump())
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3)) as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": values.latitude,
                    "longitude": values.longitude,
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
            )
        response.raise_for_status()
        current = response.json().get("current")
        if not isinstance(current, dict) or "temperature_2m" not in current:
            raise IntentError("weather service returned incomplete current conditions")
    except httpx.HTTPStatusError as exc:
        raise IntentError(f"weather service returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise IntentError("weather service could not be reached") from exc
    temperature = current["temperature_2m"]
    _record(context, "weather.current", {"latitude": values.latitude, "longitude": values.longitude})
    return {"speech": f"It is currently {temperature} degrees Celsius.", "weather": current}


def _start_game(context: ActionContext, args: BaseModel) -> dict[str, Any]:
    values = GameArgs.model_validate(args.model_dump())
    if values.game not in GAME_NAMES:
        raise IntentError("that game is not available")
    try:
        state = new_game(values.game)
    except GameMoveError as exc:
        raise IntentError("the game could not be started") from exc
    with context.application.state.db_factory() as db:
        session = GameSession(
            id=str(uuid4()),
            game=values.game,
            user_id=context.user_id,
            state=state,
        )
        db.add(session)
        db.commit()
        session_id = session.id
    _record(context, "game.started", {"session_id": session_id, "game": values.game})
    return {"speech": f"Starting {values.game.replace('_', ' ')}.", "session_id": session_id, "game": values.game}


async def _show_display_page(context: ActionContext, args: BaseModel) -> dict[str, Any]:
    values = DisplayPageArgs.model_validate(args.model_dump())
    with context.application.state.db_factory() as db:
        device = db.get(Device, context.device_id)
        location = device.config.get("display_location", {}) if device else {}
    if values.page == "weather":
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if values.latitude is not None and values.longitude is not None:
            latitude, longitude = values.latitude, values.longitude
        if latitude is None or longitude is None:
            raise IntentError("Set the device location in Display Studio before showing weather.")
        weather_result = await _weather_current(
            context,
            WeatherArgs(latitude=latitude, longitude=longitude),
        )
        return {
            "speech": weather_result["speech"],
            "display": build_display_payload(
                context.application.state.db_factory,
                values.page,
                {"summary": f"{weather_result['weather']['temperature_2m']} C"},
            ),
        }
    if values.page not in DISPLAY_PAGES:
        raise IntentError("That display page is not available.")
    display = build_display_payload(context.application.state.db_factory, values.page)
    _record(context, "display.page", {"page": values.page})
    return {"speech": f"Showing {values.page.replace('_', ' ')}.", "display": display}


async def _github_notifications(context: ActionContext, _: BaseModel) -> dict[str, Any]:
    settings = context.application.state.settings
    token = settings.github_token
    if not token:
        with context.application.state.db_factory() as db:
            account = db.execute(
                select(IntegrationAccount).where(IntegrationAccount.provider == "github")
            ).scalar_one_or_none()
            ciphertext = account.encrypted_credentials if account else None
        if ciphertext:
            try:
                token = str(decrypt_credentials(ciphertext, settings).get("token", ""))
            except VaultUnavailable as exc:
                raise IntentError("GitHub credentials could not be decrypted") from exc
    if not token:
        raise IntentError("GitHub is not connected")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
            response = await client.get(
                "https://api.github.com/notifications",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code in {401, 403}:
            raise IntentError("GitHub rejected the configured token or its permissions")
        response.raise_for_status()
        notifications = response.json()
    except httpx.HTTPStatusError as exc:
        raise IntentError(f"GitHub returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise IntentError("GitHub could not be reached") from exc
    if not isinstance(notifications, list):
        raise IntentError("GitHub returned an invalid notifications response")
    _record(context, "integration.github_notifications", {"count": len(notifications)})
    return {
        "speech": f"You have {len(notifications)} unread GitHub notifications.",
        "count": len(notifications),
    }


def build_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register("calendar.list", EmptyArgs, _list_calendar)
    registry.register("calendar.create_event", EventArgs, _create_event)
    registry.register("reminder.create", ReminderArgs, _create_reminder)
    registry.register("weather.current", WeatherArgs, _weather_current)
    registry.register("game.start", GameArgs, _start_game)
    registry.register("display.show_page", DisplayPageArgs, _show_display_page)
    registry.register("integration.github_notifications", EmptyArgs, _github_notifications)
    return registry


ACTION_REGISTRY = build_registry()
GAME_ALIASES = {
    "tic tac toe": "tic_tac_toe",
    "rock paper scissors": "rock_paper_scissors",
    "number guessing": "number_guessing",
    "trivia": "trivia",
    "hangman": "hangman",
    "word association": "word_association",
}
ISO_DATETIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})"


def deterministic_intent(text: str) -> tuple[str, dict[str, Any]] | None:
    normalized = text.strip()
    lower = normalized.casefold()
    if re.search(r"\b(calendar|agenda|schedule)\b", lower) and re.search(r"\b(what|show|list|upcoming|next)\b", lower):
        return "calendar.list", {}
    reminder = re.search(
        rf"\bremind me to\s+(.+?)\s+at\s+({ISO_DATETIME})\b",
        normalized,
        re.IGNORECASE,
    )
    if reminder:
        title, due_at = reminder.groups()
        return "reminder.create", {
            "title": title.strip(" ."),
            "due_at": due_at.replace("Z", "+00:00"),
            "timezone_name": "UTC",
        }
    event = re.search(
        rf"\b(?:create|add)\s+(?:a\s+)?event\s+(.+?)\s+at\s+({ISO_DATETIME})\b",
        normalized,
        re.IGNORECASE,
    )
    if event:
        title, due_at = event.groups()
        return "calendar.create_event", {
            "title": title.strip(" ."),
            "due_at": due_at.replace("Z", "+00:00"),
            "timezone_name": "UTC",
        }
    if re.search(r"\b(weather|temperature|forecast)\b", lower) and not re.search(
        r"\b(?:show|open|view|display)\b", lower
    ):
        coords = re.search(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b", normalized)
        if coords:
            return "weather.current", {"latitude": float(coords.group(1)), "longitude": float(coords.group(2))}
    page_patterns = (
        (r"\b(?:show|open|view|display)\b.*\b(?:dashboard|home)\b|\b(?:dashboard|home)\b.*\b(?:show|open|view)\b", "home"),
        (r"\b(?:show|open|view|display)\b.*\bweather\b", "weather"),
        (r"\b(?:show|open|view|display)\b.*\breminders?\b", "reminders"),
        (r"\b(?:show|open|view|display)\b.*\bnotes?\b", "notes"),
        (r"\b(?:show|open|view|display)\b.*\b(?:to[\s-]?dos?|tasks?)\b", "todos"),
        (r"\b(?:show|open|view|display)\b.*\balarms?\b", "alarms"),
        (r"\b(?:show|open|view|display)\b.*\bgames?\b", "games"),
        (r"\b(?:show|open|view|display)\b.*\bnotifications?\b", "notifications"),
    )
    for pattern, page in page_patterns:
        if re.search(pattern, lower):
            arguments: dict[str, Any] = {"page": page}
            if page == "weather":
                coords = re.search(
                    r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b",
                    normalized,
                )
                if coords:
                    arguments.update(latitude=float(coords.group(1)), longitude=float(coords.group(2)))
            return "display.show_page", arguments
    for spoken_name, game in GAME_ALIASES.items():
        if re.search(rf"\b(?:play|start)\s+(?:a\s+game\s+of\s+)?{re.escape(spoken_name)}\b", lower):
            return "game.start", {"game": game}
    if re.search(r"\bgithub\b", lower) and re.search(r"\b(notification|notifications|inbox)\b", lower):
        return "integration.github_notifications", {}
    return None


async def execute_action(context: ActionContext, name: str, arguments: dict[str, Any]) -> Any:
    validated, handler = ACTION_REGISTRY.validate(name, arguments)
    result = handler(context, validated)
    if hasattr(result, "__await__"):
        return await result
    return result
