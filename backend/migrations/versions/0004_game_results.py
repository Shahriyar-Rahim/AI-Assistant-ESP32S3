"""Add game result history for statistics and adaptive play."""

from alembic import op

from app.database.session import Base
from app import models  # noqa: F401 - register every SQLAlchemy model

revision = "0004_game_results"
down_revision = "0003_voice_automation_telemetry_ota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["game_results"].drop(op.get_bind(), checkfirst=True)
