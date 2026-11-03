from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import require_user
from app.services import local_brain

router = APIRouter()


@router.get("/brain")
async def brain_stats(request: Request, user=Depends(require_user)) -> dict[str, object]:
    data = local_brain.stats(request.app.state.db_factory, user.id)
    data["local_first"] = request.app.state.settings.local_brain_first
    data["runs_on"] = "backend host (retrieval model; not on the ESP32-S3)"
    return data


@router.delete("/brain")
async def brain_clear(request: Request, user=Depends(require_user)) -> dict[str, int]:
    return {"deleted": local_brain.clear(request.app.state.db_factory, user.id)}


@router.delete("/brain/{entry_id}")
async def brain_delete(entry_id: str, request: Request, user=Depends(require_user)) -> dict[str, bool]:
    if not local_brain.delete_entry(request.app.state.db_factory, user.id, entry_id):
        raise HTTPException(404, "Entry not found")
    return {"deleted": True}
