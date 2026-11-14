import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_migrations_respect_explicit_database_url(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    database = tmp_path / "migration.db"
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env["DATABASE_URL"] = f"sqlite:///{database}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend, env=env, check=True, capture_output=True, text=True,
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == "0006_todo_lists"
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' and name in ('todo_lists', 'todo_list_members')"
        ).fetchone()[0] == 2
