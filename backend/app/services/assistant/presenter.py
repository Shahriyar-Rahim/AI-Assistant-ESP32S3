from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from sqlalchemy import or_, select

from app.models import CalendarEvent, Device, Notification, Reminder, Todo
from app.services import news as news_service
from app.services.device_display import build_display_payload, fetch_weather_detail

logger = logging.getLogger(__name__)

HOLD_SECONDS = 8.0
SCENE_PAUSE_SECONDS = 0.5
SETTLE_SECONDS = 3.0
_SECTION_ORDER = ("alarms", "reminders", "todos", "events", "notifications")
_CREATE_WORDS = re.compile(
    r"\b(set|create|add|make|new|remind me|delete|remove|cancel|clear|complete|finish|mark|snooze)\b"
)
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "sixty": 60,
}


@dataclass
class Scene:
    page: str
    title: str
    detail: str
    items: list[str]
    speech: str
    weather: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _orchestrator():
    from app.services.assistant import orchestrator
    return orchestrator


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clock(value: datetime, zone: tzinfo) -> str:
    return _aware(value).astimezone(zone).strftime("%H:%M")


def _spoken_when(value: datetime, zone: tzinfo) -> str:
    local = _aware(value).astimezone(zone)
    today = datetime.now(zone).date()
    clock = local.strftime("%I:%M %p").lstrip("0").replace(":00", "")
    if local.date() == today:
        day = "today"
    elif local.date() == today + timedelta(days=1):
        day = "tomorrow"
    elif local.date() < today:
        day = "earlier"
    else:
        day = local.strftime("on %A")
    return f"{day} at {clock}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _owner(column, user_id: str | None):
    return True if user_id is None else or_(column == user_id, column.is_(None))


def load_agenda(application, user_id: str | None) -> dict[str, list]:
    now = datetime.now(timezone.utc)
    with application.state.db_factory() as db:
        alarms = db.execute(
            select(CalendarEvent).where(
                CalendarEvent.kind == "alarm", CalendarEvent.completed.is_(False),
                CalendarEvent.start_at >= now, _owner(CalendarEvent.created_by, user_id),
            ).order_by(CalendarEvent.start_at).limit(20)
        ).scalars().all()
        events = db.execute(
            select(CalendarEvent).where(
                CalendarEvent.kind != "alarm", CalendarEvent.completed.is_(False),
                CalendarEvent.start_at >= now, _owner(CalendarEvent.created_by, user_id),
            ).order_by(CalendarEvent.start_at).limit(20)
        ).scalars().all()
        reminders = db.execute(
            select(Reminder).where(
                Reminder.completed.is_(False), _owner(Reminder.created_by, user_id),
            ).order_by(Reminder.due_at).limit(20)
        ).scalars().all()
        todos = db.execute(
            select(Todo).where(
                Todo.completed.is_(False), _owner(Todo.created_by, user_id),
            ).order_by(Todo.due_at.is_(None), Todo.due_at, Todo.created_at).limit(20)
        ).scalars().all()
        notifications = db.execute(
            select(Notification).where(Notification.delivered.is_(False))
            .order_by(Notification.created_at.desc()).limit(20)
        ).scalars().all()
        db.expunge_all()
    return {
        "alarms": list(alarms), "reminders": list(reminders), "todos": list(todos),
        "events": list(events), "notifications": list(notifications),
    }


def section_scene(kind: str, agenda: dict[str, list], zone: tzinfo, *, explicit: bool = False) -> Scene | None:
    rows = agenda[kind]
    if not rows and not explicit:
        return None
    count = len(rows)
    if kind == "alarms":
        title, noun, empty = "ALARMS", "alarm", "You have no alarms set."
        items = [f"{_clock(row.start_at, zone)} {row.title}" for row in rows[:6]]
        phrases = [f"{row.title} {_spoken_when(row.start_at, zone)}" for row in rows[:3]]
    elif kind == "reminders":
        title, noun, empty = "REMINDERS", "reminder", "You have no active reminders."
        items = [f"{_clock(row.due_at, zone)} {row.title}" for row in rows[:6]]
        phrases = [f"{row.title} {_spoken_when(row.due_at, zone)}" for row in rows[:3]]
    elif kind == "todos":
        title, noun, empty = "TASKS", "task", "You have no pending tasks."
        items = [row.title for row in rows[:6]]
        phrases = [row.title for row in rows[:4]]
    elif kind == "events":
        title, noun, empty = "CALENDAR", "upcoming event", "You have no upcoming calendar events."
        items = [f"{_clock(row.start_at, zone)} {row.title}" for row in rows[:6]]
        phrases = [f"{row.title} {_spoken_when(row.start_at, zone)}" for row in rows[:3]]
    else:
        title, noun, empty = "ALERTS", "new notification", "You have no new notifications."
        items = [row.title for row in rows[:6]]
        phrases = [row.title for row in rows[:3]]
    if not rows:
        return Scene(_PAGE_FOR[kind], title, "NOTHING SCHEDULED", ["Nothing here"], empty)
    lead = f"You have {_plural(count, noun)}."
    if kind in {"alarms", "reminders", "events"}:
        speech = f"{lead} Next up: {_join(phrases)}."
    else:
        speech = f"{lead} {_join(phrases)}."
    return Scene(_PAGE_FOR[kind], title, f"{count} {noun.upper()}{'' if count == 1 else 'S'}"[:47], items, speech)


_PAGE_FOR = {
    "alarms": "alarms", "reminders": "reminders", "todos": "todos",
    "events": "calendar", "notifications": "notifications",
}


def glance_scene(agenda: dict[str, list], zone: tzinfo) -> Scene:
    nouns = {
        "alarms": "alarm", "reminders": "reminder", "todos": "task",
        "events": "calendar event", "notifications": "notification",
    }
    active = [(kind, len(agenda[kind])) for kind in _SECTION_ORDER if agenda[kind]]
    if not active:
        return Scene("home", "TODAY", "ALL CLEAR", ["Nothing pending"], "You have nothing pending right now.")
    items = [f"{count} {nouns[kind].upper()}{'' if count == 1 else 'S'}" for kind, count in active]
    speech = "You have " + _join([_plural(count, nouns[kind]) for kind, count in active]) + "."
    return Scene("home", "TODAY", "AT A GLANCE", items, speech)


def _device_location(application, device_id: str) -> tuple[float, float, str] | None:
    with application.state.db_factory() as db:
        device = db.get(Device, device_id)
        config = dict(device.config or {}) if device else {}
    location = config.get("display_location") or {}
    if location.get("latitude") is None or location.get("longitude") is None:
        return None
    city = (config.get("location") or {}).get("city") or ""
    return float(location["latitude"]), float(location["longitude"]), city


def _device_country(application, device_id: str) -> str:
    with application.state.db_factory() as db:
        device = db.get(Device, device_id)
        config = dict(device.config or {}) if device else {}
    country = str((config.get("location") or {}).get("country") or "").upper()
    return country if country in news_service.COUNTRIES else application.state.settings.news_default_country


async def weather_scene(application, device_id: str) -> Scene:
    location = await asyncio.to_thread(_device_location, application, device_id)
    if location is None:
        return Scene(
            "weather", "WEATHER", "LOCATION NEEDED", ["Set your city in", "the setup portal"],
            "I don't know your location yet. Please set your country and city in the setup portal.",
        )
    latitude, longitude, city = location
    try:
        data = await fetch_weather_detail(latitude, longitude)
    except Exception as exc:
        logger.warning("Weather unavailable for %s: %s", device_id, exc)
        return Scene("weather", "WEATHER", "OFFLINE", ["Weather service", "unreachable"],
                     "I couldn't reach the weather service right now.")
    degrees = lambda value: f"{round(value)}" if isinstance(value, (int, float)) else "?"
    place = city or "your area"
    items = [
        f"{degrees(data['temperature_c'])}C FEELS {degrees(data['feels_like_c'])}C",
        str(data["description"]).upper(),
        f"HIGH {degrees(data['high_c'])}C LOW {degrees(data['low_c'])}C",
    ]
    if data.get("rain_chance_percent") is not None:
        items.append(f"RAIN {round(data['rain_chance_percent'])}%")
    if data.get("humidity_percent") is not None:
        items.append(f"HUMIDITY {round(data['humidity_percent'])}%")
    speech = (
        f"In {place} it's {degrees(data['temperature_c'])} degrees with {data['description']}, "
        f"feeling like {degrees(data['feels_like_c'])}. Today's high is {degrees(data['high_c'])} "
        f"and the low is {degrees(data['low_c'])}."
    )
    if data.get("rain_chance_percent"):
        speech += f" There's a {round(data['rain_chance_percent'])} percent chance of rain."
    return Scene("weather", "WEATHER", (city or "CURRENT").upper()[:28], items, speech, weather=data)


async def news_scenes(request: news_service.NewsRequest, count: int) -> list[Scene]:
    try:
        headlines = await news_service.fetch_headlines(request, count)
    except news_service.NewsError as exc:
        return [Scene("news", "NEWS", "UNAVAILABLE", ["News service", "unreachable"], f"I couldn't get the news: {exc}.")]
    if not headlines:
        return [Scene("news", "NEWS", request.label[:28], ["No stories found"],
                      "I couldn't find any news stories for that.")]
    scenes = []
    for index, headline in enumerate(headlines, start=1):
        speech = headline.title.rstrip(".") + "."
        if headline.source:
            speech += f" From {headline.source}."
        scenes.append(Scene(
            "news", "NEWS", f"{request.label} {index}/{len(headlines)}"[:47],
            news_service.wrap_lines(headline.title, 30, 8), speech,
        ))
    return scenes


def _news_intro(request: news_service.NewsRequest) -> str:
    if request.query:
        return f"Here are the latest stories about {request.query}."
    if request.genre:
        place = f" in {request.country_name}" if request.country_name else ""
        return f"Here are the latest {request.genre} headlines{place}."
    if request.country_name:
        return f"Here are the top headlines from {request.country_name}."
    return "Here are the hottest stories in the world right now."


_GO_HOME = re.compile(
    r"\b(?:go|take me|bring me|get me|back|return|switch|navigate|show me)\b.*\b(?:home|homepage|home page|home screen|main screen|main page|home bar)\b|"
    r"^(?:home|go home|show home)$"
)


def go_home_after_reply(application, websocket, device_id: str, hold: float = 5.0) -> None:
    """Returns the display to the home dashboard once a spoken answer is done, unless a game or a scene owns the screen."""
    if application.state.device_games.get(device_id) or application.state.home_tasks.get(device_id) is not None:
        return
    schedule_home(application, websocket, device_id, hold=hold)


async def show_scene(application, websocket, device_id: str, scene: Scene, zone: tzinfo | None = None) -> None:
    orchestrator = _orchestrator()
    cancel_home(application, device_id)
    payload = await asyncio.to_thread(
        build_display_payload, application.state.db_factory, scene.page, scene.weather,
        tz=zone, content={"title": scene.title, "detail": scene.detail, "items": scene.items},
    )
    await orchestrator._send_json(websocket, device_id, "display.state", payload)


def cancel_home(application, device_id: str) -> None:
    task = application.state.home_tasks.pop(device_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def schedule_home(application, websocket, device_id: str, hold: float | None = None) -> None:
    cancel_home(application, device_id)
    hold = HOLD_SECONDS if hold is None else hold

    async def return_home() -> None:
        try:
            # Speech synthesis starts after this call, so give it time to set the playback end before waiting on it.
            await asyncio.sleep(SETTLE_SECONDS)
            while (remaining := application.state.audio_echo_until.get(device_id, 0.0) - time.monotonic()) > 0:
                await asyncio.sleep(remaining)
            await asyncio.sleep(hold)
            if application.state.device_games.get(device_id):
                return
            zone = await asyncio.to_thread(_orchestrator()._device_timezone, application, device_id)
            payload = await asyncio.to_thread(
                build_display_payload, application.state.db_factory, "home", tz=zone,
            )
            await _orchestrator()._send_json(websocket, device_id, "display.state", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Could not return device %s to the home dashboard: %s", device_id, exc)
        finally:
            if application.state.home_tasks.get(device_id) is asyncio.current_task():
                application.state.home_tasks.pop(device_id, None)

    application.state.home_tasks[device_id] = asyncio.create_task(return_home())


async def _tell(application, websocket, device_id: str, scenes: list[Scene], zone: tzinfo, intro: str = "") -> None:
    orchestrator = _orchestrator()
    for index, scene in enumerate(scenes):
        await show_scene(application, websocket, device_id, scene, zone)
        speech = f"{intro} {scene.speech}".strip() if index == 0 else scene.speech
        await orchestrator._speak(application, websocket, device_id, speech)
        if index < len(scenes) - 1:
            await asyncio.sleep(SCENE_PAUSE_SECONDS)


async def play_scenes(
    application, websocket, device_id: str, scenes: list[Scene], zone: tzinfo, intro: str = "",
) -> str:
    """Show and speak every scene except the last; the last one's speech is returned for the caller to speak."""
    orchestrator = _orchestrator()
    for index, scene in enumerate(scenes[:-1]):
        await show_scene(application, websocket, device_id, scene, zone)
        speech = f"{intro} {scene.speech}".strip() if index == 0 else scene.speech
        await orchestrator._speak(application, websocket, device_id, speech)
        await asyncio.sleep(SCENE_PAUSE_SECONDS)
    last = scenes[-1]
    await show_scene(application, websocket, device_id, last, zone)
    schedule_home(application, websocket, device_id)
    return f"{intro} {last.speech}".strip() if len(scenes) == 1 else last.speech


def greeting_for(zone: tzinfo) -> str:
    hour = datetime.now(zone).hour
    if hour < 5:
        return "Hello"
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


async def briefing_scenes(
    application, device_id: str, user_id: str | None, zone: tzinfo, *, news_count: int = 3,
) -> list[Scene]:
    agenda = await asyncio.to_thread(load_agenda, application, user_id)
    scenes = [glance_scene(agenda, zone)]
    for kind in _SECTION_ORDER:
        scene = section_scene(kind, agenda, zone)
        if scene is not None:
            scenes.append(scene)
    weather, news = await asyncio.gather(
        weather_scene(application, device_id),
        news_scenes(news_service.NewsRequest(None, None, await asyncio.to_thread(
            _device_country, application, device_id), None, "WORLD"), news_count)
        if news_count else asyncio.sleep(0, result=[]),
    )
    scenes.append(weather)
    scenes.extend(news)
    return scenes


async def run_boot_briefing(application, websocket, device_id: str) -> None:
    """Power-on greeting followed by a spoken, on-screen briefing. Empty sections are skipped."""
    orchestrator = _orchestrator()
    zone = await asyncio.to_thread(orchestrator._device_timezone, application, device_id)
    greeting = f"{greeting_for(zone)}! I'm Buddy, online and connected."
    if not application.state.settings.boot_briefing:
        await orchestrator._speak(
            application, websocket, device_id, f"{greeting} Say hey buddy whenever you need me.",
        )
        return
    try:
        scenes = await briefing_scenes(application, device_id, None, zone)
    except Exception:
        logger.exception("Could not build the boot briefing for %s", device_id)
        await orchestrator._speak(application, websocket, device_id, f"{greeting} Say hey buddy whenever you need me.")
        return
    await _tell(application, websocket, device_id, scenes, zone, intro=greeting)
    await orchestrator._speak(application, websocket, device_id, "Say hey buddy whenever you need me.")
    schedule_home(application, websocket, device_id, hold=2.0)


async def run_wake_greeting(application, websocket, device_id: str) -> None:
    orchestrator = _orchestrator()
    greeting = "Hi, I'm Buddy. Good to hear you."
    tail = "I'm listening now. What would you like to know?"
    await orchestrator._speak(application, websocket, device_id, f"{greeting} {tail}")


def _parse_time(text: str, zone: tzinfo, now: datetime) -> datetime | None:
    relative = re.search(r"\bin (\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|sixty)"
                         r"(?: and a half)? (second|minute|hour|day)s?\b", text)
    if relative:
        amount = int(relative.group(1)) if relative.group(1).isdigit() else _NUMBER_WORDS[relative.group(1)]
        unit = {"second": "seconds", "minute": "minutes", "hour": "hours", "day": "days"}[relative.group(2)]
        return now + timedelta(**{unit: amount})
    clock = re.search(r"\b(?:at |for )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text) or re.search(
        r"\bat (\d{1,2})(?::(\d{2}))?\b", text)
    if not clock:
        return None
    hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
    meridiem = clock.group(3) if clock.lastindex and clock.lastindex >= 3 else None
    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    local_now = now.astimezone(zone)
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.search(r"\btomorrow\b", text):
        target += timedelta(days=1)
    elif target <= local_now:
        if meridiem is None and hour < 12 and target + timedelta(hours=12) > local_now:
            target += timedelta(hours=12)
        else:
            target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _clean(text: str) -> str:
    text = re.sub(r"\b([ap])\.?\s?m\.?\b", r"\1m", text.casefold())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:'-]", " ", text)).strip()


async def _create_item(application, device_id: str, user_id: str, lowered: str, zone: tzinfo) -> str | None:
    now = datetime.now(timezone.utc)
    zone_name = getattr(zone, "key", "UTC")
    if re.search(r"\b(set|create|add|make)\b.*\balarm\b", lowered):
        when = _parse_time(lowered, zone, now)
        if when is None:
            return "What time should I set the alarm for? Say, set an alarm for 7 AM."
        label = re.search(r"\balarm (?:called|named|for the|to) (.+?)(?: at | in |$)", lowered)
        title = (label.group(1).strip().title() if label else "Alarm")[:200]

        def save() -> None:
            with application.state.db_factory() as db:
                db.add(CalendarEvent(
                    title=title, kind="alarm", start_at=when, timezone_name=zone_name, created_by=user_id,
                ))
                db.commit()

        await asyncio.to_thread(save)
        return f"Okay, alarm set for {_spoken_when(when, zone)}."
    remind = re.search(r"\bremind me (?:to |about )(.+)$", lowered)
    if remind and not re.search(r"\d{4}-\d{2}-\d{2}t", lowered):
        body = remind.group(1)
        when = _parse_time(body, zone, now)
        if when is None:
            return "When should I remind you? Say, remind me to call mom at 5 PM."
        title = re.sub(r"\s*\b(?:at \d{1,2}(?::\d{2})?\s*(?:am|pm)?|in \w+ (?:second|minute|hour|day)s?|tomorrow)\b",
                       "", body).strip().capitalize()[:200] or "Reminder"

        def save_reminder() -> None:
            with application.state.db_factory() as db:
                db.add(Reminder(title=title, due_at=when, timezone_name=zone_name, created_by=user_id))
                db.commit()

        await asyncio.to_thread(save_reminder)
        return f"Okay, I'll remind you to {title.lower()} {_spoken_when(when, zone)}."
    task = re.search(
        r"\b(?:add|create|new)\s+(?:a\s+|an\s+)?(?:task|to-do|todo|to do)(?:\s+(?:to|called|named|for))?\s+(.+)$", lowered
    ) or re.search(r"\badd\s+(.+?)\s+to (?:my|the)\s+(?:task|to-do|todo|to do)s?(?: list)?$", lowered)
    if task:
        title = task.group(1).strip().capitalize()[:200]

        def save_task() -> None:
            with application.state.db_factory() as db:
                db.add(Todo(title=title, created_by=user_id))
                db.commit()

        await asyncio.to_thread(save_task)
        return f"Added {title} to your tasks."
    return None


_BRIEFING = re.compile(
    r"\b(brief me|briefing|what did i miss|catch me up|my day|what do i have|what have i got|"
    r"what(?:'s| is) on my plate|what(?:'s| is) up today|what(?:'s| is) happening today|good morning)\b"
)
_WEATHER = re.compile(r"\b(weather|temperature|forecast|how (?:hot|cold|warm)|is it raining|umbrella)\b")
_LISTS = (
    ("alarms", re.compile(r"\balarms?\b")),
    ("reminders", re.compile(r"\breminders?\b")),
    ("todos", re.compile(r"\b(?:to[\s-]?dos?|tasks?)\b")),
    ("events", re.compile(r"\b(?:calendar|schedule|agenda|events?|appointments?|meetings?)\b")),
    ("notifications", re.compile(r"\bnotifications?\b")),
)


async def route_dashboard_request(
    application, websocket, device_id: str, user_id: str, transcript: str,
) -> str | None:
    """Handle spoken agenda, weather, news and briefing requests by showing them on the device display."""
    if websocket is None:
        return None
    lowered = _clean(transcript)
    zone = await asyncio.to_thread(_orchestrator()._device_timezone, application, device_id)
    if _GO_HOME.search(lowered):
        schedule_home(application, websocket, device_id, hold=0.0)
        return "Going back to the home screen."
    created = await _create_item(application, device_id, user_id, lowered, zone) if _CREATE_WORDS.search(lowered) else None
    if created is not None:
        return created
    if re.search(r"\bgithub\b", lowered) or re.search(r"-?\d{1,2}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?", transcript):
        return None
    if _BRIEFING.search(lowered):
        scenes = await briefing_scenes(application, device_id, user_id, zone)
        return await play_scenes(application, websocket, device_id, scenes, zone, intro=f"{greeting_for(zone)}!")
    request = news_service.parse_news_request(
        lowered, await asyncio.to_thread(_device_country, application, device_id),
    )
    if request is not None:
        scenes = await news_scenes(request, 4)
        return await play_scenes(application, websocket, device_id, scenes, zone, intro=_news_intro(request))
    if _WEATHER.search(lowered):
        return await play_scenes(application, websocket, device_id, [await weather_scene(application, device_id)], zone)
    if _CREATE_WORDS.search(lowered):
        return None
    for kind, pattern in _LISTS:
        if pattern.search(lowered):
            agenda = await asyncio.to_thread(load_agenda, application, user_id)
            scene = section_scene(kind, agenda, zone, explicit=True)
            return await play_scenes(application, websocket, device_id, [scene], zone)
    return None
