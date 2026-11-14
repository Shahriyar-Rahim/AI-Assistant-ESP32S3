"""Spoken camera commands: faces, selfies, recording, objects, paper text and home organisation."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from app.services import local_brain
from app.services.vision import store
from app.services.vision.monitor import VisionError

_ME = r"(?:my ?self|me|my face|my own face)"
_ENROLL_OWNER = re.compile(rf"\b(?:remember|learn|enrol+|register|save|scan|recogni[sz]e)\s+{_ME}\b|\bthis is me\b")
_ENROLL_PERSON = re.compile(
    r"\b(?:remember|learn|save|enrol+)\s+(?:this|that)\s+(?:person|guy|man|woman|girl|boy|face)(?:\s+(?:as|is|named|called))?\s+(.+)$"
    r"|\b(?:remember|learn)\s+(?:the\s+)?face\s+of\s+(.+)$|\b(?:remember|learn)\s+(.+?)'?s\s+face$"
    r"|\bthis (?:person )?is\s+(?!me\b|my\b|a\b|the\b)([a-z][a-z ]{1,30})$"
)
_NAME_VISITOR = re.compile(r"\b(?:the\s+)?(?:last\s+)?(?:visitor|stranger|person|guy|man|woman)\s+(?:was|is)\s+([a-z][a-z ]{1,30})$")
_FORGET_FACE = re.compile(r"\bforget\s+(?:the\s+face\s+of\s+(.+)|(.+?)'?s\s+face)$")
_WHO_HERE = re.compile(r"\bwho\s+(?:is\s+this|am\s+i\s+looking\s+at|do\s+you\s+see|is\s+(?:in\s+(?:the\s+)?(?:room|front)|here))\b")
_WHO_KNOWN = re.compile(r"\bwho\s+(?:do\s+you\s+know|have\s+you\s+(?:seen|remembered))\b|\bwhich\s+(?:faces|people)\b")
_PHOTO = re.compile(r"\b(?:take|snap|capture|click|get)\b.*\b(?:selfie|photo|picture|pic)\b")
_REC_STOP = re.compile(r"\b(?:stop|end|finish)\b.*\b(?:recording|video)\b")
_REC_START = re.compile(r"\b(?:start|begin|turn\s+on)\b.*\b(?:recording|video)\b|\brecord\b.*\b(?:video|room|me)\b")
_WATCH_ON = re.compile(r"\b(?:start|turn\s+on|enable|activate)\b.*\b(?:security|camera|watching|guard)\b|\bwatch\s+(?:my|the)\s+room\b")
_WATCH_OFF = re.compile(r"\b(?:stop|turn\s+off|disable|deactivate)\b.*\b(?:security|camera|watching|guard)\b")
_OBJECT_AS = re.compile(r"\bremember\s+(?:this|that)\s+(?:object\s+|thing\s+|item\s+)?as\s+(?:my\s+|the\s+|a\s+)?(.+)$")
_OBJECT_MINE = re.compile(r"\bthis\s+is\s+my\s+(?!face\b)(.+)$|\bremember\s+my\s+(?!face\b)([a-z][a-z0-9 ]{1,30})$")
_WHAT_OBJECT = re.compile(
    r"^(?:what(?:'s|\s+is)\s+this(?:\s+(?:object|thing|item|device|gadget|one))?|what\s+am\s+i\s+holding(?:\s+up)?|"
    r"do\s+you\s+recogni[sz]e\s+this(?:\s+(?:object|thing))?|identify\s+this(?:\s+object)?|"
    r"do\s+you\s+(?:remember|know)\s+(?:this|that|what\s+this\s+is)(?:\s+(?:object|thing|item|device|phone|gadget))?)$"
)
_READ = re.compile(
    r"\b(?:read|scan|copy)\s+(?:this|that|the|my)\s+(?:paper|page|note|document|letter|writing|text|sign|label)\b"
    r"|\bwhat\s+does\s+(?:this|it|the\s+paper)\s+say\b|\bwhat(?:'s|\s+is)\s+written\b|\bremember\s+what(?:'s|\s+is)\s+written\b"
)
_HOME = re.compile(
    r"\bremember\s+(?:that\s+)?(?:my\s+|the\s+|our\s+)?(.+?)\s+(?:is|are|goes|go|lives|live|stays|stay)\s+"
    r"((?:in|on|under|inside|at|behind|beside|next\s+to|near|above|on\s+top\s+of|in\s+front\s+of)\s+.+)$"
)
_HOME_ORG = re.compile(r"\bremember\s+(?:my\s+|the\s+)?(?:home|house|room|home's)\s+organi[sz]ation\s*(?:that|is|as|:)?\s*(.+)$")
_WHERE = re.compile(r"^where\s+(?:is|are|did\s+i\s+(?:put|leave|keep|place))\s+(?:my\s+|the\s+|our\s+)?(.+)$")
_RECALL_TEXT = re.compile(r"\bwhat\s+did\s+(?:the|that|my)\s+(?:paper|note|page|letter|document)\s+say(?:\s+about\s+(.+))?$")
_REPORT = re.compile(
    r"\bwho\s+(?:came|entered|was)\s+(?:in|into|here)\b|\bdid\s+(?:anyone|anybody|someone)\s+(?:come|enter)\b|"
    r"\b(?:security|room)\s+(?:report|log|summary|activity)\b|\bany\s+visitors\b"
)
_DURATION = re.compile(r"(\d+)\s*(second|sec|minute|min)s?")
_FILLER = {"the", "a", "an", "my", "of", "is", "are", "in", "on", "it", "this", "that"}


def _clean_name(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip(" .?!'")).title()


async def route(application, device_id: str, user_id: str | None, transcript: str) -> str | None:
    monitor = getattr(application.state, "vision", None)
    if monitor is None:
        return None
    text = re.sub(r"[^a-z0-9' ]", " ", transcript.casefold().replace("’", "'"))
    text = " ".join(text.split())
    if not text:
        return None
    try:
        return await _dispatch(application, monitor, user_id, text)
    except VisionError as error:
        return str(error)


async def _dispatch(application, monitor, user_id: str | None, text: str) -> str | None:
    factory = application.state.db_factory
    uid = monitor.uid(user_id)

    if _ENROLL_OWNER.search(text):
        name = await asyncio.to_thread(local_brain.user_name, factory, user_id) or "Owner"
        monitor.start_voice_enrollment(name)
        return "I'll collect face samples for 60 seconds. Please stay alone in view and turn your head slowly."

    if match := _FORGET_FACE.search(text):
        person = await asyncio.to_thread(store.find_identity, factory, _clean_name(match.group(1) or match.group(2)))
        if person is None:
            return "I don't know anyone by that name."
        await asyncio.to_thread(store.delete_identity, factory, person.id)
        await monitor.reload()
        return f"I've forgotten {person.name}'s face."

    if match := _NAME_VISITOR.search(text):
        events = await asyncio.to_thread(store.recent_events, factory, 30)
        target = next((e["identity_id"] for e in events if e["identity_id"] and monitor.people.get(e["identity_id"], {}).get("role") == "stranger"), None)
        if target:
            name = _clean_name(match.group(1))
            await asyncio.to_thread(store.set_identity, factory, target, name, "known")
            await monitor.reload()
            return f"Okay, I'll remember that person as {name}."

    if match := _ENROLL_PERSON.search(text):
        name = _clean_name(next(group for group in match.groups() if group))
        if name.casefold() in {"me", "myself"}:
            return None
        await monitor.enroll(name, "known")
        return f"Okay, I'll recognise {name} from now on."

    if _WHO_KNOWN.search(text):
        names = [f"{person['name']}, {person['role']}" for person in monitor.people.values() if person["role"] != "stranger"]
        strangers = sum(1 for person in monitor.people.values() if person["role"] == "stranger")
        parts = ("I know " + "; ".join(names) + ".") if names else "I don't know anyone by name yet."
        return f"{parts} I've also seen {strangers} unnamed visitor{'s' if strangers != 1 else ''}."

    if _WHO_HERE.search(text):
        names = await monitor.who_is_here()
        if not names:
            return "I can't see anyone's face right now."
        return "I can see " + ", ".join(names) + "."

    if _REC_STOP.search(text):
        return "Recording stopped and saved on your PC." if monitor.stop_recording() else "I'm not recording."
    if _REC_START.search(text):
        found = _DURATION.search(text)
        seconds = int(found.group(1)) * (60 if found.group(2).startswith("min") else 1) if found else None
        monitor.start_recording(seconds)
        return f"Recording video for {found.group(0)}." if found else "Recording video. Say stop recording when you're done."

    if _PHOTO.search(text):
        selfie = "selfie" in text
        path = await monitor.take_photo("selfie" if selfie else "photo", send=bool(re.search(r"\b(?:send|telegram)\b", text)))
        return f"Done. Saved the {'selfie' if selfie else 'photo'} on your PC."

    if _WATCH_OFF.search(text):
        await monitor.set_enabled(False)
        return "Camera security is off."
    if _WATCH_ON.search(text):
        await monitor.set_enabled(True)
        extra = "" if monitor.telegram.configured else " Telegram is not configured yet, so alerts will only be logged."
        return "Camera security is on. I'm watching the room." + extra

    if match := _HOME_ORG.search(text):
        await asyncio.to_thread(store.add_memory, factory, uid, "home", "home organisation", match.group(1))
        return "Okay, I'll remember how your home is organised."
    if match := _HOME.search(text):
        item = match.group(1).strip()
        if item.split()[0] in {"to", "that", "what"} or len(item.split()) > 6:
            return None
        await asyncio.to_thread(store.add_memory, factory, uid, "home", item, f"{item} is {match.group(2)}")
        return f"Okay, your {item} is {match.group(2)}."

    if match := _OBJECT_AS.search(text) or _OBJECT_MINE.search(text):
        label = _clean_name(next(group for group in match.groups() if group)).lower()
        if re.search(r"\b(?:is|are)\b", label):
            return None
        await monitor.remember_object(label)
        return f"Okay, I'll recognise your {label}."

    if _WHAT_OBJECT.search(text):
        label, count = await monitor.identify_object()
        if label:
            return f"That looks like your {label}."
        return "I don't recognise that. Say, remember this as, and a name, to teach me."

    if _READ.search(text):
        words = await monitor.read_paper()
        spoken = " ".join(words.split())
        return "I read: " + (spoken[:350] + ("…" if len(spoken) > 350 else "")) + " I've saved it for later."

    if match := _WHERE.search(text):
        words = {word for word in match.group(1).split() if word not in _FILLER}
        found = await asyncio.to_thread(store.search_memories, factory, uid, words, ("home", "object"))
        if found:
            hit = found[0]
            return hit["content"] and f"I remember that {hit['content']}." or f"I know your {hit['label']}, but not where it is."
        return None

    if match := _RECALL_TEXT.search(text):
        topic = {word for word in (match.group(1) or "").split() if word not in _FILLER}
        items = await asyncio.to_thread(store.memories, factory, uid, "text")
        if topic:
            items = await asyncio.to_thread(store.search_memories, factory, uid, topic, ("text",))
        if not items:
            return "I haven't read any paper about that yet."
        return "The paper said: " + " ".join(items[0]["content"].split())[:350]

    if _REPORT.search(text):
        events = await asyncio.to_thread(store.recent_events, factory, 100, timedelta(hours=24))
        alerts = [event for event in events if event["kind"] == "intruder_alert"]
        with_you = [event for event in events if event["kind"] == "visitor_with_owner"]
        if not alerts and not with_you:
            return "Nobody else came into your room in the last day."
        return f"In the last day, {len(alerts)} visit{'s' if len(alerts) != 1 else ''} while you were away, and {len(with_you)} while you were here."
    return None
