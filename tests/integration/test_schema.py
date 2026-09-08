import sqlite3
import shutil
from pathlib import Path

import pytest

from classroom_interpreter.storage import Database, IdempotencyConflict, SessionRepository


EXPECTED_TABLES = {
    "schema_migrations",
    "sessions",
    "purged_session_metadata",
    "capture_chunks",
    "segments",
    "asr_results",
    "translation_results",
    "processing_attempts",
    "summary_jobs",
    "agent_heartbeats",
    "metric_samples",
    "state_transition_audits",
    "websocket_events",
}


def test_migration_is_idempotent_and_creates_expected_schema(
    database: Database,
) -> None:
    assert database.migrate() == 4
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert EXPECTED_TABLES <= tables
    assert journal_mode == "wal"
    assert foreign_keys == 1


@pytest.mark.parametrize("v2_already_has_clock", [False, True])
def test_migration_three_upgrades_both_experimental_v2_variants(
    tmp_path: Path, v2_already_has_clock: bool
) -> None:
    source = Path(__file__).resolve().parents[2] / "backend" / "migrations"
    v2_dir = tmp_path / "v2-migrations"
    v2_dir.mkdir()
    shutil.copy(source / "001_initial.sql", v2_dir / "001_initial.sql")
    script = (source / "002_capture.sql").read_text()
    if v2_already_has_clock:
        script += "\nALTER TABLE capture_chunks ADD COLUMN monotonic_clock_id TEXT;\n"
    (v2_dir / "002_capture.sql").write_text(script)
    path = tmp_path / "upgrade.sqlite3"
    assert Database(path, migrations_dir=v2_dir).migrate() == 2
    assert Database(path, migrations_dir=source).migrate() == 4
    with Database(path).connect() as connection:
        columns = [row["name"] for row in connection.execute(
            "PRAGMA table_info(capture_chunks)")]
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    assert columns.count("monotonic_clock_id") == 1
    assert versions == [1, 2, 3, 4]


def test_post_purge_metadata_table_has_exact_allowlist(database: Database) -> None:
    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(purged_session_metadata)"
            ).fetchall()
        }
    assert columns == {
        "session_id",
        "course_name",
        "started_at",
        "ended_at",
        "duration_ms",
        "summary_path",
        "summary_hash",
        "summary_model",
        "app_version",
        "purged_at",
        "status",
    }


def test_segment_key_is_unique_and_idempotent(
    repository: SessionRepository, tmp_path: Path
) -> None:
    session = repository.create_session(
        "Risk Management", app_version="test", session_id="session-1"
    )
    first = repository.create_segment(
        session_id=session.id,
        sequence=381,
        start_ms=0,
        end_ms=2000,
        overlap_ms=0,
        audio_path=tmp_path / "381.wav",
        audio_sha256="same-source",
        segment_id="segment-381",
    )
    repeated = repository.create_segment(
        session_id=session.id,
        sequence=381,
        start_ms=0,
        end_ms=2000,
        overlap_ms=0,
        audio_path=tmp_path / "381-copy.wav",
        audio_sha256="same-source",
        segment_id="segment-381-copy",
    )
    assert repeated.id == first.id

    with pytest.raises(IdempotencyConflict):
        repository.create_segment(
            session_id=session.id,
            sequence=381,
            start_ms=0,
            end_ms=2500,
            overlap_ms=0,
            audio_path=tmp_path / "381-copy-2.wav",
            audio_sha256="different-source",
        )

    with repository.database.connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM segments WHERE session_id = ? AND sequence = ?",
            (session.id, 381),
        ).fetchone()[0]
    assert count == 1


def test_database_constraint_rejects_duplicate_segment_key(
    repository: SessionRepository, tmp_path: Path
) -> None:
    session = repository.create_session("Course", app_version="test")
    repository.create_segment(
        session_id=session.id,
        sequence=1,
        start_ms=0,
        end_ms=1000,
        overlap_ms=0,
        audio_path=tmp_path / "1.wav",
        audio_sha256="one",
    )
    with pytest.raises(sqlite3.IntegrityError):
        with repository.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO segments(
                    id, session_id, sequence, start_ms, end_ms, duration_ms,
                    overlap_ms, audio_path, audio_sha256, state, created_at, updated_at
                ) VALUES ('copy', ?, 1, 0, 1000, 1000, 0, ?, 'two',
                          'CAPTURED', 'now', 'now')
                """,
                (session.id, str(tmp_path / "copy.wav")),
            )
