import asyncio
from types import SimpleNamespace

from app.services.assistant import device_games as dg


def test_parse_commands():
    assert dg.parse_game_command("which games do we have", False) == ("list", None)
    assert dg.parse_game_command("pick a game", False) == ("start", "random")
    assert dg.parse_game_command("let's play tik tak toe", False) == ("start", "tic_tac_toe")
    assert dg.parse_game_command("play hangman", False) == ("start", "hangman")
    assert dg.parse_game_command("trivia", False) == ("start", "trivia")
    assert dg.parse_game_command("what time is it", False) is None


class FakeSocket:
    def __init__(self, app, reply):
        self.app, self.reply, self.sent = app, reply, []

    async def send_json(self, message):
        self.sent.append(message)
        request_id = message["payload"]["request_id"]
        waiter = self.app.state.game_waiters[("dev", request_id)]
        waiter.set_result({**self.reply, "request_id": request_id})


def make_app(simulated=False):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database.session import Base
    from app import models  # noqa: F401

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return SimpleNamespace(state=SimpleNamespace(
        devices={"dev": (0, simulated)}, game_waiters={}, device_games={}, relisten={}, home_tasks={}, pending_suggestions={}, audio_echo_until={},
        db_factory=sessionmaker(engine, expire_on_commit=False),
    ))


def test_route_sends_command_and_relistens():
    app = make_app()
    socket = FakeSocket(app, {"handled": True, "speech": "Tic tac toe.", "active": True})
    speech = asyncio.run(dg.route_device_game(app, socket, "dev", "play tic tac toe"))
    assert speech == "Tic tac toe."
    assert socket.sent[0]["type"] == "game.command"
    assert socket.sent[0]["payload"]["action"] == "start"
    assert app.state.device_games["dev"] is True
    assert app.state.relisten["dev"] is True


def test_finished_game_result_is_persisted():
    from app.services import game_history

    app = make_app()
    reply = {"handled": True, "speech": "You win", "active": True, "game": "hangman", "outcome": "win",
             "score": 4, "max_score": 6, "rounds": 2, "duration_seconds": 40}
    asyncio.run(dg.route_device_game(app, FakeSocket(app, reply), "dev", "play hangman", None))
    assert game_history.user_stats(app.state.db_factory, None)["games"]["hangman"]["wins"] == 1


def test_active_game_input_and_fallthrough():
    app = make_app()
    app.state.device_games["dev"] = True
    socket = FakeSocket(app, {"handled": False, "speech": "", "active": True})
    assert asyncio.run(dg.route_device_game(app, socket, "dev", "what time is it")) is None
    assert socket.sent[0]["payload"]["action"] == "input"


def test_simulated_device_uses_backend_games():
    app = make_app(simulated=True)
    assert asyncio.run(dg.route_device_game(app, FakeSocket(app, {}), "dev", "play hangman")) is None


def test_result_is_recorded_and_stats_learn(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database.session import Base
    from app import models  # noqa: F401
    from app.services import game_history

    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for outcome in ["win", "win", "win", "loss"]:
        assert game_history.record_result(
            factory, user_id=None, device_id="dev",
            result={"game": "tic_tac_toe", "outcome": outcome, "score": 1, "max_score": 1, "rounds": 4, "duration_seconds": 30},
        )
    assert game_history.record_result(factory, user_id=None, device_id="dev", result={"game": "bogus", "outcome": "win"}) is None
    stats = game_history.user_stats(factory, None)
    assert stats["games"]["tic_tac_toe"]["wins"] == 3
    assert stats["games"]["tic_tac_toe"]["win_rate"] == 0.75
    assert stats["patterns"]["favorite"] == "tic_tac_toe"
    assert stats["patterns"]["suggested_difficulty"]["tic_tac_toe"] == "hard"
    assert "tic tac toe" in game_history.spoken_summary(factory, None, "tic_tac_toe")
    assert game_history.recommend_game(factory, None) != "tic_tac_toe"
    assert "Game history" in game_history.prompt_context(factory, None)
    assert game_history.clear_history(factory, None) == 4


def test_game_stats_question_detection():
    from app.services.assistant.orchestrator import _is_game_stats_question

    assert _is_game_stats_question("how am i doing at games")
    assert _is_game_stats_question("what is my win rate")
    assert not _is_game_stats_question("how am i doing today")


def test_location_sanitized():
    from app.services.device_location import sanitize_location

    assert sanitize_location({"country": "bd", "timezone": "Asia/Dhaka", "city": " Dhaka "}) == {
        "country": "BD", "timezone": "Asia/Dhaka", "city": "Dhaka"}
    assert sanitize_location({"country": "", "timezone": "Not/AZone", "city": ""}) is None


def test_game_list_keeps_listening_and_schedules_home():
    async def run():
        app = make_app()
        socket = FakeSocket(app, {"handled": True, "speech": "Six games.", "active": False})
        await dg.route_device_game(app, socket, "dev", "which games do we have")
        return app.state.relisten.get("dev"), "dev" in app.state.home_tasks

    assert asyncio.run(run()) == (True, True)


def test_go_home_stops_an_active_game():
    assert dg.parse_game_command("go to the home page", True) == ("stop", None)
    assert dg.parse_game_command("go to the home page", False) is None
