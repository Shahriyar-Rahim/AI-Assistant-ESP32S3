"""Repairs speech-recognition mishearings of short device commands and learns the user's own corrections.

Whisper often turns "pick a game" into "peek a game" or "open to-dos" into "open 2DOS". Each phrase below is a
spoken variant that routes to one canonical command the existing routers already understand. Matching is done on
the written text and on a vowel-free sound skeleton so near-homophones score highly.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.services import local_brain

AUTO_THRESHOLD = 0.84
SUGGEST_THRESHOLD = 0.58
MAX_WORDS = 7

COMMANDS: dict[str, str] = {
    "which games do we have": "which games do we have",
    "what games do you have": "which games do we have",
    "list the games": "which games do we have",
    "pick a game": "pick a game",
    "pick any game": "pick a game",
    "play a game": "pick a game",
    "lets play a game": "pick a game",
    "play tic tac toe": "play tic tac toe",
    "play rock paper scissors": "play rock paper scissors",
    "play number guessing": "play number guessing",
    "play trivia": "play trivia",
    "play hangman": "play hangman",
    "play word association": "play word association",
    "go to home": "go to home",
    "go to the home page": "go to home",
    "go home": "go to home",
    "take me home": "go to home",
    "show my to do list": "show my to do list",
    "open my to dos": "show my to do list",
    "open to dos": "show my to do list",
    "show my tasks": "show my to do list",
    "show my reminders": "show my reminders",
    "open reminders": "show my reminders",
    "show my alarms": "show my alarms",
    "open alarms": "show my alarms",
    "show my calendar": "show my calendar",
    "show my notifications": "show my notifications",
    "go to weather": "what is the weather",
    "what is the weather": "what is the weather",
    "show the weather": "what is the weather",
    "show me the news": "show me the news",
    "tell me the news": "show me the news",
    "brief me": "brief me",
    "give me my briefing": "brief me",
    "what time is it": "what time is it",
    "what is the date today": "what is the date today",
    "erase yourself": "erase yourself",
    "open the setup portal": "open the setup portal",
    "change the wifi": "change the wifi",
    "what have you learned about me": "what have you learned about me",
}

_DIGITS = {"2": "to", "4": "for", "1": "one", "8": "ate"}
_PLURAL_DO = re.compile(r"\bdo'?s\b")


def _words(text: str) -> str:
    lowered = text.casefold().replace("'", "")
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", lowered)
    lowered = " ".join(_DIGITS.get(word, word) for word in lowered.split())
    return re.sub(r"\bto dos\b", "to dos", lowered).strip()


def _skeleton(text: str) -> str:
    sound = re.sub(r"ph", "f", text)
    sound = re.sub(r"ck|c(?![eiy])|q", "k", sound)
    sound = re.sub(r"[aeiouy\s]", "", sound)
    return re.sub(r"(.)\1+", r"\1", sound)


def _score(heard: str, phrase: str) -> float:
    written = SequenceMatcher(None, heard, phrase).ratio()
    sound = SequenceMatcher(None, _skeleton(heard), _skeleton(phrase)).ratio()
    return max(written, sound * 0.93)


def candidates(transcript: str, limit: int = 3) -> list[tuple[str, float]]:
    heard = _words(transcript)
    if not heard or len(heard.split()) > MAX_WORDS:
        return []
    best: dict[str, float] = {}
    for phrase, canonical in COMMANDS.items():
        score = _score(heard, _words(phrase))
        if score > best.get(canonical, 0.0):
            best[canonical] = score
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    return [item for item in ranked[:limit] if item[1] >= SUGGEST_THRESHOLD]


def repair(db_factory, user_id: str | None, transcript: str) -> tuple[str, str | None]:
    """Returns (text to route, reason). The text is unchanged when nothing needs repairing."""
    heard = _words(transcript)
    if not heard or len(heard.split()) > MAX_WORDS:
        return transcript, None
    if heard in {_words(phrase) for phrase in COMMANDS}:
        return transcript, None
    learned = local_brain.find(db_factory, user_id, heard, ("alias",), 0.9)
    if learned is not None:
        local_brain.mark_used(db_factory, learned[0].id)
        return str(learned[0].response["command"]), "learned"
    ranked = candidates(transcript, 1)
    if ranked and ranked[0][1] >= AUTO_THRESHOLD:
        return ranked[0][0], "sound-alike"
    return transcript, None


def learn_alias(db_factory, user_id: str | None, heard: str, command: str) -> bool:
    cleaned = _words(heard)
    if not cleaned or len(cleaned.split()) > MAX_WORDS or cleaned == _words(command):
        return False
    return local_brain._upsert(db_factory, user_id, "alias", cleaned, {"command": command}, "voice")


def suggestion_prompt(transcript: str) -> tuple[str, str] | None:
    """Returns (spoken question, best command) when the transcript is close to a known command."""
    ranked = candidates(transcript, 2)
    if not ranked:
        return None
    options = " or ".join(item[0] for item in ranked)
    return f"I heard {transcript.strip(' .?!')}. Did you mean {options}? Say yes to use the first one.", ranked[0][0]
