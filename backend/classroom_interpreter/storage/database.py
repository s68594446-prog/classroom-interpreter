from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback AND release handles on context exit."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Database:
    def __init__(self, path: Path, migrations_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.migrations_dir = migrations_dir or (
            Path(__file__).resolve().parents[2] / "migrations"
        )

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def migrate(self) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }

        for migration_path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
            version_text, _, _ = migration_path.name.partition("_")
            version = int(version_text)
            if version in applied:
                continue
            script = migration_path.read_text(encoding="utf-8")
            applied_at_expression = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            connection = self.connect()
            try:
                directive = next((line for line in script.splitlines()
                                  if line.startswith("-- add-column-if-missing ")), None)
                if directive:
                    marker, operation, table, column = directive.split()
                    if marker != "--" or operation != "add-column-if-missing":
                        raise ValueError("invalid schema-aware migration directive")
                    if not table.isidentifier() or not column.isidentifier():
                        raise ValueError("invalid schema-aware migration directive")
                    columns = {row["name"] for row in connection.execute(
                        f"PRAGMA table_info({table})").fetchall()}
                    if column in columns:
                        script = "SELECT 1;"
                wrapped = (
                    "BEGIN IMMEDIATE;\n"
                    f"{script}\n"
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES ({version}, '{migration_path.name}', {applied_at_expression});\n"
                    "COMMIT;"
                )
                connection.executescript(wrapped)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            applied.add(version)
        return max(applied, default=0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
