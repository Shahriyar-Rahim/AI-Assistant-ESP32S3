"""Add persistent named to-do lists without changing existing tasks."""

from alembic import op

from app.database.session import Base
from app import models  # noqa: F401

revision = "0006_todo_lists"
down_revision = "0005_learned_responses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["todo_lists"].create(bind, checkfirst=True)
    Base.metadata.tables["todo_list_members"].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["todo_list_members"].drop(bind, checkfirst=True)
    Base.metadata.tables["todo_lists"].drop(bind, checkfirst=True)
