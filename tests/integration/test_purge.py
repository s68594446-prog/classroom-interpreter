from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest

from classroom_interpreter.agents.cleanup import CleanupAgent, PurgeRefused
from classroom_interpreter.domain.models import SessionStatus
from classroom_interpreter.storage.repository import SessionRepository

from test_live_api import make_client, make_settings
from test_summary_pipeline import run_flow_to_summary

ALLOWLIST_COLUMNS = {
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

CONTENT_TABLES = (
    "capture_chunks",
    "segments",
    "asr_results",
    "translation_results",
    "translation_context_snapshots",
    "translation_staged_results",
    "processing_attempts",
    "summary_jobs",
    "summary_chunks",
    "websocket_events",
    "pipeline_cursors",
)


def count_rows(database, table, session_id):
    with database.connect() as connection:
        return connection.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]


def run_to_purged(tmp_path: Path):
    client, database, runtime = make_client(tmp_path, auto_purge=True)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Purge Course"}).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        deadline = time.monotonic() + 15
        gone = False
        while time.monotonic() < deadline:
            # The durable fact, not the HTTP status. `GET /api/sessions/{id}`
            # used to 404 for a purged class, and this waited on that -- but a
            # 404 was also what made the client stop updating the screen of the
            # class it had just finished, so the endpoint now answers with the
            # retained metadata instead. What purge guarantees is that the
            # `sessions` row is gone; ask the database that directly.
            with database.connect() as connection:
                gone = connection.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()["n"] == 0
            if gone:
                break
            time.sleep(0.1)
        assert gone, "session row should be deleted after successful purge"
    return client, database, session_id


def test_auto_purge_leaves_only_summary_and_metadata(tmp_path):
    client, database, session_id = run_to_purged(tmp_path)
    summaries = list((tmp_path / "data" / "summaries").glob("*.md"))
    assert len(summaries) == 1
    summary_path = summaries[0]
    assert not list((tmp_path / "data" / "summaries").glob("*.tmp"))
    # Temporary session tree is gone.
    assert not (tmp_path / "data" / "sessions" / session_id).exists()
    # All session content rows are gone.
    for table in CONTENT_TABLES:
        assert count_rows(database, table, session_id) == 0
    # The allowlisted metadata ledger is the only retained record.
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        columns = {r["name"] for r in connection.execute(
            "PRAGMA table_info(purged_session_metadata)"
        ).fetchall()}
    assert row is not None
    assert row["course_name"] == "Purge Course"
    assert row["status"] == "PURGED"
    assert row["summary_hash"] == hashlib.sha256(summary_path.read_bytes()).hexdigest()
    assert columns == ALLOWLIST_COLUMNS
    # The purged session is no longer offered as recoverable.
    listed = client.get("/api/recovery").json()["sessions"]
    assert all(s["id"] != session_id for s in listed)


def test_purge_refuses_wrong_state(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "X"}).json()
        session_id = view["session"]["id"]
    repository = SessionRepository(database)
    assert repository.get_session(session_id).status == SessionStatus.RECORDING
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    with pytest.raises(PurgeRefused, match="NO_VALID_SUMMARY"):
        cleanup.purge(session_id)
    # Nothing was deleted.
    assert (tmp_path / "data" / "sessions" / session_id).exists()


def test_purge_refuses_missing_summary_file(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "X"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    Path(record.summary_path).unlink()
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    with pytest.raises(PurgeRefused, match="NO_VALID_SUMMARY"):
        cleanup.purge(session_id)
    # Session content is fully retained after refusal.
    assert count_rows(database, "segments", session_id) > 0
    assert (tmp_path / "data" / "sessions" / session_id).exists()


def test_purge_refuses_tampered_summary(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "X"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    original = Path(record.summary_path).read_bytes()
    Path(record.summary_path).write_bytes(original + b"tampered")
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    with pytest.raises(PurgeRefused, match="hash mismatch"):
        cleanup.purge(session_id)
    assert count_rows(database, "segments", session_id) > 0


def test_manifest_rejects_symlink_inside_session_tree(tmp_path, database):
    import helpers_phase3 as h

    session_id = h.make_session(database)
    session_dir = tmp_path / "data" / "sessions" / session_id
    (session_dir / "capture" / session_id).mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "summaries", session_dir / "capture" / "link")
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    with pytest.raises(PurgeRefused, match="symlink"):
        cleanup.build_manifest(session_id)


def test_partial_purge_resumes_to_completion(tmp_path):
    """Crash after some deletions: re-running purge completes idempotently."""
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Partial"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    summary_bytes = Path(record.summary_path).read_bytes()

    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    repository.transition_session(
        session_id, SessionStatus.PURGING,
        reason_code="TEST_PARTIAL", actor="test",
    )
    # Simulate a partial pass: remove a few files, then "crash".
    session_dir = tmp_path / "data" / "sessions" / session_id
    files = sorted(p for p in session_dir.rglob("*") if p.is_file())
    assert files
    for path in files[:2]:
        path.unlink()
    receipt = cleanup.purge(session_id)
    assert receipt["outcome"] == "PURGED"
    assert not session_dir.exists()
    assert Path(record.summary_path).read_bytes() == summary_bytes
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 1


def test_duplicate_purge_after_completion_is_noop(tmp_path):
    client, database, session_id = run_to_purged(tmp_path)
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    receipt = cleanup.purge(session_id)
    assert receipt["outcome"] == "ALREADY_PURGED"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 1


def test_delete_active_recording_session_is_refused_without_data_loss(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Unfinished"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            view2 = client.get(f"/api/sessions/{session_id}").json()
            if view2["progress"]["segment_counts"].get("CAPTURED") or view2["progress"]["captured_audio_ms"] > 0:
                break
            time.sleep(0.1)
        response = client.delete(f"/api/recovery/{session_id}")
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "PURGE_REFUSED:ACTIVE_RUNTIME_OWNERSHIP: "
            "stop and enter recovery before deletion"
        )
        assert runtime.active_session == session_id
        assert SessionRepository(database).get_session(session_id).status is SessionStatus.RECORDING
    assert (tmp_path / "data" / "sessions" / session_id).exists()
    assert count_rows(database, "capture_chunks", session_id) > 0
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 0  # no summary ever existed


def test_delete_recovery_required_session_removes_files_and_rows(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Recoverable"}).json()
        session_id = view["session"]["id"]
        runtime.active_session = None  # model process loss: runtime no longer owns capture
        SessionRepository(database).transition_session(
            session_id,
            SessionStatus.RECOVERY_REQUIRED,
            reason_code="TEST_PROCESS_LOSS",
            actor="test",
        )
        response = client.delete(f"/api/recovery/{session_id}")
        assert response.status_code == 200
        assert response.json()["outcome"] == "DELETED"
    assert not (tmp_path / "data" / "sessions" / session_id).exists()
    for table in CONTENT_TABLES:
        assert count_rows(database, table, session_id) == 0


def test_delete_finalizing_session_is_refused_without_row_loss(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    repository = SessionRepository(database)
    with client:
        record = repository.create_session(
            "Finalizing", app_version="test"
        )
        repository.transition_session(
            record.id, SessionStatus.RECORDING,
            reason_code="TEST_START", actor="test",
        )
        repository.transition_session(
            record.id, SessionStatus.FINALIZING,
            reason_code="TEST_STOP", actor="test",
        )
        response = client.delete(f"/api/recovery/{record.id}")
        assert response.status_code == 409
        assert "ACTIVE_OR_IN_PROGRESS_SESSION" in response.json()["detail"]
    assert repository.get_session(record.id).status is SessionStatus.FINALIZING


def test_delete_refused_for_saved_summary(tmp_path):
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Saved"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
        response = client.delete(f"/api/recovery/{session_id}")
        assert response.status_code == 409
        assert "PURGE_REFUSED" in response.json()["detail"]
    # Content retained for the purge path.
    assert (tmp_path / "data" / "sessions" / session_id).exists()


def test_purge_journal_is_content_free(tmp_path):
    client, database, session_id = run_to_purged(tmp_path)
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT step, target, outcome FROM purge_journal WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    assert rows
    for row in rows:
        text = " ".join((row["step"], row["target"], row["outcome"]))
        assert "课程" not in text  # no classroom content in targets/outcomes
        assert ".md" not in row["target"]  # summaries never appear as targets


@pytest.mark.parametrize("boundary", ["mid_file_deletion", "before_row_finalization"])
def test_purge_resumes_after_interrupting_the_real_implementation(tmp_path, monkeypatch, boundary):
    """Interrupt the actual purge, then finish it from a fresh agent.

    The independent review judged `test_partial_purge_resumes_to_completion`
    only partially credible: it deletes files by hand and re-runs purge, so it
    proves missing-file idempotency but never interrupts the real
    implementation at a journal / files / rows boundary. This test injects the
    failure *inside* `CleanupAgent.purge` at each boundary, then resumes with a
    newly constructed agent (modelling a restarted process reading only durable
    state).

    Mutations that turn this red: finalize the metadata/rows before the files
    are provably gone; drop the `resume=` handling in `_load_or_skip_manifest`
    so a second pass cannot rebuild its work; or let the residue check pass
    with files still present.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Resume"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    assert record.status is SessionStatus.SUMMARY_SAVED
    summary_path = Path(record.summary_path)
    summary_bytes = summary_path.read_bytes()
    session_dir = tmp_path / "data" / "sessions" / session_id

    def make_agent():
        return CleanupAgent(
            database,
            summaries_root=tmp_path / "data" / "summaries",
            session_store_root=tmp_path / "data" / "sessions",
        )

    # ---- interrupt the real purge at the chosen boundary
    if boundary == "mid_file_deletion":
        real_unlink = Path.unlink
        calls = {"n": 0}

        def failing_unlink(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected crash mid file deletion")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
    else:
        def failing_finalize(self, *args, **kwargs):
            raise RuntimeError("injected crash before row finalization")

        monkeypatch.setattr(CleanupAgent, "_finalize_rows", failing_finalize)

    with pytest.raises(RuntimeError, match="injected crash"):
        make_agent().purge(session_id)

    monkeypatch.undo()

    # Nothing may be finalized yet: the ledger row must not exist and the
    # session row must survive, or a crash would have destroyed the evidence
    # needed to finish.
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"] == 1
    assert repository.get_session(session_id).status is SessionStatus.PURGING
    # The retained summary is untouched by the interrupted pass.
    assert summary_path.read_bytes() == summary_bytes

    # ---- resume from a fresh agent, as a restarted process would
    receipt = make_agent().purge(session_id)

    assert receipt["outcome"] == "PURGED"
    assert not session_dir.exists()
    assert summary_path.read_bytes() == summary_bytes
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 1
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"] == 0
    for table in CONTENT_TABLES:
        assert count_rows(database, table, session_id) == 0


def test_purge_refuses_to_finalize_while_manifest_files_remain(tmp_path, monkeypatch):
    """Purge must not finalize rows/ledger while manifest files still exist.

    Found while mutation-probing the purge-resume tests: deleting the explicit
    `residue` check in `CleanupAgent.purge` broke no test. Investigating that
    showed the check is in practice unreachable for files inside the session
    tree -- `_remove_dirs` hits `OSError: Directory not empty` first -- so the
    guard that actually holds this line today is an incidental OSError, not the
    domain refusal (see NEW-4 in the takeover report).

    This test therefore asserts the *invariant* rather than one exception type:
    whichever guard fires, purge must abort with content still on disk, no
    ledger row, the session row intact, and the session left resumable. A silent
    unlink failure must never let the session row disappear while capture audio
    and transcripts stay on disk -- content retained with no owning row to find
    it by.

    Mutation that turns this red: allow finalization to proceed when manifest
    files remain (remove both the residue check and the directory removal).
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Residue"}).json()
        session_id = view["session"]["id"]
        client.post(f"/api/sessions/{session_id}/end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status == "SUMMARY_SAVED":
                break
            time.sleep(0.1)
    repository = SessionRepository(database)
    assert repository.get_session(session_id).status is SessionStatus.SUMMARY_SAVED

    # Model a silent unlink failure: the call "succeeds" but the file survives.
    real_unlink = Path.unlink
    survivor = {"path": None}

    def lying_unlink(self, *args, **kwargs):
        if survivor["path"] is None:
            survivor["path"] = self
            return None  # pretend the delete worked
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", lying_unlink)
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    # Must be the domain refusal, not an incidental OSError from rmdir. Before
    # NEW-4 was fixed the residue check ran *after* `_remove_dirs`, so a
    # surviving file raised OSError("Directory not empty") first and this
    # guard was unreachable; asserting the exception type is what keeps the
    # corrected ordering from silently regressing.
    with pytest.raises(PurgeRefused, match="residue"):
        cleanup.purge(session_id)
    monkeypatch.undo()

    assert survivor["path"] is not None and survivor["path"].exists()
    # Fail closed: no ledger row, session row intact, still PURGING so a later
    # pass can finish the job.
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"] == 1
    assert repository.get_session(session_id).status is SessionStatus.PURGING

    # And a clean retry still completes.
    assert cleanup.purge(session_id)["outcome"] == "PURGED"


def _file_occurrences(path: Path, needles: tuple[str, ...]) -> dict[str, int]:
    """Count raw byte occurrences of each needle in a file.

    Reads the file as bytes, so it sees content in freed pages that SQL can no
    longer reach.
    """
    blob = path.read_bytes()
    return {n: blob.count(n.encode("utf-8")) for n in needles}


def test_purge_leaves_no_recoverable_content_in_the_database_file(tmp_path):
    """NO PURGE means unrecoverable, not merely unreachable by SQL.

    Deleting rows returns their pages to SQLite's freelist; by default
    (`secure_delete=0`, `auto_vacuum=0`, no VACUUM) the bytes stay in the file
    and are trivially recoverable with `strings`. The project's retention rule
    is that the only classroom content kept long-term is the final Markdown
    summary plus a content-free metadata ledger, so transcript and translation
    bytes surviving in free pages violate it even though every logical check
    passes.

    Mutations that turn this red: drop `PRAGMA secure_delete` from
    `Database.connect`, or remove the post-purge `VACUUM` in `CleanupAgent`.
    """
    client, database, session_id = run_to_purged(tmp_path)

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for table in CONTENT_TABLES:
        assert count_rows(database, table, session_id) == 0

    # The English ASR text and the Chinese translation the fake providers wrote.
    hits = _file_occurrences(database.path, ("Lecture point", "要点"))
    assert hits == {"Lecture point": 0, "要点": 0}, (
        "purged classroom content is still recoverable from the database file: "
        f"{hits}"
    )
    # The retained ledger must survive; this is not a test that wipes everything.
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"] == 1


def test_purge_reclaims_content_left_in_free_pages_by_earlier_versions(tmp_path):
    """Content already stranded in the freelist must also be reclaimed.

    `secure_delete` only zeroes rows deleted *after* it is switched on. A
    database written by an earlier build (or before D-039) already carries
    purged transcripts in free pages, and simply enabling the pragma leaves
    those bytes untouched forever. This models that upgrade path: content is
    deleted with `secure_delete = OFF`, exactly as an older version would, and
    a later successful purge must still leave nothing recoverable.

    Mutation that turns this red: remove the post-purge `VACUUM`.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Legacy"}).json()
        legacy_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{legacy_id}/end").status_code == 200
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if client.get(f"/api/sessions/{legacy_id}").json()["session"]["status"] == "SUMMARY_SAVED":
                break
            time.sleep(0.1)

    # Delete the legacy session's content the way an older build would: rows
    # dropped with secure_delete OFF, leaving the bytes in free pages.
    import sqlite3 as _sqlite3
    raw = _sqlite3.connect(database.path)
    raw.execute("PRAGMA secure_delete = OFF")
    raw.execute("PRAGMA foreign_keys = OFF")
    for table in CONTENT_TABLES:
        raw.execute(f"DELETE FROM {table} WHERE session_id = ?", (legacy_id,))
    raw.execute("DELETE FROM sessions WHERE id = ?", (legacy_id,))
    raw.commit()
    raw.close()

    stranded = _file_occurrences(database.path, ("Lecture point", "要点"))
    assert stranded["Lecture point"] > 0, (
        "precondition failed: this test must start with content stranded in "
        f"free pages, got {stranded}"
    )

    # A later, ordinary purge of a *different* session must also reclaim it.
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    cleanup.compact()

    hits = _file_occurrences(database.path, ("Lecture point", "要点"))
    assert hits == {"Lecture point": 0, "要点": 0}, (
        f"content stranded by an earlier version survived compaction: {hits}"
    )
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_deleted_rows_are_zeroed_without_relying_on_vacuum(tmp_path):
    """`secure_delete` alone must make a delete unrecoverable.

    Split out deliberately. The end-to-end purge test runs both defences
    (`secure_delete` on the connection and `compact()` afterwards), so removing
    either one still left it green -- the two mechanisms cover for each other
    and neither was actually pinned. This test exercises the pragma on its own:
    rows are deleted and the file is inspected with **no VACUUM**, so it can
    only pass if the delete itself zeroed the bytes.

    Why both defences are kept: this one keeps ordinary deletes clean without
    paying to rewrite the file, while `compact()` reclaims content stranded
    before the pragma existed. Neither subsumes the other.

    Mutation that turns this red: remove `PRAGMA secure_delete` from
    `Database.connect`.
    """
    from classroom_interpreter.storage import Database

    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    marker = "SECURE-DELETE-PROBE-Lecture-point-42"
    with database.connect() as connection:
        connection.execute("CREATE TABLE probe(x TEXT)")
        for _ in range(50):
            connection.execute("INSERT INTO probe VALUES (?)", (marker,))
        connection.commit()
    assert _file_occurrences(database.path, (marker,))[marker] > 0

    with database.connect() as connection:
        connection.execute("DELETE FROM probe")
        connection.commit()
    # Deliberately no VACUUM here.
    assert _file_occurrences(database.path, (marker,)) == {marker: 0}, (
        "deleted rows are still readable in the file; secure_delete is not "
        "taking effect on this connection"
    )


def test_compact_is_best_effort_and_never_fails_a_completed_purge(tmp_path):
    """A locked database must not turn a finished purge into a failure.

    `compact()` runs *after* the purge has been committed and recorded, so if
    VACUUM cannot get the lock the correct outcome is a content-free warning
    and recoverable bytes left for next time -- not an exception that makes a
    completed purge look failed to the caller.

    Mutation that turns this red: let the `sqlite3.Error` propagate out of
    `compact()`.
    """
    import sqlite3 as _sqlite3

    from classroom_interpreter.storage import Database

    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "summaries",
        session_store_root=tmp_path / "sessions",
    )
    blocker = _sqlite3.connect(database.path, timeout=1)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO agent_heartbeats(agent_name, instance_id, status, last_seen_at)"
            " VALUES ('blocker','1','ok',datetime('now'))"
        )
        cleanup.compact()  # must not raise
    finally:
        blocker.rollback()
        blocker.close()
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
