"""Routes game voice commands to the ESP32-S3, which runs every game on its own processor."""

import asyncio
import re
from difflib import SequenceMatcher
from uuid import uuid4

from app.services import game_history
from app.services.assistant.actions import GAME_ALIASES

GAME_COMMAND_TIMEOUT_SECONDS = 3.0

_LIST = re.compile(
    r"\b(?:which|what)\s+(?:games?|game do)\b|\b(?:list|show)\s+(?:the\s+|me\s+)?(?:all\s+)?games\b|"
    r"\bwhat\s+(?:can|could)\s+we\s+play\b|\bavailable\s+games\b|\bgames\s+(?:do\s+)?(?:we|you)\s+have\b|"
    r"\bgames\s+(?:are\s+)?available\b"
)
_RANDOM = re.compile(
    r"\b(?:pick|choose|select)\s+(?:a|any|one|me a)?\s*(?:random\s+)?game\b|\bplay\s+(?:a|any|some)\s+game\b|"
    r"\bsurprise\s+me\b|\b(?:play|start|let'?s\s+play|lets\s+play|pick|choose)\s+(?:a\s+|some\s+|the\s+)?(?:fun\s+)?games?\b|\bi\s+(?:want|wanna)\s+(?:to\s+)?play\b\s*(?:a\s+)?(?:game)?\s*$|\blet'?s\s+play(?:\s+a\s+game)?\s*$|\brandom\s+game\b|\bplay\s+something\b"
)
_HOME = re.compile(
    r"\b(?:go|take me|bring me|get me|back|return|switch|navigate)\b.*\b(?:home|homepage|home page|home screen|main screen|main page|home bar)\b|"
    r"^(?:home|go home|show home|show me home)\s*$"
)
_START_VERB = r"(?:play|start|begin|open|launch|let'?s\s+play|lets\s+play)"
_EXTRA_NAMES = {
    "guess the number": "number_guessing",
    "guessing game": "number_guessing",
    "quiz": "trivia",
    "word game": "word_association",
    "rps": "rock_paper_scissors",
    "tic tac toe": "tic_tac_toe",
    "tic-tac-toe": "tic_tac_toe",
    "tick tack toe": "tic_tac_toe",
    "tik tak toe": "tic_tac_toe",
    "tik tac toe": "tic_tac_toe",
    "tick tac toe": "tic_tac_toe",
    "tic tac": "tic_tac_toe",
    "tic tac toy": "tic_tac_toe",
    "tic-tac-toy": "tic_tac_toe",
    "tic tac toes": "tic_tac_toe",
    "tick tack toy": "tic_tac_toe",
    "tik tak toy": "tic_tac_toe",
    "tic tac tow": "tic_tac_toe",
    "noughts and crosses": "tic_tac_toe",
    "rock paper scissors": "rock_paper_scissors",
    "dice": "dice",
    "die": "dice",
    "roulette": "roulette",
    "decision symbols": "symbols",
    "symbols": "symbols",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", text.casefold())).strip()


_PHONETIC = str.maketrans({"c": "k", "q": "k", "z": "s", "y": "i"})
_FUZZY_VERB = re.compile(rf"^(?:please\s+)?{_START_VERB}\s+(?:a\s+|the\s+|some\s+)?(?:game\s+of\s+)?(.+)$")


def _skeleton(text: str) -> str:
    text = re.sub(r"\b(?:game|games)\b", " ", text).translate(_PHONETIC)
    text = re.sub(r"[^a-z]", "", text)
    return re.sub(r"(.)\1+", r"\1", text)


def _fuzzy_game(requested: str) -> str | None:
    """Matches a mis-transcribed game name ("numbar gazing") by sound so games never need the cloud."""
    wanted = _skeleton(requested)
    if len(wanted) < 4:
        return None
    best, best_score = None, 0.0
    for spoken, game in {**GAME_ALIASES, **_EXTRA_NAMES}.items():
        score = SequenceMatcher(None, wanted, _skeleton(spoken)).ratio()
        if score > best_score:
            best, best_score = game, score
    return best if best_score >= 0.72 else None


def parse_game_command(text: str, active: bool) -> tuple[str, str | None] | None:
    """Returns (action, game) for list/start requests, or None when the text is ordinary input."""
    lowered = _normalize(text)
    if active and _HOME.search(lowered):
        return "stop", None
    if _LIST.search(lowered):
        return "list", None
    if not active and re.fullmatch(r"(?:please\s+)?(?:play|start)\s+(?:again|another(?:\s+game)?|a\s+gain)|another\s+game", lowered):
        return "list", None
    if not active:
        if re.fullmatch(r"(?:please\s+)?roll\s+(?:(?:a|the)\s+)?(?:die|dice)(?:\s+please)?", lowered):
            return "start", "dice"
        if re.fullmatch(r"(?:please\s+)?spin\s+(?:(?:the|a)\s+)?(?:roulette|roulette wheel)(?:\s+please)?", lowered):
            return "start", "roulette"
        if re.fullmatch(r"(?:please\s+)?(?:choose|pick|draw)\s+(?:(?:a|the)\s+)?(?:decision\s+)?symbol(?:\s+please)?", lowered):
            return "start", "symbols"
    names = {**GAME_ALIASES, **_EXTRA_NAMES}
    for spoken, game in names.items():
        if re.search(rf"\b{_START_VERB}\s+(?:a\s+|the\s+|some\s+)?(?:game\s+of\s+)?{re.escape(spoken)}\b", lowered):
            return "start", game
    if _RANDOM.search(lowered):
        return "start", "random"
    fuzzy = _FUZZY_VERB.match(lowered)
    if fuzzy:
        game = _fuzzy_game(fuzzy.group(1))
        if game:
            return "start", game
    if not active:
        for spoken, game in names.items():
            if lowered in {spoken, f"{spoken} game", f"the {spoken} game"}:
                return "start", game
    return None


def is_physical_device(application, device_id: str) -> bool:
    entry = application.state.devices.get(device_id)
    return entry is not None and not entry[1]


async def send_game_command(application, websocket, device_id: str, action: str, game: str | None = None, text: str = "",
                            difficulty: str = "normal") -> dict | None:
    request_id = uuid4().hex
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    application.state.game_waiters[(device_id, request_id)] = waiter
    from datetime import datetime, timezone

    try:
        await websocket.send_json({
            "protocol_version": 1,
            "message_id": request_id,
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "game.command",
            "payload": {"request_id": request_id, "action": action, "game": game or "", "text": text[:150], "difficulty": difficulty},
        })
        return await asyncio.wait_for(waiter, GAME_COMMAND_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, RuntimeError):
        return None
    finally:
        application.state.game_waiters.pop((device_id, request_id), None)


async def route_device_game(application, websocket, device_id: str, transcript: str, user_id: str | None = None) -> str | None:
    """Returns the device's spoken reply, or None when the transcript is not a game request."""
    if websocket is None or not is_physical_device(application, device_id):
        return None
    active = bool(application.state.device_games.get(device_id))
    parsed = parse_game_command(transcript, active)
    if parsed is None and not active:
        return None
    if parsed is not None:
        action, game = parsed
        difficulty = "normal"
        if action == "start" and game == "random":
            game = await asyncio.to_thread(game_history.recommend_game, application.state.db_factory, user_id)
        if action == "start" and game:
            difficulty = await asyncio.to_thread(game_history.suggested_difficulty, application.state.db_factory, user_id, game)
        result = await send_game_command(application, websocket, device_id, action, game, difficulty=difficulty)
    else:
        result = await send_game_command(application, websocket, device_id, "input", text=transcript)
    if not result:
        if parsed is not None and parsed[0] in {"list", "start"}:
            return "My game engine did not respond. Please try again in a moment."
        return None
    if not result.get("handled"):
        return None
    application.state.device_games[device_id] = bool(result.get("active"))
    from app.services.assistant.presenter import cancel_home, schedule_home

    if parsed is not None and parsed[0] == "list":
        # The user usually answers with a game name, so keep listening without the wake word.
        application.state.relisten[device_id] = True
        schedule_home(application, websocket, device_id, hold=25.0)
    elif result.get("active"):
        cancel_home(application, device_id)
    else:
        schedule_home(application, websocket, device_id, hold=6.0)
    if result.get("outcome"):
        await asyncio.to_thread(
            game_history.record_result, application.state.db_factory,
            user_id=user_id, device_id=device_id, result=result,
            difficulty=str(result.get("difficulty") or "normal"),
        )
    if result.get("active"):
        application.state.relisten[device_id] = True
    speech = str(result.get("speech") or "").strip()
    return speech[:1000] or None
