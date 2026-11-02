"""Create initial audit log table."""

from alembic import op
import sqlalchemy as sa

revision = "0001_audit_logs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_logs_created_at_type", "audit_logs", ["created_at", "event_type"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at_type", table_name="audit_logs")
    op.drop_table("audit_logs")
