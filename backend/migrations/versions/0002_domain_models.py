"""Add persistent assistant domain entities."""

from alembic import op

from app.database.session import Base
from app import models  # noqa: F401 - register every SQLAlchemy model

revision = "0002_domain_models"
down_revision = "0001_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in reversed(Base.metadata.sorted_tables):
        if table.name != "audit_logs":
            table.drop(op.get_bind(), checkfirst=True)
