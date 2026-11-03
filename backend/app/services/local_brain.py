"""A small, dependency-free brain that learns from the user and from cloud AI answers.

It runs on the backend host (not on the ESP32-S3): it stores what the cloud assistant answered and which
actions it chose, remembers facts the user tells it, and answers similar requests locally later. It is a
retrieval model (token + character-trigram similarity), not a generative model, so it can only repeat or
adapt what it has already seen.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.models import LearnedResponse

MAX_ENTRIES_PER_USER = 2000
MAX_PROMPT_CHARACTERS = 300
MAX_REPLY_CHARACTERS = 1000
REPLY_TTL_DAYS = 90
LOCAL_FIRST_THRESHOLD = 0.86
OFFLINE_THRESHOLD = 0.62

_STOP = frozenset(
    "a an the is are was were am be been to of and or for in on at it its this that these those do does did "
    "can could would should will you your yours i me my we our please hey buddy tell give about what whats "
    "how who which there with just so then also".split()
)
# Answers to these change over time or depend on state, so they are never replayed from memory.
_VOLATILE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|now|current|currently|latest|recent|recently|this (?:week|month|year|morning|evening)|"
    r"weather|forecast|news|headlines?|time|date|price|score|scores|stock|stocks|won|winner|result|results|"
    r"remind|alarm|timer|calendar|reminders?|tasks?|todos?|play|game|games|brief|briefing)\b"
)
_SECRET = re.compile(r"\b(password|passcode|pin|token|api key|secret|credit card|ssn)\b")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.casefold())).strip()


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    return sorted({_stem(word) for word in normalize(text).split() if word not in _STOP})


def _trigrams(text: str) -> set[str]:
    padded = f"  {normalize(text)} "
    return {padded[index:index + 3] for index in range(max(1, len(padded) - 2))}


def similarity(left: str, right: str, left_tokens: list[str] | None = None, right_tokens: list[str] | None = None) -> float:
    a = set(left_tokens if left_tokens is not None else tokenize(left))
    b = set(right_tokens if right_tokens is not None else tokenize(right))
    if not a or not b:
        return 0.0
    token_score = len(a & b) / len(a | b)
    ta, tb = _trigrams(left), _trigrams(right)
    char_score = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    numbers_left, numbers_right = set(re.findall(r"\d+", left)), set(re.findall(r"\d+", right))
    if numbers_left != numbers_right:
        return min(token_score, char_score) * 0.5
    return 0.65 * token_score + 0.35 * char_score


def is_learnable(prompt: str) -> bool:
    lowered = normalize(prompt)
    words = lowered.split()
    if not 2 <= len(words) <= 40 or len(lowered) > MAX_PROMPT_CHARACTERS:
        return False
    return not _VOLATILE.search(lowered) and not _SECRET.search(lowered)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(db_factory, user_id: str | None, kind: str, prompt: str, response: dict[str, Any], source: str) -> bool:
    key = normalize(prompt)
    tokens = tokenize(prompt)
    if not tokens:
        return False
    with db_factory() as db:
        existing = db.execute(
            select(LearnedResponse).where(
                LearnedResponse.user_id == user_id, LearnedResponse.kind == kind,
                LearnedResponse.prompt == key[:MAX_PROMPT_CHARACTERS],
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.response = response
            existing.source = source
            existing.last_used_at = _now()
        else:
            db.add(LearnedResponse(
                user_id=user_id, kind=kind, prompt=key[:MAX_PROMPT_CHARACTERS], tokens=tokens,
                response=response, source=source,
            ))
            count = db.scalar(select(func.count()).select_from(LearnedResponse).where(LearnedResponse.user_id == user_id)) or 0
            if count >= MAX_ENTRIES_PER_USER:
                stale = db.execute(
                    select(LearnedResponse.id).where(LearnedResponse.user_id == user_id, LearnedResponse.kind != "fact")
                    .order_by(LearnedResponse.uses, LearnedResponse.last_used_at).limit(count - MAX_ENTRIES_PER_USER + 1)
                ).scalars().all()
                if stale:
                    db.execute(delete(LearnedResponse).where(LearnedResponse.id.in_(stale)))
        db.commit()
    return True


def learn_reply(db_factory, user_id: str | None, prompt: str, reply: str, source: str = "cloud") -> bool:
    reply = reply.strip()
    if not reply or not is_learnable(prompt):
        return False
    return _upsert(db_factory, user_id, "reply", prompt, {"text": reply[:MAX_REPLY_CHARACTERS]}, source)


def learn_action(db_factory, user_id: str | None, prompt: str, name: str, arguments: dict[str, Any]) -> bool:
    if not is_learnable(prompt):
        return False
    return _upsert(db_factory, user_id, "action", prompt, {"name": name, "arguments": arguments}, "cloud")


def find(db_factory, user_id: str | None, prompt: str, kinds: tuple[str, ...], threshold: float) -> tuple[LearnedResponse, float] | None:
    if not is_learnable(prompt):
        return None
    tokens = tokenize(prompt)
    cutoff = _now() - timedelta(days=REPLY_TTL_DAYS)
    best: tuple[LearnedResponse, float] | None = None
    with db_factory() as db:
        rows = db.execute(
            select(LearnedResponse).where(LearnedResponse.user_id == user_id, LearnedResponse.kind.in_(kinds))
        ).scalars().all()
        for row in rows:
            last_used = row.last_used_at if row.last_used_at.tzinfo else row.last_used_at.replace(tzinfo=timezone.utc)
            if row.kind == "reply" and last_used < cutoff:
                continue
            score = similarity(prompt, row.prompt, tokens, list(row.tokens))
            if score >= threshold and (best is None or score > best[1]):
                best = (row, score)
        if best is not None:
            db.expunge(best[0])
    return best


def mark_used(db_factory, entry_id: str) -> None:
    with db_factory() as db:
        row = db.get(LearnedResponse, entry_id)
        if row is not None:
            row.uses += 1
            row.last_used_at = _now()
            db.commit()


_NAME = re.compile(r"\bmy name is ([a-z][a-z' -]{0,40})$|\bcall me ([a-z][a-z' -]{0,40})$")
_REMEMBER = re.compile(r"^(?:please )?(?:remember|note) that (.{3,200})$")
_ASK_NAME = re.compile(r"\b(?:what(?:'s| is) my name|who am i|do you know my name)\b")
_ASK_MEMORY = re.compile(r"\bwhat do you (?:remember|know) about me\b|\bwhat have you learned(?: about me)?\b")
_FORGET = re.compile(r"\b(?:forget (?:everything|all)(?: you (?:learned|know|remember))?|clear your memory)\b")


def fact_command(db_factory, user_id: str | None, transcript: str) -> str | None:
    lowered = normalize(transcript)
    name = _NAME.search(lowered)
    if name:
        value = (name.group(1) or name.group(2)).strip().title()
        _upsert(db_factory, user_id, "fact", "name", {"text": value}, "user")
        return f"Nice to meet you, {value}. I'll remember that."
    remember = _REMEMBER.match(lowered)
    if remember:
        fact = remember.group(1).strip()
        _upsert(db_factory, user_id, "fact", f"note {fact}"[:MAX_PROMPT_CHARACTERS], {"text": fact}, "user")
        return "Okay, I'll remember that."
    if _ASK_NAME.search(lowered):
        with db_factory() as db:
            row = db.execute(select(LearnedResponse).where(
                LearnedResponse.user_id == user_id, LearnedResponse.kind == "fact", LearnedResponse.prompt == "name",
            )).scalar_one_or_none()
        return f"Your name is {row.response['text']}." if row else "I don't know your name yet. Say, my name is, and then your name."
    if _ASK_MEMORY.search(lowered):
        with db_factory() as db:
            facts = db.execute(select(LearnedResponse).where(
                LearnedResponse.user_id == user_id, LearnedResponse.kind == "fact",
            ).order_by(LearnedResponse.created_at.desc()).limit(5)).scalars().all()
            learned = db.scalar(select(func.count()).select_from(LearnedResponse).where(
                LearnedResponse.user_id == user_id, LearnedResponse.kind != "fact")) or 0
        parts = [f"{row.response['text']}" if row.prompt != "name" else f"your name is {row.response['text']}" for row in facts]
        if not parts and not learned:
            return "I haven't learned anything about you yet. Tell me things and I'll remember them."
        text = "I remember that " + "; ".join(parts) + "." if parts else ""
        return f"{text} I've also learned {learned} things from our conversations.".strip()
    if _FORGET.search(lowered):
        with db_factory() as db:
            db.execute(delete(LearnedResponse).where(LearnedResponse.user_id == user_id))
            db.commit()
        return "Okay, I've forgotten everything I learned."
    return None


_JOKES = (
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my Wi-Fi a joke, but it didn't get the connection.",
    "Why did the robot go on vacation? It needed to recharge its batteries.",
)


def small_talk(transcript: str, user_name: str | None = None) -> str | None:
    lowered = normalize(transcript)
    who = f", {user_name}" if user_name else ""
    if re.search(r"^(?:hi|hello|hey|yo|good (?:morning|afternoon|evening))\b", lowered) and len(lowered.split()) <= 4:
        return f"Hello{who}! What can I do for you?"
    if re.search(r"\bhow are you\b|\bhow(?:'s| is) it going\b", lowered):
        return "I'm doing great and happy to chat. How can I help?"
    if re.search(r"\b(?:thanks|thank you|cheers)\b", lowered):
        return "You're welcome!"
    if re.search(r"\bwho are you\b|\bwhat(?:'s| is) your name\b", lowered):
        return "I'm Buddy, your assistant running on an ESP32-S3."
    if re.search(r"\bwhat can you do\b|\bhelp me\b|\bwhat are your (?:features|skills)\b", lowered):
        return ("I can play games, set alarms, reminders and tasks, show your agenda, tell you the weather and news, "
                "and answer questions when my cloud brain is available.")
    if re.search(r"\b(?:good ?bye|bye|see you|good night)\b", lowered):
        return f"Goodbye{who}! Say hey buddy whenever you need me."
    if re.search(r"\btell me a joke\b|\bmake me laugh\b", lowered):
        return random.choice(_JOKES)
    return None


def user_name(db_factory, user_id: str | None) -> str | None:
    with db_factory() as db:
        row = db.execute(select(LearnedResponse).where(
            LearnedResponse.user_id == user_id, LearnedResponse.kind == "fact", LearnedResponse.prompt == "name",
        )).scalar_one_or_none()
    return row.response["text"] if row else None


def stats(db_factory, user_id: str | None) -> dict[str, Any]:
    with db_factory() as db:
        rows = db.execute(select(LearnedResponse).where(LearnedResponse.user_id == user_id)).scalars().all()
    kinds = Counter(row.kind for row in rows)
    top = sorted((row for row in rows if row.kind != "fact"), key=lambda row: row.uses, reverse=True)[:10]
    return {
        "entries": len(rows),
        "kinds": dict(kinds),
        "total_local_answers": sum(row.uses for row in rows),
        "facts": [{"id": row.id, "text": row.response.get("text", "")} for row in rows if row.kind == "fact"],
        "top": [
            {"id": row.id, "kind": row.kind, "prompt": row.prompt, "uses": row.uses, "source": row.source,
             "answer": (row.response.get("text") or row.response.get("name") or "")[:160]}
            for row in top
        ],
        "recent": [
            {"id": row.id, "kind": row.kind, "prompt": row.prompt, "uses": row.uses, "source": row.source,
             "answer": (row.response.get("text") or row.response.get("name") or "")[:160]}
            for row in sorted(rows, key=lambda row: row.created_at, reverse=True)[:15] if row.kind != "fact"
        ],
    }


def clear(db_factory, user_id: str | None) -> int:
    with db_factory() as db:
        result = db.execute(delete(LearnedResponse).where(LearnedResponse.user_id == user_id))
        db.commit()
    return result.rowcount or 0


def delete_entry(db_factory, user_id: str | None, entry_id: str) -> bool:
    with db_factory() as db:
        result = db.execute(delete(LearnedResponse).where(
            LearnedResponse.id == entry_id, LearnedResponse.user_id == user_id))
        db.commit()
    return bool(result.rowcount)
