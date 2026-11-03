import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app import models  # noqa: F401
from app.models import CalendarEvent, Reminder
from app.services import local_brain, schedule_sync
from app.services.assistant import setup_commands
from app.services.assistant.device_games import parse_game_command


def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_learns_cloud_reply_and_answers_similar_question_locally():
    db = factory()
    assert local_brain.learn_reply(db, None, "why is the sky blue", "Because of Rayleigh scattering.")
    hit = local_brain.find(db, None, "why is the sky blue?", ("reply",), local_brain.LOCAL_FIRST_THRESHOLD)
    assert hit is not None and hit[0].response["text"].startswith("Because")
    assert local_brain.find(db, None, "why is grass green", ("reply",), local_brain.LOCAL_FIRST_THRESHOLD) is None


def test_offline_threshold_accepts_paraphrase_but_numbers_must_match():
    db = factory()
    local_brain.learn_reply(db, None, "how many legs does a spider have", "Eight.")
    assert local_brain.find(db, None, "how many legs do spiders have", ("reply",), local_brain.OFFLINE_THRESHOLD)
    local_brain.learn_reply(db, None, "what is 12 times 3", "36")
    assert local_brain.find(db, None, "what is 12 times 4", ("reply",), local_brain.OFFLINE_THRESHOLD) is None


def test_volatile_and_secret_prompts_are_never_learned():
    db = factory()
    for prompt in ("what is the weather today", "what time is it", "my password is hunter2", "play tic tac toe"):
        assert not local_brain.learn_reply(db, None, prompt, "anything")
    assert local_brain.stats(db, None)["entries"] == 0


def test_user_facts_are_remembered_and_forgotten():
    db = factory()
    assert "Alex" in local_brain.fact_command(db, None, "My name is alex")
    assert "Alex" in local_brain.fact_command(db, None, "what is my name")
    local_brain.fact_command(db, None, "remember that I like jazz")
    assert "jazz" in local_brain.fact_command(db, None, "what do you know about me")
    assert "forgotten" in local_brain.fact_command(db, None, "forget everything you know")
    assert local_brain.stats(db, None)["entries"] == 0


def test_small_talk_and_usage_counter():
    assert "Hello" in local_brain.small_talk("hey buddy hello")
    assert local_brain.small_talk("explain quantum chromodynamics") is None
    db = factory()
    local_brain.learn_reply(db, None, "capital of france", "Paris.")
    entry = local_brain.find(db, None, "capital of france", ("reply",), 0.8)[0]
    local_brain.mark_used(db, entry.id)
    assert local_brain.stats(db, None)["total_local_answers"] == 1


def test_game_phrases_reach_the_device():
    for phrase in ("let's play tik tak toe", "play a game", "lets play games", "pick a game"):
        assert parse_game_command(phrase, False) is not None


def test_setup_command_patterns():
    assert setup_commands._PORTAL.search("open your ap portal")
    assert setup_commands._PORTAL.search("change the wifi")
    assert setup_commands._ERASE.search("erase yourself")
    assert not setup_commands._ERASE.search("what is the time")


def _application(ws):
    state = SimpleNamespace(
        devices={"dev": ("now", False)}, pending_setup={}, relisten={}, audio_echo_until={},
    )
    return SimpleNamespace(state=state)


def test_erase_requires_spoken_confirmation(monkeypatch):
    sent = []
    monkeypatch.setattr(setup_commands, "SETTLE_SECONDS", 0)

    class Socket:
        async def send_json(self, message):
            sent.append(message)

    async def scenario():
        app, ws = _application(None), Socket()
        reply = await setup_commands.route_setup_command(app, ws, "dev", "erase yourself")
        assert "yes" in reply.casefold() and not sent and app.state.relisten["dev"]
        reply = await setup_commands.route_setup_command(app, ws, "dev", "no cancel")
        assert "keep" in reply.casefold()
        await setup_commands.route_setup_command(app, ws, "dev", "erase yourself")
        reply = await setup_commands.route_setup_command(app, ws, "dev", "yes do it")
        assert "erasing" in reply.casefold()
        await asyncio.sleep(0.8)
        assert [m["payload"]["action"] for m in sent] == ["erase_setup"]
        await setup_commands.route_setup_command(app, ws, "dev", "open your setup portal")
        await asyncio.sleep(0.8)
        assert sent[-1]["type"] == "device.setup" and sent[-1]["payload"]["action"] == "open_portal"

    asyncio.run(scenario())


def test_schedule_sync_lists_future_items_and_hash_changes():
    db = factory()
    now = datetime.now(timezone.utc)
    with db() as session:
        session.add(Reminder(title="Call mom", due_at=now + timedelta(hours=1), timezone_name="UTC"))
        session.add(Reminder(title="Old", due_at=now - timedelta(hours=1), timezone_name="UTC"))
        session.add(CalendarEvent(title="Wake up", kind="alarm", start_at=now + timedelta(hours=2), timezone_name="UTC"))
        session.add(CalendarEvent(title="Daily", kind="event", start_at=now + timedelta(minutes=5), timezone_name="UTC",
                                  recurrence_rule="FREQ=DAILY"))
        session.commit()
    items = schedule_sync.build_schedule(db, now)
    titles = [item["title"] for item in items]
    assert "Call mom" in titles and "Old" not in titles and titles.count("Daily") == 3
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)
    assert all(len(item["id"]) == 16 for item in items)
    assert next(item for item in items if item["title"] == "Wake up")["kind"] == "alarm"
    before = schedule_sync.schedule_hash(items)
    with db() as session:
        session.add(Reminder(title="New", due_at=now + timedelta(hours=3), timezone_name="UTC"))
        session.commit()
    assert schedule_sync.schedule_hash(schedule_sync.build_schedule(db, now)) != before
    assert schedule_sync.sync_message("dev", items)["type"] == "schedule.sync"
