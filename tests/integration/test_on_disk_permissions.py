"""Nothing this product writes may be readable by another local account.

Found by auditing a live 60-minute run rather than by reading code: the
content-free health database was chmod 0600, while `runtime.sqlite3` -- every
transcript, every translation, and the websocket replay log -- sat at 0644
inside a 0755 directory, and the recorded segment audio at 0644.

The whole data-lifecycle design (`NO VALID SUMMARY = NO PURGE`, secure_delete,
D-039's zeroed free pages) exists to stop classroom content from spreading.
Leaving the file world-readable defeats all of it without any attacker at all.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from classroom_interpreter.storage import Database

GROUP_AND_OTHER = stat.S_IRWXG | stat.S_IRWXO


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_runtime_database_is_not_readable_by_other_accounts(tmp_path):
    """Mutation that turns this red: drop the `_restrict_permissions` call."""
    database = Database(tmp_path / "data" / "runtime.sqlite3")
    database.migrate()
    with database.connect() as connection:
        connection.execute("SELECT 1")

    assert mode_of(tmp_path / "data" / "runtime.sqlite3") & GROUP_AND_OTHER == 0


def test_the_wal_sidecar_is_protected_too(tmp_path):
    """WAL mode is on, so uncheckpointed transcripts live in the -wal file.

    Protecting only the main database would leave the most recent minutes of a
    class -- exactly the part not yet checkpointed -- readable.

    Mutation that turns this red: chmod only `self.path`.
    """
    database = Database(tmp_path / "data" / "runtime.sqlite3")
    database.migrate()
    # The sidecar only exists while a connection is open -- a clean close
    # checkpoints and removes it -- so the check has to happen mid-connection.
    connection = database.connect()
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, t TEXT)"
        )
        connection.execute("INSERT INTO probe(t) VALUES ('x')")
        connection.commit()
        wal = tmp_path / "data" / "runtime.sqlite3-wal"
        assert wal.exists(), "WAL mode should have produced a sidecar"
        assert mode_of(wal) & GROUP_AND_OTHER == 0
    finally:
        connection.close()


def test_the_containing_directory_is_owner_only(tmp_path):
    """A 0700 directory is what stops another account from listing session ids.

    Mutation that turns this red: chmod the file but not `self.path.parent`.
    """
    database = Database(tmp_path / "data" / "runtime.sqlite3")
    database.migrate()
    assert mode_of(tmp_path / "data") & GROUP_AND_OTHER == 0


def test_recorded_segment_audio_is_owner_only(database, tmp_path):
    """The audio itself, not just the text derived from it.

    This drives the real seal path -- an earlier version of this test chmod-ed
    the sample file itself and so proved only that `os.chmod` works.

    Mutation that turns this red: drop the `os.chmod(final_path, 0o600)` after
    `os.replace` in `segmentation.py`.
    """
    from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig

    import helpers_phase3 as h

    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("silence", 0.5), ("speech", 3.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    store = tmp_path / "sessions" / session_id
    agent = SegmentAgent(
        database,
        store,
        session_id,
        SegmenterConfig(
            min_seconds=2.0, max_seconds=30.0, silence_milliseconds=700,
            overlap_milliseconds=0, threshold_rms=300.0,
        ),
    )
    sealed = agent.process_available_chunks()
    assert sealed, "no segment was sealed, so nothing was audited"

    written = sorted((store / "segments").glob("seg_*.wav"))
    assert written, "seal produced no audio file"
    for path in written:
        assert mode_of(path) & GROUP_AND_OTHER == 0, f"{path.name} is readable by others"

    assert mode_of(store / "segments") & GROUP_AND_OTHER == 0
    assert mode_of(store) & GROUP_AND_OTHER == 0, (
        "the session directory was left 0755 because mkdir(mode=) does not "
        "apply to parents or to an existing directory"
    )
