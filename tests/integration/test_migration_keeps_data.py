"""A migration must never delete the data it is migrating.

Migration 010 rebuilt `segments` with `PRAGMA foreign_keys = OFF` written in
the script. The runner wraps every script in a transaction, where SQLite
ignores that pragma, so `DROP TABLE segments` ran an implicit DELETE that
cascaded: on a copy of a real classroom database it removed 10 transcripts, 3
translations, 7 staged translations, 10 context snapshots and 40 attempt
records, and kept all 14 segments. `foreign_key_check` came back clean,
because the dependent rows were deleted rather than orphaned.

Every existing migration test starts from an empty database, which is exactly
the one case where a cascading delete deletes nothing.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from classroom_interpreter.storage import Database

MIGRATIONS = Path(__file__).resolve().parents[2] / "backend" / "migrations"
NOW = "2026-09-14T00:00:00+00:00"
CHILD_TABLES = (
    "asr_results",
    "translation_results",
    "translation_staged_results",
    "translation_context_snapshots",
    "processing_attempts",
)


def database_at(tmp_path: Path, last_version: int) -> Database:
    partial = tmp_path / "migrations-partial"
    partial.mkdir()
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        if int(path.name[:3]) <= last_version:
            shutil.copy(path, partial / path.name)
    db = Database(tmp_path / "runtime.sqlite3", migrations_dir=partial)
    assert db.migrate() == last_version
    return db


def populate(db: Database) -> None:
    """One class mid-pipeline: every table that references `segments`."""
    with db.connect() as c:
        c.execute(
            "INSERT INTO sessions(id, course_name, status, created_at, app_version, "
            "updated_at) VALUES ('s', 'Course', 'RECOVERY_REQUIRED', ?, 't', ?)",
            (NOW, NOW),
        )
        for seq, state in ((0, "DONE"), (1, "TRANSLATION_PROCESSING")):
            sid = f"seg{seq}"
            c.execute(
                "INSERT INTO segments(id, session_id, sequence, start_ms, end_ms, "
                "duration_ms, audio_path, audio_sha256, state, created_at, updated_at) "
                "VALUES (?, 's', ?, ?, ?, 1000, '/x.wav', 'h', ?, ?, ?)",
                (sid, seq, seq * 1000, seq * 1000 + 1000, state, NOW, NOW),
            )
            c.execute(
                "INSERT INTO asr_results(segment_id, session_id, segment_sequence, "
                "english_text, provider, model, latency_ms, audio_seconds, created_at) "
                "VALUES (?, 's', ?, 'Hello class.', 'p', 'm', 1, 1.0, ?)",
                (sid, seq, NOW),
            )
            c.execute(
                "INSERT INTO translation_context_snapshots(segment_id, session_id, "
                "segment_sequence, prompt_version, context_json, context_sha256, "
                "created_at) VALUES (?, 's', ?, 1, '{}', 'h', ?)",
                (sid, seq, NOW),
            )
            c.execute(
                "INSERT INTO processing_attempts(id, session_id, segment_id, stage, "
                "attempt_number, started_at) VALUES (?, 's', ?, 'ASR', 1, ?)",
                (f"a{seq}", sid, NOW),
            )
        c.execute(
            "INSERT INTO translation_results(segment_id, session_id, segment_sequence, "
            "chinese_text, english_text_sha256, provider, model, latency_ms, "
            "created_at) VALUES ('seg0', 's', 0, '同学们好。', 'h', 'p', 'm', 1, ?)",
            (NOW,),
        )
        c.execute(
            "INSERT INTO translation_staged_results(segment_id, session_id, "
            "segment_sequence, chinese_text, english_text_sha256, provider, model, "
            "latency_ms, created_at, staged_at) "
            "VALUES ('seg1', 's', 1, '同学们好。', 'h', 'p', 'm', 1, ?, ?)",
            (NOW, NOW),
        )
        c.commit()


def counts(db: Database) -> dict[str, int]:
    with db.connect() as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("segments", *CHILD_TABLES)}


def test_migrating_a_database_with_data_keeps_every_row(tmp_path):
    """Mutation that turns this red: drop the `fk_off` PRAGMA from `migrate()`
    -- which is the shipped bug exactly."""
    old = database_at(tmp_path, 9)
    populate(old)
    before = counts(old)
    assert all(before.values()), f"fixture did not populate every table: {before}"

    upgraded = Database(old.path)  # the real, full migrations directory
    assert upgraded.migrate() >= 10
    after = counts(upgraded)

    assert after == before, f"migration deleted data: {before} -> {after}"


def test_foreign_keys_are_back_on_after_a_rebuild(tmp_path):
    """The connection that ran the rebuild must not leak `foreign_keys = OFF`.

    Mutation that turns this red: drop the `PRAGMA foreign_keys = ON` after
    commit AND make `connect()` stop setting it -- the two layers together.
    """
    old = database_at(tmp_path, 9)
    populate(old)
    Database(old.path).migrate()
    with Database(old.path).connect() as c:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_table_drop_without_the_directive_is_refused(tmp_path):
    """The next rebuild must not be able to repeat this silently.

    Mutation that turns this red: remove the `drops_table and not fk_off`
    guard.
    """
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "001_bad.sql").write_text(
        "CREATE TABLE a(id INTEGER PRIMARY KEY);\nDROP TABLE a;\n"
    )
    with pytest.raises(ValueError, match="requires-foreign-keys-off"):
        Database(tmp_path / "x.sqlite3", migrations_dir=migrations).migrate()


def test_a_rebuild_that_breaks_a_reference_is_rolled_back(tmp_path):
    """Foreign keys off is only safe if they are checked before commit.

    Mutation that turns this red: skip the `foreign_key_check` before commit.
    """
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "001_base.sql").write_text(
        "CREATE TABLE parent(id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child(id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id));\n"
    )
    db = Database(tmp_path / "x.sqlite3", migrations_dir=migrations)
    db.migrate()
    with db.connect() as c:
        c.execute("INSERT INTO parent(id) VALUES (1)")
        c.execute("INSERT INTO child(id, parent_id) VALUES (1, 1)")
        c.commit()
    (migrations / "002_breaks.sql").write_text(
        "-- requires-foreign-keys-off\n"
        "CREATE TABLE parent_new(id INTEGER PRIMARY KEY);\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;\n"
    )

    with pytest.raises(RuntimeError, match="foreign-key violations"):
        db.migrate()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1, (
            "a failed rebuild was committed"
        )
