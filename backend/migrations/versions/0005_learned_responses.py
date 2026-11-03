"""Add the local brain's learned responses."""

from alembic import op

from app.database.session import Base
from app import models  # noqa: F401 - register every SQLAlchemy model

revision = "0005_learned_responses"
down_revision = "0004_game_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["learned_responses"].drop(op.get_bind(), checkfirst=True)
