from app.services import command_repair as cr


def test_misheard_commands_are_matched():
    assert cr.candidates("Peek-A-Game")[0][0] == "pick a game"
    assert cr.candidates("Open 2DOS")[0][0] == "show my to do list"
    assert cr.candidates("what is the capital of bangladesh") == []


def test_repair_auto_and_exact():
    assert cr.repair(None, None, "Peek-A-Game")[0] == "pick a game"
    assert cr.repair(None, None, "go home") == ("go home", None)


def test_suggestion_prompt():
    question, best = cr.suggestion_prompt("Go to the rules.")
    assert "Did you mean" in question and best


def test_play_again_and_tic_tac_toy_start_games():
    from app.services.assistant.device_games import parse_game_command

    assert parse_game_command("Play again", False) == ("list", None)
    assert parse_game_command("tic-tac-toy", False) == ("start", "tic_tac_toe")
    assert parse_game_command("Play tic tac toy", False) == ("start", "tic_tac_toe")
