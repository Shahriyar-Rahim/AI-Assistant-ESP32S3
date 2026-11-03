from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.database.session import Base
from app.models import GameResult
from app.services import game_history

ROOT = Path(__file__).resolve().parents[2]


def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_offline_results_are_ingested_once():
    db = factory()
    records = [{"id": 1, "ts": 1_800_000_000, "game": "trivia", "outcome": "win", "difficulty": "hard",
                "score": 4, "max_score": 5, "rounds": 5, "duration_seconds": 60},
               {"id": 2, "ts": 1_800_000_100, "game": "bogus", "outcome": "win"}]
    assert game_history.ingest_offline(db, "esp32", records) == 1
    assert game_history.ingest_offline(db, "esp32", records) == 0
    with db() as session:
        rows = list(session.execute(select(GameResult)).scalars())
    assert len(rows) == 1 and rows[0].source == "device-offline" and rows[0].difficulty == "hard"


def test_firmware_keeps_compact_log_and_uploads_it():
    games = (ROOT / "firmware/main/device_games.cpp").read_text()
    network = (ROOT / "firmware/main/device_network.cpp").read_text()
    assert "__attribute__((packed)) LogRecord" in games and "log_append" in games
    assert "send_game_history();" in network and '"game.history"' in network
    assert "game.history" in (ROOT / "backend/app/schemas/protocol.py").read_text()
    assert "game.history" in (ROOT / "shared/protocol/control.schema.json").read_text()
