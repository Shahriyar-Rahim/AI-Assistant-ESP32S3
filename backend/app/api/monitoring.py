from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi import Depends, HTTPException, Request

from app.api.dependencies import require_user
from app.core.security import AuthenticationError, verify_access_token
from app.models import User

router = APIRouter()


@router.get("/monitoring/telemetry")
def telemetry_history(
    request: Request,
    device_id: str | None = None,
    limit: int = Query(default=120, ge=1, le=600),
    _: User = Depends(require_user),
) -> dict[str, object]:
    histories = request.app.state.telemetry_history
    ids = [device_id] if device_id else list(histories)
    samples = [sample for selected in ids for sample in list(histories.get(selected, ()))[-limit:]]
    samples.sort(key=lambda item: str(item.get("timestamp", "")))
    return {"samples": samples[-limit:], "devices": ids}


@router.websocket("/monitoring/stream")
async def telemetry_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        authentication = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except (TimeoutError, WebSocketDisconnect):
        await websocket.close(code=1008, reason="authentication required")
        return
    access_token = authentication.get("access_token") if isinstance(authentication, dict) else None
    if not isinstance(access_token, str) or not access_token or len(access_token) > 4096:
        await websocket.close(code=1008, reason="authentication required")
        return
    settings = websocket.app.state.settings
    if not settings.auth_signing_secret:
        await websocket.close(code=1011, reason="dashboard authentication is not configured")
        return
    try:
        claims = verify_access_token(access_token, settings.auth_signing_secret)
    except AuthenticationError:
        await websocket.close(code=1008, reason="authentication failed")
        return
    with websocket.app.state.db_factory() as db:
        user = db.get(User, claims["sub"])
        if user is None or not user.active or user.role != claims["role"]:
            await websocket.close(code=1008, reason="authentication failed")
            return
    websocket.app.state.telemetry_clients.add(websocket)
    try:
        for device_id, history in websocket.app.state.telemetry_history.items():
            for sample in list(history)[-30:]:
                await websocket.send_json({"type": "telemetry.sample", "payload": sample})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        websocket.app.state.telemetry_clients.discard(websocket)
