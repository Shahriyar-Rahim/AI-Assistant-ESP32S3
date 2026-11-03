from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, update

from app.models import (
    AuditLog,
    CalendarEvent,
    Device,
    GameSession,
    IntegrationAccount,
    Reminder,
)
from app.services.games import GAME_NAMES, GameMoveError, apply_move, new_game
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

    page: Literal[
        "home", "weather", "reminders", "notes", "todos", "alarms",
        "games", "calendar", "notifications",
    ]
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
    display = build_display_payload(context.application.state.db_factory, "calendar")
    if not result:
        return {"speech": "You have no upcoming calendar events.", "events": [], "display": display}
    summary = "; ".join(
        f"{item['title']} at {datetime.fromisoformat(item['start_at']).astimezone(timezone.utc).strftime('%A %H:%M UTC')}"
        for item in result[:5]
    )
    return {"speech": f"Your upcoming events are: {summary}.", "events": result, "display": display}


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
    return {
        "speech": _game_prompt(values.game, state),
        "session_id": session_id,
        "game": values.game,
        "display": _game_display(values.game, state),
        "sound_effect": "success",
    }


def _game_prompt(game: str, state: dict[str, Any]) -> str:
    if state.get("finished") or state.get("won") or state.get("lost") or state.get("winner") or state.get("draw"):
        if game == "trivia":
            return f"Trivia is complete. You got {state['score']} correct."
        if game == "hangman":
            return "You won." if state.get("won") else f"Game over. The word was {state['word']}."
        if game == "number_guessing":
            return f"Correct! The number was {state['secret']}."
        if game == "word_association":
            return "Great association!" if state.get("won") else "That round is complete. Let's start another."
        if game == "tic_tac_toe":
            return "You won!" if state.get("winner") == "human" else (
                "It's a draw." if state.get("draw") else "I won this round."
            )
    if game == "tic_tac_toe":
        return "Tic tac toe is ready. Choose a square from one to nine, starting at the top left."
    if game == "rock_paper_scissors":
        return "Rock paper scissors is ready. Say rock, paper, or scissors."
    if game == "number_guessing":
        return "I'm thinking of a number from one to one hundred. What is your guess?"
    if game == "trivia":
        question = state["questions"][state["question_index"]]
        return f"{question['question']} " + " ".join(question["choices"])
    if game == "hangman":
        return "Hangman is ready. Guess a letter."
    return "Word association is ready. Say a word related to the hidden topic."


def _game_display(game: str, state: dict[str, Any]) -> dict[str, Any]:
    if game == "tic_tac_toe":
        board = state["board"]
        items = [" ".join(mark or str(index + 1) for index, mark in enumerate(board[row:row + 3], row))
                 for row in (0, 3, 6)]
        detail = f"YOUR TURN {state['turn'] or '-'}"
    elif game == "rock_paper_scissors":
        last_round = state.get("last_round")
        items = [f"ROUND {state['round']}", f"YOU {state['scores']['human']}  AI {state['scores']['ai']}"]
        if last_round:
            items.append(f"{last_round['human']} / {last_round['ai']}")
        detail = "SAY ROCK PAPER SCISSORS"
    elif game == "number_guessing":
        items = [f"ATTEMPT {state['guesses']}"]
        if state.get("last_hint"):
            items.append("CORRECT" if state["last_hint"] == "correct" else f"GO {state['last_hint'].upper()}")
        detail = "GUESS 1 TO 100"
    elif game == "trivia":
        if state.get("finished"):
            items = [f"SCORE {state['score']}", "GAME COMPLETE"]
            detail = "TRIVIA FINISHED"
        else:
            question = state["questions"][state["question_index"]]
            choices = question["choices"]
            items = [
                question["question"],
                " ".join(choices[:2]),
                " ".join(choices[2:]),
            ]
            detail = f"QUESTION {state['question_index'] + 1}"
    elif game == "hangman":
        word = state["word"]
        items = [" ".join(letter if letter in state["guessed"] else "_" for letter in word)]
        items.append(f"MISTAKES {state['mistakes']}/{state['max_mistakes']}")
        detail = "GUESS A LETTER"
    else:
        items = [f"TURN {state['turns']}/5"]
        detail = "SAY A RELATED WORD"
    return {
        "state": "dashboard",
        "page": "games",
        "title": game.replace("_", " ").upper(),
        "detail": detail,
        "items": [str(item)[:96] for item in items[:4]],
        "counts": {},
        "emoji": "🎮",
    }


def _game_move_speech(
    game: str,
    previous: dict[str, Any],
    state: dict[str, Any],
    move: dict[str, Any],
    finished: bool,
) -> str:
    if game == "tic_tac_toe":
        position = move["position"]
        if finished:
            return _game_prompt(game, state)
        ai_position = next(
            (index for index, mark in enumerate(state["board"])
             if mark == "O" and previous["board"][index] != "O"),
            None,
        )
        return f"You marked square {position + 1}." + (
            f" I marked square {ai_position + 1}." if ai_position is not None else ""
        ) + " Your turn."
    if game == "rock_paper_scissors":
        result = state["last_round"]
        outcome = {
            "human": "You win this round.",
            "ai": "I win this round.",
            "draw": "That round is a draw.",
        }[result["winner"]]
        return f"You chose {result['human']}; I chose {result['ai']}. {outcome} Say rock, paper, or scissors."
    if game == "number_guessing":
        if state["won"]:
            return _game_prompt(game, state)
        return f"Try {state['last_hint']}. What is your next guess?"
    if game == "trivia":
        result = "That's correct." if state["last_correct"] else "That's not correct."
        return f"{result} {_game_prompt(game, state)}"
    if game == "hangman":
        if finished:
            return _game_prompt(game, state)
        visible = " ".join(
            character if character in state["guessed"] else "_"
            for character in state["word"]
        )
        found = "is in the word" if move["letter"] in state["word"] else "is not in the word"
        return (
            f"{move['letter']} {found}. {visible}. "
            f"{state['mistakes']} of {state['max_mistakes']} misses. Guess another letter."
        )
    if finished:
        return _game_prompt(game, state)
    return f"Not that one. Try another related word. Turn {state['turns']} of 5."


def _spoken_number(text: str) -> int | None:
    words = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
        "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
        "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90,
    }
    tens = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"}
    tokens = re.findall(r"[a-z]+|\d+", text.replace("-", " "))
    for index, token in enumerate(tokens):
        if token.isdigit():
            return int(token)
        if token == "hundred" and index > 0 and tokens[index - 1] in words:
            return min(words[tokens[index - 1]] * 100, 100)
        if token not in words:
            continue
        value = words[token]
        if index + 1 < len(tokens) and tokens[index + 1] == "hundred":
            return min(value * 100, 100)
        if token in tens and index + 1 < len(tokens) and tokens[index + 1] in words:
            value += words[tokens[index + 1]]
        if value:
            return value
    return None


def _voice_move(game: str, transcript: str) -> dict[str, Any] | None:
    text = transcript.strip().casefold()
    if game == "tic_tac_toe":
        squares = {
            "top left": 0, "top middle": 1, "top center": 1, "top right": 2,
            "middle left": 3, "center": 4, "middle": 4, "middle right": 5,
            "bottom left": 6, "bottom middle": 7, "bottom center": 7, "bottom right": 8,
        }
        for location, position in squares.items():
            if location in text:
                return {"position": position}
        match = re.search(r"\b(?:square|cell|number)?\s*(?:number\s*)?([1-9])\b", text)
        if match:
            return {"position": int(match.group(1)) - 1}
        number_words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9,
        }
        for word, position in number_words.items():
            if re.search(rf"\b{word}\b", text):
                return {"position": position - 1}
    elif game == "rock_paper_scissors":
        match = re.search(r"\b(rock|paper|scissors?)\b", text)
        if match:
            choice = match.group(1)
            return {"choice": "scissors" if choice.startswith("scissor") else choice}
    elif game == "number_guessing":
        match = re.search(r"\b(?:guess\s+)?(\d{1,3})\b", text)
        if match:
            return {"guess": int(match.group(1))}
        guess = _spoken_number(text)
        if guess is not None:
            return {"guess": guess}
    elif game == "trivia":
        match = re.search(r"\b(?:option\s+|answer\s+)?([a-d])\b", text)
        if match:
            return {"answer": match.group(1).upper()}
        for index, word in enumerate(("first", "second", "third", "fourth")):
            if re.search(rf"\b{word}\b", text):
                return {"answer": "ABCD"[index]}
    elif game == "hangman":
        match = re.search(r"\b(?:letter|guess)\s+([a-z])\b", text)
        if match:
            return {"letter": match.group(1)}
        if re.fullmatch(r"[a-z]", text):
            return {"letter": text}
    else:
        word = re.sub(r"^(?:i (?:say|choose|pick)|the word is)\s+", "", text).strip(" .,!?:;")
        if word:
            return {"word": word}
    return None


def apply_voice_game_move(context: ActionContext, transcript: str) -> dict[str, Any] | None:
    with context.application.state.db_factory() as db:
        session = db.execute(
            select(GameSession)
            .where(GameSession.user_id == context.user_id, GameSession.status == "active")
            .order_by(GameSession.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if session is None:
            return None
        game = session.game
        if re.search(r"\b(stop|quit|exit|end|cancel)\b.*\b(game|playing)\b|\bstop playing\b", transcript.casefold()):
            session.status = "abandoned"
            db.commit()
            return {"speech": "Okay, I ended the game.", "game": game, "display": None, "sound_effect": None}
        updated = session.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated is not None and datetime.now(timezone.utc) - updated > timedelta(minutes=10):
            return None
        move = _voice_move(game, transcript)
        if move is None:
            return {
                "speech": _game_prompt(game, session.state),
                "game": game,
                "display": _game_display(game, session.state),
                "sound_effect": None,
            }
        previous = copy.deepcopy(session.state)
        state = copy.deepcopy(session.state)
        try:
            state = apply_move(game, state, move)
        except GameMoveError as exc:
            return {
                "speech": f"{exc}. {_game_prompt(game, session.state)}",
                "game": game,
                "display": _game_display(game, session.state),
                "sound_effect": "error",
            }
        finished = any(state.get(flag) for flag in ("finished", "won", "lost", "winner", "draw"))
        result = db.execute(
            update(GameSession)
            .where(
                GameSession.id == session.id,
                GameSession.user_id == context.user_id,
                GameSession.version == session.version,
                GameSession.status == "active",
            )
            .values(
                state=state,
                version=GameSession.version + 1,
                status="completed" if finished else "active",
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount != 1:
            raise IntentError("The game changed while I was processing your move. Please try again.")
        db.commit()
        session_id = session.id
    _record(context, "game.voice_move", {"session_id": session_id, "game": game, "move": move})
    return {
        "speech": _game_move_speech(game, previous, state, move, finished),
        "game": game,
        "display": _game_display(game, state),
        "sound_effect": "success" if finished else "move",
    }


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
    "tik tak toe": "tic_tac_toe",
    "tick tack toe": "tic_tac_toe",
    "tic-tac-toe": "tic_tac_toe",
    "tictactoe": "tic_tac_toe",
    "tic tac toes": "tic_tac_toe",
    "noughts and crosses": "tic_tac_toe",
    "rock paper scissors": "rock_paper_scissors",
    "number guessing": "number_guessing",
    "trivia": "trivia",
    "hangman": "hangman",
    "word association": "word_association",
}
ISO_DATETIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})"


def deterministic_intent(text: str) -> tuple[str, dict[str, Any]] | None:
    normalized = text.strip()
    lower = re.sub(r"[^\w\s-]", " ", normalized.casefold())
    lower = re.sub(r"\s+", " ", lower)
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
        (r"\b(?:show|open|view|display)\b.*\bcalendar\b", "calendar"),
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
        if re.search(rf"\b(?:play|start|begin|open)\s+(?:a\s+|the\s+|some\s+)?(?:game\s+of\s+)?{re.escape(spoken_name)}\b", lower):
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
