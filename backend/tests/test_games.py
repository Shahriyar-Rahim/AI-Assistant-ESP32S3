import random

import pytest

from app.services.games import GameMoveError, apply_move, new_game


def test_tic_tac_toe_checks_winner_and_rejects_occupied_cell() -> None:
    state = {"board": ["X", "X", "", "O", "O", "", "", "", ""], "turn": "X", "winner": None, "scores": {"human": 0, "ai": 0}, "draw": False}
    result = apply_move("tic_tac_toe", state, {"position": 2})
    assert result["winner"] == "human"
    assert result["scores"]["human"] == 1
    with pytest.raises(GameMoveError, match="finished"):
        apply_move("tic_tac_toe", result, {"position": 8})


def test_tic_tac_toe_ai_blocks_a_forced_human_win() -> None:
    state = new_game("tic_tac_toe")
    state["board"] = ["X", "X", "", "O", "", "", "", "", ""]
    apply_move("tic_tac_toe", state, {"position": 8})
    assert state["board"][2] == "O"


def test_rps_and_number_guessing_rules() -> None:
    state = new_game("rock_paper_scissors", rng=random.Random(1))
    result = apply_move("rock_paper_scissors", state, {"choice": "rock"}, rng=random.Random(2))
    assert result["round"] == 1 and sum(result["scores"].values()) == 1
    secret_state = {"secret": 50, "guesses": 0, "last_hint": None, "won": False}
    assert apply_move("number_guessing", secret_state, {"guess": 42})["last_hint"] == "higher"
    with pytest.raises(GameMoveError):
        apply_move("number_guessing", secret_state, {"guess": 101})


def test_trivia_and_hangman_state_transitions() -> None:
    trivia = new_game("trivia", rng=random.Random(3))
    result = apply_move("trivia", trivia, {"answer": trivia["questions"][0]["answer"]})
    assert result["score"] == 1 and result["question_index"] == 1
    hangman = {"word": "hi", "guessed": [], "mistakes": 0, "max_mistakes": 6, "won": False, "lost": False}
    apply_move("hangman", hangman, {"letter": "h"})
    assert hangman["won"] is False
    assert apply_move("hangman", hangman, {"letter": "i"})["won"] is True


def test_word_association_game_has_bounded_turns() -> None:
    state = {"target": "sun", "accepted": [], "turns": 0, "finished": False, "won": False}
    assert apply_move("word_association", state, {"word": "light"})["won"] is True
    with pytest.raises(GameMoveError):
        apply_move("word_association", state, {"word": "warm"})
