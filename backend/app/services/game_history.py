"""Persistence, statistics and learned play patterns for device games."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from app.models import GameResult

GAME_LABELS = {
    "tic_tac_toe": "tic tac toe",
    "rock_paper_scissors": "rock paper scissors",
    "number_guessing": "number guessing",
    "trivia": "trivia",
    "hangman": "hangman",
    "word_association": "word association",
}
OUTCOMES = {"win", "loss", "draw", "abandoned"}


def record_result(db_factory, *, user_id: str | None, device_id: str, result: dict[str, Any],
                  difficulty: str = "normal", source: str = "device", simulated: bool = False) -> str | None:
    game = str(result.get("game") or "")
    outcome = str(result.get("outcome") or "")
    if game not in GAME_LABELS or outcome not in OUTCOMES:
        return None

    def number(key: str, ceiling: int) -> int:
        value = result.get(key, 0)
        return max(0, min(int(value), ceiling)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    with db_factory() as db:
        row = GameResult(
            user_id=user_id, device_id=device_id[:128], game=game, outcome=outcome,
            score=number("score", 100000), max_score=number("max_score", 100000),
            rounds=number("rounds", 10000), duration_seconds=number("duration_seconds", 86400),
            difficulty=difficulty if difficulty in {"easy", "normal", "hard"} else "normal",
            source=source, simulated=simulated,
        )
        db.add(row)
        db.commit()
        return row.id


def ingest_offline(db_factory, device_id: str, records: Any) -> int:
    """Stores results the device kept while the backend was unreachable; replays are ignored."""
    if not isinstance(records, list):
        return 0
    stored = 0
    for item in records[:32]:
        if not isinstance(item, dict):
            continue
        stamp = item.get("ts")
        finished = (
            datetime.fromtimestamp(stamp, timezone.utc)
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 1_700_000_000
            else datetime.now(timezone.utc)
        )
        with db_factory() as db:
            known = db.execute(select(GameResult.id).where(
                GameResult.device_id == device_id[:128], GameResult.game == str(item.get("game")),
                GameResult.finished_at == finished)).first()
            owner = db.execute(select(GameResult.user_id).where(
                GameResult.device_id == device_id[:128], GameResult.user_id.is_not(None)
            ).order_by(GameResult.finished_at.desc()).limit(1)).scalar()
        if known and finished.year > 2000 and isinstance(stamp, (int, float)) and stamp > 1_700_000_000:
            continue
        row_id = record_result(db_factory, user_id=owner, device_id=device_id, result=item,
                               difficulty=str(item.get("difficulty") or "normal"), source="device-offline")
        if row_id:
            stored += 1
            with db_factory() as db:
                row = db.get(GameResult, row_id)
                row.finished_at = finished
                db.commit()
    return stored


def _rows(db_factory, user_id: str | None, limit: int | None = None) -> list[GameResult]:
    with db_factory() as db:
        query = select(GameResult).order_by(GameResult.finished_at.desc())
        if user_id is not None:
            query = query.where(GameResult.user_id == user_id)
        if limit:
            query = query.limit(limit)
        rows = list(db.execute(query).scalars())
        for row in rows:
            db.expunge(row)
        return rows


def _streak(outcomes_newest_first: list[str]) -> dict[str, Any]:
    if not outcomes_newest_first:
        return {"kind": None, "length": 0}
    kind = outcomes_newest_first[0]
    length = 0
    for outcome in outcomes_newest_first:
        if outcome != kind:
            break
        length += 1
    return {"kind": kind, "length": length}


def compute_stats(rows: list[GameResult]) -> dict[str, Any]:
    per_game: dict[str, dict[str, Any]] = {}
    for game in GAME_LABELS:
        game_rows = [row for row in rows if row.game == game]
        if not game_rows:
            continue
        counted = [row for row in game_rows if row.outcome != "abandoned"]
        wins = sum(1 for row in counted if row.outcome == "win")
        per_game[game] = {
            "played": len(game_rows),
            "wins": wins,
            "losses": sum(1 for row in counted if row.outcome == "loss"),
            "draws": sum(1 for row in counted if row.outcome == "draw"),
            "abandoned": len(game_rows) - len(counted),
            "win_rate": round(wins / len(counted), 3) if counted else None,
            "average_duration_seconds": round(sum(row.duration_seconds for row in game_rows) / len(game_rows)),
            "best_score": max((row.score for row in counted), default=0),
            "streak": _streak([row.outcome for row in counted]),
            "recent_form": [row.outcome for row in game_rows[:5]],
            "last_played": game_rows[0].finished_at.isoformat(),
        }
    patterns = learning_patterns(rows, per_game)
    return {
        "total_games": len(rows),
        "total_wins": sum(item["wins"] for item in per_game.values()),
        "games": per_game,
        "patterns": patterns,
    }


def learning_patterns(rows: list[GameResult], per_game: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"favorite": None, "strongest": None, "weakest": None, "play_hours": [], "suggested_difficulty": {}, "insights": []}
    favorite = max(per_game, key=lambda game: per_game[game]["played"])
    rated = {game: item for game, item in per_game.items() if item["win_rate"] is not None and item["played"] >= 3}
    strongest = max(rated, key=lambda game: rated[game]["win_rate"], default=None)
    weakest = min(rated, key=lambda game: rated[game]["win_rate"], default=None)
    hours = Counter(row.finished_at.astimezone(timezone.utc).hour for row in rows)
    play_hours = [hour for hour, _ in hours.most_common(3)]
    difficulty: dict[str, str] = {}
    for game, item in per_game.items():
        last = [row for row in rows if row.game == game and row.outcome != "abandoned"][:6]
        if len(last) >= 3:
            rate = sum(1 for row in last if row.outcome == "win") / len(last)
            difficulty[game] = "hard" if rate >= 0.67 else "easy" if rate <= 0.2 else "normal"
    insights = [f"Plays {GAME_LABELS[favorite]} most often ({per_game[favorite]['played']} games)."]
    if strongest:
        insights.append(f"Best results in {GAME_LABELS[strongest]} ({round(rated[strongest]['win_rate'] * 100)}% wins).")
    if weakest and weakest != strongest:
        insights.append(f"Finds {GAME_LABELS[weakest]} hardest ({round(rated[weakest]['win_rate'] * 100)}% wins).")
    abandoned = sum(item["abandoned"] for item in per_game.values())
    if abandoned and abandoned / len(rows) >= 0.3:
        insights.append("Often stops games before they finish; prefer shorter games.")
    return {
        "favorite": favorite, "strongest": strongest, "weakest": weakest,
        "play_hours": play_hours, "suggested_difficulty": difficulty, "insights": insights,
    }


def user_stats(db_factory, user_id: str | None) -> dict[str, Any]:
    return compute_stats(_rows(db_factory, user_id))


def history(db_factory, user_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id, "game": row.game, "outcome": row.outcome, "score": row.score, "max_score": row.max_score,
            "rounds": row.rounds, "duration_seconds": row.duration_seconds, "difficulty": row.difficulty,
            "device_id": row.device_id, "simulated": row.simulated, "source": row.source,
            "finished_at": row.finished_at.isoformat(),
        }
        for row in _rows(db_factory, user_id, limit)
    ]


def clear_history(db_factory, user_id: str | None) -> int:
    with db_factory() as db:
        statement = delete(GameResult)
        if user_id is not None:
            statement = statement.where(GameResult.user_id == user_id)
        deleted = db.execute(statement).rowcount or 0
        db.commit()
        return deleted


def suggested_difficulty(db_factory, user_id: str | None, game: str) -> str:
    return user_stats(db_factory, user_id)["patterns"]["suggested_difficulty"].get(game, "normal")


def spoken_summary(db_factory, user_id: str | None, game: str | None = None) -> str:
    stats = user_stats(db_factory, user_id)
    if not stats["total_games"]:
        return "We have not finished any games yet. Say play a game to start."
    games = stats["games"]
    if game and game in games:
        item = games[game]
        rate = f", a {round(item['win_rate'] * 100)} percent win rate" if item["win_rate"] is not None else ""
        return (f"In {GAME_LABELS[game]} you have played {item['played']} times with {item['wins']} wins, "
                f"{item['losses']} losses and {item['draws']} draws{rate}.")
    wins = stats["total_wins"]
    parts = [f"You have played {stats['total_games']} games and won {wins}."]
    parts.extend(stats["patterns"]["insights"][:2])
    streak = games[stats["patterns"]["favorite"]]["streak"] if stats["patterns"]["favorite"] else None
    if streak and streak["length"] >= 2 and streak["kind"] in {"win", "loss"}:
        parts.append(f"Your current {GAME_LABELS[stats['patterns']['favorite']]} streak is {streak['length']} {'wins' if streak['kind'] == 'win' else 'losses'}.")
    return " ".join(parts)


def prompt_context(db_factory, user_id: str | None) -> str:
    """Compact, non-sensitive description of the user's game habits for the AI system prompt."""
    try:
        stats = user_stats(db_factory, user_id)
    except Exception:
        return ""
    if not stats["total_games"]:
        return ""
    lines = [f"Game history (device mini-games): {stats['total_games']} played, {stats['total_wins']} won."]
    for game, item in stats["games"].items():
        rate = "n/a" if item["win_rate"] is None else f"{round(item['win_rate'] * 100)}%"
        lines.append(f"- {GAME_LABELS[game]}: {item['played']} played, win rate {rate}, recent {','.join(item['recent_form'])}.")
    lines.extend(stats["patterns"]["insights"])
    lines.append("Use this only when the user asks about games or wants a recommendation; do not volunteer it otherwise.")
    return "\n".join(lines)


def recommend_game(db_factory, user_id: str | None) -> str:
    """Picks the least-played game so random picks add variety; ties break randomly."""
    import random

    games = user_stats(db_factory, user_id)["games"]
    counts = {game: games.get(game, {}).get("played", 0) for game in GAME_LABELS}
    fewest = min(counts.values())
    return random.choice([game for game, count in counts.items() if count == fewest])
