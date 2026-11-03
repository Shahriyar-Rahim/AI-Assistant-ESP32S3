from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FaceIdentity(Base):
    """A person the camera has seen. Only embeddings are stored for matching, never the whole picture."""

    __tablename__ = "face_identities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False, default="stranger")  # owner | known | stranger
    sightings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"
    __table_args__ = (Index("ix_face_embeddings_identity", "identity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    identity_id: Mapped[str] = mapped_column(ForeignKey("face_identities.id", ondelete="CASCADE"), nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # 128 float32 SFace values
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class VisionEvent(Base):
    __tablename__ = "vision_events"
    __table_args__ = (Index("ix_vision_events_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_id: Mapped[str | None] = mapped_column(String(36))
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_path: Mapped[str | None] = mapped_column(String(400))
    alerted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class VisionMemory(Base):
    """Things the user asked to remember: objects (ORB descriptors), written text (OCR) and home organisation notes."""

    __tablename__ = "vision_memories"
    __table_args__ = (Index("ix_vision_memories_user_kind", "user_id", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # object | text | home
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    descriptor: Mapped[bytes | None] = mapped_column(LargeBinary)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
