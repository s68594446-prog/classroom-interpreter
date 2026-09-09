from pathlib import Path

import pytest

from classroom_interpreter.storage import Database, SessionRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "runtime.sqlite3")
    assert db.migrate() == 5
    return db


@pytest.fixture
def repository(database: Database) -> SessionRepository:
    return SessionRepository(database)
