"""Add automation rules, display configuration, and OTA releases."""

from alembic import op

from app.database.session import Base
from app import models  # noqa: F401 - register every SQLAlchemy model

revision = "0003_voice_automation_telemetry_ota"
down_revision = "0002_domain_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in ("ota_releases", "display_configurations", "automation_rules"):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
