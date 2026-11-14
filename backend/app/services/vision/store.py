"""Database access for faces, vision events and memories. All functions are synchronous (call via asyncio.to_thread)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import delete, func, select, update

from app.models import FaceEmbedding, FaceIdentity, User, VisionEvent, VisionMemory


def admin_user_id(db_factory) -> str | None:
    with db_factory() as db:
        user = db.execute(select(User).where(User.active.is_(True)).order_by(User.role, User.created_at)).scalars().first()
        return user.id if user else None


def load_embeddings(db_factory) -> list[tuple[str, bytes]]:
    with db_factory() as db:
        return [(row.identity_id, bytes(row.vector)) for row in db.execute(select(FaceEmbedding)).scalars()]


def identities(db_factory) -> dict[str, dict]:
    with db_factory() as db:
        counts = dict(db.execute(select(FaceEmbedding.identity_id, func.count()).group_by(FaceEmbedding.identity_id)).all())
        return {
            row.id: {
                "id": row.id, "name": row.name, "role": row.role, "sightings": row.sightings,
                "embeddings": counts.get(row.id, 0),
                "created_at": row.created_at.isoformat(),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            }
            for row in db.execute(select(FaceIdentity).order_by(FaceIdentity.created_at)).scalars()
        }


def create_identity(db_factory, user_id: str | None, name: str, role: str, vector: np.ndarray) -> str:
    with db_factory() as db:
        identity = FaceIdentity(user_id=user_id, name=name, role=role, sightings=1, last_seen_at=datetime.now(timezone.utc))
        db.add(identity)
        db.flush()
        db.add(FaceEmbedding(identity_id=identity.id, vector=vector.astype(np.float32).tobytes()))
        db.commit()
        return identity.id


def add_embedding(db_factory, identity_id: str, vector: np.ndarray) -> None:
    with db_factory() as db:
        db.add(FaceEmbedding(identity_id=identity_id, vector=vector.astype(np.float32).tobytes()))
        db.commit()


def touch(db_factory, identity_id: str) -> None:
    with db_factory() as db:
        row = db.get(FaceIdentity, identity_id)
        if row is not None:
            row.sightings += 1
            row.last_seen_at = datetime.now(timezone.utc)
            db.commit()


def next_stranger_name(db_factory) -> str:
    with db_factory() as db:
        count = db.scalar(select(func.count()).select_from(FaceIdentity).where(FaceIdentity.role == "stranger")) or 0
    return f"Unknown {count + 1}"


def find_identity(db_factory, name: str, role: str | None = None) -> FaceIdentity | None:
    with db_factory() as db:
        query = select(FaceIdentity).where(func.lower(FaceIdentity.name) == name.strip().casefold())
        if role:
            query = query.where(FaceIdentity.role == role)
        row = db.execute(query).scalars().first()
        if row is not None:
            db.expunge(row)
        return row


def set_identity(db_factory, identity_id: str, name: str | None = None, role: str | None = None) -> bool:
    with db_factory() as db:
        row = db.get(FaceIdentity, identity_id)
        if row is None:
            return False
        if name:
            row.name = name.strip()[:80]
        if role:
            row.role = role
        db.commit()
        return True


def delete_identity(db_factory, identity_id: str) -> bool:
    with db_factory() as db:
        row = db.get(FaceIdentity, identity_id)
        if row is None:
            return False
        db.execute(delete(FaceEmbedding).where(FaceEmbedding.identity_id == identity_id))
        db.delete(row)
        db.commit()
        return True


def merge_identities(db_factory, source_id: str, target_id: str) -> None:
    if source_id == target_id:
        raise ValueError("Choose two different people.")
    with db_factory() as db:
        source = db.get(FaceIdentity, source_id)
        target = db.get(FaceIdentity, target_id)
        if source is None or target is None:
            raise ValueError("One of the selected people no longer exists.")
        if source.user_id != target.user_id:
            raise ValueError("People belonging to different users cannot be merged.")
        db.execute(update(FaceEmbedding).where(FaceEmbedding.identity_id == source_id).values(identity_id=target_id))
        db.execute(update(VisionEvent).where(VisionEvent.identity_id == source_id).values(identity_id=target_id))
        target.sightings += source.sightings
        if source.last_seen_at is not None:
            def utc(value: datetime) -> datetime:
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

            if target.last_seen_at is None or utc(source.last_seen_at) > utc(target.last_seen_at):
                target.last_seen_at = source.last_seen_at
        db.delete(source)
        db.commit()


def log_event(db_factory, user_id: str | None, kind: str, detail: str = "", identity_id: str | None = None,
              media_path: str | None = None, alerted: bool = False) -> str:
    with db_factory() as db:
        event = VisionEvent(user_id=user_id, kind=kind, detail=detail[:2000], identity_id=identity_id,
                            media_path=media_path, alerted=int(alerted))
        db.add(event)
        db.commit()
        return event.id


def recent_events(db_factory, limit: int = 50, since: timedelta | None = None) -> list[dict]:
    with db_factory() as db:
        query = select(VisionEvent).order_by(VisionEvent.created_at.desc()).limit(limit)
        if since:
            query = query.where(VisionEvent.created_at >= datetime.now(timezone.utc) - since)
        return [
            {"id": row.id, "created_at": row.created_at.isoformat(), "kind": row.kind, "detail": row.detail,
             "identity_id": row.identity_id, "media_path": row.media_path, "alerted": bool(row.alerted)}
            for row in db.execute(query).scalars()
        ]


def add_memory(db_factory, user_id: str | None, kind: str, label: str, content: str = "",
               descriptor: bytes | None = None, meta: dict | None = None) -> str:
    with db_factory() as db:
        memory = VisionMemory(user_id=user_id, kind=kind, label=label.strip()[:120], content=content,
                              descriptor=descriptor, meta=meta or {})
        db.add(memory)
        db.commit()
        return memory.id


def memories(db_factory, user_id: str | None, kind: str | None = None) -> list[dict]:
    with db_factory() as db:
        query = select(VisionMemory).where(VisionMemory.user_id == user_id).order_by(VisionMemory.created_at.desc())
        if kind:
            query = query.where(VisionMemory.kind == kind)
        return [
            {"id": row.id, "kind": row.kind, "label": row.label, "content": row.content, "meta": row.meta,
             "created_at": row.created_at.isoformat(), "has_descriptor": row.descriptor is not None}
            for row in db.execute(query).scalars()
        ]


def object_descriptors(db_factory, user_id: str | None) -> list[tuple[str, str, bytes]]:
    with db_factory() as db:
        return [
            (row.id, row.label, bytes(row.descriptor))
            for row in db.execute(select(VisionMemory).where(
                VisionMemory.user_id == user_id, VisionMemory.kind == "object", VisionMemory.descriptor.is_not(None)
            )).scalars()
        ]


def delete_memory(db_factory, user_id: str | None, memory_id: str) -> bool:
    with db_factory() as db:
        row = db.get(VisionMemory, memory_id)
        if row is None or row.user_id != user_id:
            return False
        db.delete(row)
        db.commit()
        return True


def search_memories(db_factory, user_id: str | None, words: set[str], kinds: tuple[str, ...] = ("home", "text", "object")) -> list[dict]:
    scored = []
    for item in memories(db_factory, user_id):
        if item["kind"] not in kinds:
            continue
        haystack = set(f"{item['label']} {item['content']}".casefold().replace(",", " ").split())
        hits = len(words & haystack)
        if hits:
            scored.append((hits, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]
