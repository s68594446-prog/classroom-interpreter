from pathlib import Path

import pytest

from classroom_interpreter.storage import Database, SessionRepository

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "migrations"


def expected_schema_version() -> int:
    """Highest migration on disk.

    Derived rather than hard-coded: a literal here meant that adding migration
    006 failed the *fixture*, so 85 unrelated tests errored at setup with no
    hint that a new migration was the cause.
    """
    versions = [
        int(path.name.split("_", 1)[0])
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")
    ]
    assert versions, f"no migrations found in {MIGRATIONS_DIR}"
    return max(versions)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "runtime.sqlite3")
    assert db.migrate() == expected_schema_version()
    return db


@pytest.fixture
def repository(database: Database) -> SessionRepository:
    return SessionRepository(database)
