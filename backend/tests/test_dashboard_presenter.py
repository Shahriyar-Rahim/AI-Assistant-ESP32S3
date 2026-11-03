import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import CalendarEvent, Device, Reminder, Todo
from app.services import news as news_service
from app.services.assistant import orchestrator, presenter


def make_app(device_config=None):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app import models  # noqa: F401
    from app.database.session import Base

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    if device_config is not None:
        with factory() as db:
            db.add(Device(id="dev", name="dev", credential_hash="x", config=device_config))
            db.commit()
    settings = SimpleNamespace(boot_briefing=True, wake_weather=True, news_default_country="US")
    return SimpleNamespace(state=SimpleNamespace(
        db_factory=factory, settings=settings, home_tasks={}, audio_echo_until={}, device_games={},
    ))


@pytest.fixture
def recorder(monkeypatch):
    log = {"sent": [], "spoken": []}

    async def fake_send(websocket, device_id, message_type, payload):
        log["sent"].append((message_type, payload))

    async def fake_speak(application, websocket, device_id, text):
        log["spoken"].append(text)

    monkeypatch.setattr(orchestrator, "_send_json", fake_send)
    monkeypatch.setattr(orchestrator, "_speak", fake_speak)
    monkeypatch.setattr(presenter, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(presenter, "HOLD_SECONDS", 0.01)
    return log


def seed(app):
    now = datetime.now(timezone.utc)
    with app.state.db_factory() as db:
        db.add(CalendarEvent(title="Wake up", kind="alarm", start_at=now + timedelta(hours=3), timezone_name="UTC"))
        db.add(Reminder(title="Call mom", due_at=now + timedelta(hours=2), timezone_name="UTC"))
        db.add(Todo(title="Buy milk"))
        db.commit()


def run(coro):
    return asyncio.run(coro)


def test_alarm_request_shows_page_and_returns_home(recorder):
    app = make_app()
    seed(app)

    async def scenario():
        reply = await presenter.route_dashboard_request(app, object(), "dev", "u1", "what alarms do I have")
        await asyncio.sleep(0.2)
        return reply

    reply = run(scenario())
    assert "Wake up" in reply and "1 alarm" in reply
    pages = [payload["page"] for kind, payload in recorder["sent"] if kind == "display.state"]
    assert pages == ["alarms", "home"]
    assert recorder["sent"][0][1]["items"][0].endswith("Wake up")


def test_empty_list_is_spoken_but_briefing_skips_empty_sections(recorder):
    app = make_app()
    with app.state.db_factory() as db:
        db.add(Todo(title="Buy milk"))
        db.commit()
    assert run(presenter.route_dashboard_request(app, object(), "dev", "u1", "show my reminders")) == (
        "You have no reminders." if False else "You have no active reminders."
    )

    async def build():
        agenda = await asyncio.to_thread(presenter.load_agenda, app, None)
        zone = timezone.utc
        return [presenter.glance_scene(agenda, zone)] + [
            s for s in (presenter.section_scene(k, agenda, zone) for k in presenter._SECTION_ORDER) if s
        ]

    scenes = run(build())
    assert [scene.page for scene in scenes] == ["home", "todos"]
    assert scenes[0].speech == "You have 1 task."


def test_nothing_pending_glance():
    agenda = {kind: [] for kind in presenter._SECTION_ORDER}
    scene = presenter.glance_scene(agenda, timezone.utc)
    assert scene.detail == "ALL CLEAR" and "nothing pending" in scene.speech


def test_news_request_shows_each_headline(recorder, monkeypatch):
    app = make_app()

    async def fake_fetch(request, limit=5):
        assert request.genre == "technology" and request.country_code == "IN"
        return [news_service.Headline("Chip makers expand factories across the region today", "Wire", ""),
                news_service.Headline("New phone launched", "Verge", "")]

    monkeypatch.setattr(news_service, "fetch_headlines", fake_fetch)
    reply = run(presenter.route_dashboard_request(app, object(), "dev", "u1", "give me technology news from India"))
    news_pages = [p for k, p in recorder["sent"] if p["page"] == "news"]
    assert len(news_pages) == 2 and news_pages[0]["detail"] == "TECHNOLOGY 1/2"
    assert all(len(line) <= 30 for page in news_pages for line in page["items"])
    assert recorder["spoken"][0].startswith("Here are the latest technology headlines in India.")
    assert reply == "New phone launched. From Verge."


def test_weather_uses_saved_location(recorder, monkeypatch):
    app = make_app({"display_location": {"latitude": 23.8, "longitude": 90.4}, "location": {"city": "Dhaka"}})

    async def fake_weather(lat, lon):
        assert (lat, lon) == (23.8, 90.4)
        return {"temperature_c": 31.2, "feels_like_c": 36, "humidity_percent": 70, "wind_kmh": 8,
                "description": "partly cloudy", "high_c": 33, "low_c": 26, "rain_chance_percent": 20}

    monkeypatch.setattr(presenter, "fetch_weather_detail", fake_weather)
    reply = run(presenter.route_dashboard_request(app, object(), "dev", "u1", "how's the weather today"))
    assert "Dhaka" in reply and "31 degrees" in reply and "20 percent" in reply
    payload = recorder["sent"][0][1]
    assert payload["page"] == "weather" and payload["detail"] == "DHAKA"


def test_weather_without_location_explains(recorder):
    app = make_app()
    reply = run(presenter.route_dashboard_request(app, object(), "dev", "u1", "what's the weather"))
    assert "setup portal" in reply


def test_voice_creation_of_alarm_task_and_reminder(recorder):
    app = make_app()
    assert "alarm set" in run(presenter.route_dashboard_request(app, object(), "dev", "u1", "set an alarm for 7 a.m."))
    assert "Added Buy bread" in run(presenter.route_dashboard_request(app, object(), "dev", "u1", "add a task to buy bread"))
    assert "remind you to call mom" in run(
        presenter.route_dashboard_request(app, object(), "dev", "u1", "remind me to call mom in 10 minutes"))
    with app.state.db_factory() as db:
        assert db.query(CalendarEvent).filter_by(kind="alarm").count() == 1
        assert db.query(Todo).one().title == "Buy bread"
        assert db.query(Reminder).one().title == "Call mom"


def test_news_parsing():
    parse = news_service.parse_news_request
    assert parse("what time is it") is None
    assert parse("tell me the news").label == "WORLD"
    request = parse("news about bitcoin")
    assert request.query == "bitcoin"
    assert parse("sports news from Germany").country_code == "DE"
    assert news_service.wrap_lines("a " * 40, 10, 3)[-1].endswith("...")


def test_feed_parser_rejects_entities_and_dedupes():
    with pytest.raises(news_service.NewsError):
        news_service.parse_feed(b'<!DOCTYPE x [<!ENTITY a "b">]><rss/>', 3)
    feed = (b"<rss><channel><link>https://news.google.com/?gl=US</link>"
            b"<item><title>Hello world - Src</title><source>Src</source></item>"
            b"<item><title>Hello world - Src</title><source>Src</source></item></channel></rss>")
    headlines, _ = news_service.parse_feed(feed, 5)
    assert [h.title for h in headlines] == ["Hello world"]
