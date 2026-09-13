from __future__ import annotations

import os
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
        self._restrict_permissions()
        connection = sqlite3.connect(self.path, timeout=5, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        # Overwrite deleted content instead of returning it to the freelist
        # intact. Without this, purged transcripts and translations stay
        # readable in the file with `strings` long after the rows are gone,
        # which breaks the retention rule that only the final summary and the
        # content-free ledger persist (D-039). Applies to deletes made on this
        # connection, so it must be set on every connection, not once.
        connection.execute("PRAGMA secure_delete = ON")
        self._restrict_permissions()
        return connection

    def _restrict_permissions(self) -> None:
        """Keep the file and its directory owner-only.

        This database holds every transcript, every translation and the replay
        log -- the most sensitive data the product touches. SQLite creates it
        with 0666 masked by the process umask, which on a default macOS account
        is 0644: world-readable. The content-free health database already
        chmods itself to 0600, so the protection was applied to the one store
        that did not need it and omitted from the one that did.

        Re-applied on every connect because SQLite recreates the -wal and -shm
        sidecars, which carry the same content, whenever the file is reopened.
        Failures are ignored: a session that cannot be tightened is still far
        better than a session that refuses to record.
        """
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        for candidate in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

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
                # A table rebuild must run with foreign keys OFF. Every script is
                # wrapped in a transaction below, and SQLite silently ignores
                # `PRAGMA foreign_keys` inside one -- so a `PRAGMA foreign_keys =
                # OFF` written in the migration itself does nothing at all, and
                # `DROP TABLE` then performs an implicit DELETE that cascades.
                #
                # That shipped. Migration 010 rebuilt `segments` and, on a copy
                # of a real database, deleted every asr_result, translation,
                # staged translation, context snapshot and processing attempt
                # while leaving the segments themselves in place -- and
                # `foreign_key_check` came back clean, because the children were
                # gone rather than orphaned. No test migrated a database that
                # already held data, so nothing noticed.
                #
                # The pragma is only honoured here, on the connection, BEFORE the
                # transaction opens. A drop without the directive is refused
                # outright rather than trusted.
                fk_off = any(
                    line.strip() == "-- requires-foreign-keys-off"
                    for line in script.splitlines()
                )
                drops_table = "DROP TABLE" in script.upper()
                if drops_table and not fk_off:
                    raise ValueError(
                        f"{migration_path.name} drops a table without declaring "
                        "'-- requires-foreign-keys-off'; with foreign keys on, the "
                        "drop cascades and deletes every dependent row"
                    )
                if fk_off:
                    connection.execute("PRAGMA foreign_keys = OFF")
                    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
                    if enabled:
                        raise RuntimeError("could not disable foreign keys for migration")
                connection.executescript(f"BEGIN IMMEDIATE;\n{script}\n")
                if fk_off:
                    # Still inside the transaction: a rebuild that broke a
                    # reference rolls back instead of committing it.
                    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise RuntimeError(
                            f"{migration_path.name} left {len(violations)} "
                            "foreign-key violations"
                        )
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES (?, ?, {applied_at_expression})",
                    (version, migration_path.name),
                )
                connection.commit()
                if fk_off:
                    connection.execute("PRAGMA foreign_keys = ON")
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
