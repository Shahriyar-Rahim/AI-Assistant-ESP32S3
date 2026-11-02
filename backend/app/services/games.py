from __future__ import annotations

import random
from typing import Any


GAME_NAMES = {"tic_tac_toe", "rock_paper_scissors", "number_guessing", "trivia", "hangman", "word_association"}
WORDS = ("planet", "garden", "signal", "puzzle", "window", "lantern")
TRIVIA = {
    "general": [
        {"question": "Which planet is known as the Red Planet?", "choices": ["A. Mars", "B. Venus", "C. Jupiter", "D. Mercury"], "answer": "A"},
        {"question": "How many sides does a hexagon have?", "choices": ["A. Five", "B. Six", "C. Seven", "D. Eight"], "answer": "B"},
    ],
    "programming": [
        {"question": "Which data structure uses FIFO ordering?", "choices": ["A. Stack", "B. Queue", "C. Tree", "D. Set"], "answer": "B"},
        {"question": "Which HTTP method is commonly used to retrieve a resource?", "choices": ["A. GET", "B. PATCH", "C. DELETE", "D. TRACE"], "answer": "A"},
    ],
    "web": [
        {"question": "Which language styles web pages?", "choices": ["A. SQL", "B. CSS", "C. Bash", "D. C"], "answer": "B"},
    ],
    "technology": [
        {"question": "What does RAM primarily provide?", "choices": ["A. Temporary working memory", "B. Long-term storage", "C. Network routing", "D. Power regulation"], "answer": "A"},
    ],
}
ASSOCIATIONS = {
    "sun": {"star", "light", "day", "warm"},
    "ocean": {"water", "wave", "blue", "sea"},
    "book": {"read", "story", "page", "words"},
    "music": {"song", "sound", "rhythm", "melody"},
}


class GameMoveError(ValueError):
    pass


def new_game(game: str, *, rng: random.Random | None = None, category: str = "general") -> dict[str, Any]:
    randomizer = rng or random.SystemRandom()
    if game not in GAME_NAMES:
        raise GameMoveError(f"unsupported game: {game}")
    if game == "tic_tac_toe":
        return {"board": [""] * 9, "turn": "X", "winner": None, "scores": {"human": 0, "ai": 0}, "draw": False}
    if game == "rock_paper_scissors":
        return {"round": 0, "scores": {"human": 0, "ai": 0, "draw": 0}, "last_round": None, "choices": ["rock", "paper", "scissors"]}
    if game == "number_guessing":
        return {"secret": randomizer.randint(1, 100), "guesses": 0, "last_hint": None, "won": False}
    if game == "trivia":
        if category not in TRIVIA:
            raise GameMoveError(f"unsupported trivia category: {category}")
        questions = list(TRIVIA[category])
        randomizer.shuffle(questions)
        return {"category": category, "questions": questions, "question_index": 0, "score": 0, "streak": 0, "finished": False, "last_correct": None}
    if game == "hangman":
        word = randomizer.choice(WORDS)
        return {"word": word, "guessed": [], "mistakes": 0, "max_mistakes": 6, "won": False, "lost": False}
    return {"target": randomizer.choice(tuple(ASSOCIATIONS)), "accepted": [], "turns": 0, "finished": False, "won": False}


def apply_move(game: str, state: dict[str, Any], move: dict[str, Any], *, rng: random.Random | None = None) -> dict[str, Any]:
    randomizer = rng or random.SystemRandom()
    if state.get("finished") or state.get("won") or state.get("lost") or state.get("winner") or state.get("draw"):
        raise GameMoveError("game is already finished")
    if game == "tic_tac_toe":
        position = move.get("position")
        if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position < 9:
            raise GameMoveError("position must be an integer from 0 to 8")
        board = list(state["board"])
        if board[position]:
            raise GameMoveError("cell is already occupied")
        board[position] = "X"
        winner = _winner(board)
        if winner == "X":
            state["scores"]["human"] += 1
            state.update(board=board, winner="human", turn=None)
            return state
        if all(board):
            state.update(board=board, draw=True, turn=None)
            return state
        ai_position = _best_move(board)
        board[ai_position] = "O"
        winner = _winner(board)
        if winner == "O":
            state["scores"]["ai"] += 1
            state.update(board=board, winner="ai", turn=None)
        elif all(board):
            state.update(board=board, draw=True, turn=None)
        else:
            state.update(board=board, turn="X")
        return state
    if game == "rock_paper_scissors":
        choice = move.get("choice")
        if choice not in ("rock", "paper", "scissors"):
            raise GameMoveError("choice must be rock, paper, or scissors")
        ai_choice = randomizer.choice(("rock", "paper", "scissors"))
        outcome = "draw" if choice == ai_choice else (
            "human" if (choice, ai_choice) in {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")} else "ai"
        )
        state["scores"][outcome] += 1
        state["round"] += 1
        state["last_round"] = {"human": choice, "ai": ai_choice, "winner": outcome}
        return state
    if game == "number_guessing":
        guess = move.get("guess")
        if not isinstance(guess, int) or isinstance(guess, bool) or not 1 <= guess <= 100:
            raise GameMoveError("guess must be an integer from 1 to 100")
        state["guesses"] += 1
        state["last_hint"] = "higher" if guess < state["secret"] else "lower" if guess > state["secret"] else "correct"
        state["won"] = guess == state["secret"]
        return state
    if game == "trivia":
        answer = move.get("answer")
        if not isinstance(answer, str) or answer.upper() not in {"A", "B", "C", "D"}:
            raise GameMoveError("answer must be A, B, C, or D")
        question = state["questions"][state["question_index"]]
        correct = answer.upper() == question["answer"]
        state["last_correct"] = correct
        state["score"] += int(correct)
        state["streak"] = state["streak"] + 1 if correct else 0
        state["question_index"] += 1
        state["finished"] = state["question_index"] >= len(state["questions"])
        return state
    if game == "hangman":
        letter = move.get("letter")
        if not isinstance(letter, str) or len(letter) != 1 or not letter.isalpha():
            raise GameMoveError("letter must be a single alphabetic character")
        letter = letter.lower()
        if letter in state["guessed"]:
            raise GameMoveError("letter was already guessed")
        state["guessed"].append(letter)
        if letter not in state["word"]:
            state["mistakes"] += 1
        state["won"] = all(character in state["guessed"] for character in state["word"])
        state["lost"] = state["mistakes"] >= state["max_mistakes"] and not state["won"]
        return state
    if game == "word_association":
        response = move.get("word")
        if not isinstance(response, str) or not 1 <= len(response.strip()) <= 40:
            raise GameMoveError("word must contain 1 to 40 characters")
        normalized = response.strip().casefold()
        if normalized in state["accepted"]:
            raise GameMoveError("word was already used")
        state["accepted"].append(normalized)
        state["turns"] += 1
        state["won"] = normalized in ASSOCIATIONS[state["target"]]
        state["finished"] = state["won"] or state["turns"] >= 5
        return state
    raise GameMoveError(f"unsupported game: {game}")


def _winner(board: list[str]) -> str | None:
    lines = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def _best_move(board: list[str]) -> int:
    best_score = -2
    best_position = -1
    for position, cell in enumerate(board):
        if cell:
            continue
        board[position] = "O"
        score = _minimax(board, False)
        board[position] = ""
        if score > best_score:
            best_score, best_position = score, position
    return best_position


def _minimax(board: list[str], ai_turn: bool) -> int:
    result = _winner(board)
    if result == "O":
        return 1
    if result == "X":
        return -1
    if all(board):
        return 0
    values = []
    for position, cell in enumerate(board):
        if cell:
            continue
        board[position] = "O" if ai_turn else "X"
        values.append(_minimax(board, not ai_turn))
        board[position] = ""
    return max(values) if ai_turn else min(values)
