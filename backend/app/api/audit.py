from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select

from app.api.dependencies import require_user
from app.models import AuditLog, User

router = APIRouter(dependencies=[Depends(require_user)])


def _row_dict(row: AuditLog) -> dict[str, object]:
    return {
        "id": row.id,
        "timestamp": row.created_at.isoformat(),
        "event_type": row.event_type,
        "source": row.source,
        "device_id": row.device_id,
        "action": row.action,
        "result": row.result,
        "metadata": row.metadata_json,
    }


@router.get("/audit")
def list_audit(
    request: Request,
    event_type: str | None = Query(default=None, max_length=80),
    source: str | None = Query(default=None, max_length=80),
    start: datetime | None = None,
    end: datetime | None = None,
    search: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_user),
) -> dict[str, object]:
    query = select(AuditLog)
    if event_type:
        query = query.where(AuditLog.event_type == event_type)
    if source:
        query = query.where(AuditLog.source == source)
    if start:
        query = query.where(AuditLog.created_at >= start)
    if end:
        query = query.where(AuditLog.created_at <= end)
    if search:
        query = query.where(AuditLog.action.ilike(f"%{search}%"))
    with request.app.state.db_factory() as db:
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        records = db.execute(query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)).scalars().all()
        return {"items": [_row_dict(record) for record in records], "total": total, "offset": offset, "limit": limit}


@router.get("/audit/export.json")
def export_audit_json(request: Request, _: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        records = db.execute(select(AuditLog).order_by(AuditLog.created_at)).scalars().all()
        content = json.dumps([_row_dict(record) for record in records], ensure_ascii=False)
    return Response(content, media_type="application/json", headers={"Content-Disposition": "attachment; filename=audit.json"})


@router.get("/audit/export.csv")
def export_audit_csv(request: Request, _: User = Depends(require_user)) -> Response:
    with request.app.state.db_factory() as db:
        records = db.execute(select(AuditLog).order_by(AuditLog.created_at)).scalars().all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "timestamp", "event_type", "source", "device_id", "action", "result", "metadata"])
        for record in records:
            writer.writerow([
                record.id,
                record.created_at.isoformat(),
                _csv_safe(record.event_type),
                _csv_safe(record.source),
                _csv_safe(record.device_id or ""),
                _csv_safe(record.action),
                _csv_safe(record.result),
                json.dumps(record.metadata_json, ensure_ascii=False),
            ])
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})


def _csv_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
