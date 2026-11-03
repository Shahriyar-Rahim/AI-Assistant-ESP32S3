from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.api.dependencies import require_user
from app.models import AuditLog, GameSession, User
from app.services import game_history
from app.services.games import GAME_NAMES, GameMoveError, apply_move, new_game

router = APIRouter(dependencies=[Depends(require_user)])


class GameStart(BaseModel):
    category: str = "general"


class GameMove(BaseModel):
    expected_version: int = Field(ge=1)
    move: dict[str, object]


def _public_state(game: str, state: dict) -> dict:
    result = dict(state)
    if game == "number_guessing" and not state.get("won"):
        result.pop("secret", None)
    if game == "hangman":
        if not state.get("won") and not state.get("lost"):
            result["display"] = "".join(letter if letter in state["guessed"] else "_" for letter in state["word"])
            result.pop("word", None)
    if game == "trivia" and not state.get("finished"):
        result["questions"] = [
            {key: value for key, value in question.items() if key != "answer"}
            for question in state["questions"]
        ]
    return result


def _audit(request: Request, user_id: str, action: str, metadata: dict) -> None:
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            event_type="game.session",
            source="dashboard",
            action=action,
            result="success",
            metadata_json={"user_id": user_id, **metadata},
        ))
        db.commit()


@router.get("/history")
def game_history_list(request: Request, limit: int = Query(50, ge=1, le=500), user: User = Depends(require_user)) -> dict[str, object]:
    return {"results": game_history.history(request.app.state.db_factory, user.id, limit)}


@router.get("/stats")
def game_statistics(request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    stats = game_history.user_stats(request.app.state.db_factory, user.id)
    stats["summary"] = game_history.spoken_summary(request.app.state.db_factory, user.id)
    return stats


@router.delete("/history", status_code=204)
def game_history_clear(request: Request, user: User = Depends(require_user)) -> None:
    deleted = game_history.clear_history(request.app.state.db_factory, user.id)
    _audit(request, user.id, "game history cleared", {"deleted": deleted})


@router.post("/{game}/sessions", status_code=201)
def start_game(game: str, body: GameStart, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    if game not in GAME_NAMES:
        raise HTTPException(404, "unknown game")
    try:
        state = new_game(game, category=body.category)
    except GameMoveError as exc:
        raise HTTPException(422, str(exc)) from exc
    with request.app.state.db_factory() as db:
        session = GameSession(
            id=str(uuid4()),
            game=game,
            user_id=user.id,
            state=state,
        )
        db.add(session)
        db.commit()
        result = {
            "session_id": session.id,
            "game": game,
            "version": session.version,
            "status": session.status,
            "state": _public_state(game, state),
        }
    _audit(request, user.id, "game.started", {"session_id": result["session_id"], "game": game})
    return result


@router.get("/sessions/{session_id}")
def get_game(session_id: str, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        session = db.get(GameSession, session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(404, "game session not found")
        return {
            "session_id": session.id,
            "game": session.game,
            "version": session.version,
            "status": session.status,
            "state": _public_state(session.game, session.state),
            "updated_at": session.updated_at.isoformat(),
        }


@router.post("/sessions/{session_id}/moves")
def make_move(
    session_id: str,
    body: GameMove,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        session = db.get(GameSession, session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(404, "game session not found")
        if session.version != body.expected_version:
            raise HTTPException(409, detail={"message": "stale game version", "current_version": session.version})
        updated_state = dict(session.state)
        try:
            updated_state = apply_move(session.game, updated_state, body.move)
        except GameMoveError as exc:
            raise HTTPException(422, str(exc)) from exc
        finished = any(updated_state.get(flag) for flag in ("finished", "won", "lost", "winner", "draw"))
        result = db.execute(
            update(GameSession)
            .where(
                GameSession.id == session_id,
                GameSession.user_id == user.id,
                GameSession.version == body.expected_version,
            )
            .values(
                state=updated_state,
                version=GameSession.version + 1,
                status="finished" if finished else "active",
            )
        )
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "game state changed; refresh and retry")
        db.commit()
        version = body.expected_version + 1
        response = {
            "session_id": session_id,
            "game": session.game,
            "version": version,
            "status": "finished" if finished else "active",
            "state": _public_state(session.game, updated_state),
        }
    _audit(request, user.id, "game.move", {"session_id": session_id, "version": version})
    return response
