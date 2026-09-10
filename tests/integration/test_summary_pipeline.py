from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import pytest

from classroom_interpreter.agents.summary import (
    SummaryAgent,
    SummaryValidationError,
)
from classroom_interpreter.agents.cleanup import CleanupAgent, PurgeRefused
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockSummaryProvider
from classroom_interpreter.providers.summary_contract import REQUIRED_SECTIONS
from classroom_interpreter.domain.models import (
    ProviderError,
    ProviderErrorCategory,
    SessionStatus,
)
from classroom_interpreter.storage.repository import SessionRepository

from test_live_api import FakeASR, FakeListener, FakeTranslation, make_client, wait_for_status


def make_summary_agent(tmp_path: Path, database, provider=None) -> SummaryAgent:
    return SummaryAgent(
        database,
        provider or MockSummaryProvider(),
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
        chunk_token_budget=50,
    )


def run_flow_to_summary(tmp_path: Path, **kwargs):
    """Full pipeline: capture → ASR → translation → End Class → summary."""
    kwargs.setdefault("auto_purge", False)
    client, database, runtime = make_client(tmp_path, **kwargs)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Summary Course"}).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        deadline = time.monotonic() + 15
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/sessions/{session_id}").json()["session"]["status"]
            if status in ("SUMMARY_SAVED", "RECOVERY_REQUIRED"):
                break
            time.sleep(0.1)
        return client, database, runtime, session_id, status


def test_full_flow_reaches_summary_saved(tmp_path):
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    assert status == "SUMMARY_SAVED"
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    assert record.summary_path is not None
    path = Path(record.summary_path)
    assert path.is_file()
    assert not path.is_symlink()
    # Summary lives outside the purgeable session tree.
    assert not path.resolve().is_relative_to(
        (tmp_path / "data" / "sessions" / session_id).resolve()
    )
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == record.summary_hash
    text = content.decode("utf-8")
    assert text.startswith("# 课程名称：Summary Course")
    for index, title in enumerate(REQUIRED_SECTIONS):
        assert f"## {index + 1}. {title}" in text
    job = asyncio.run(_job_of(database, session_id))
    assert job["state"] == "SAVED"


async def _job_of(database, session_id):
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM summary_jobs WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row)


def test_summary_path_and_structure_of_saved_file(tmp_path):
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    text = Path(record.summary_path).read_text(encoding="utf-8")
    # Header fields precede the ten sections.
    header = text.split("##", 1)[0]
    assert "课程时长：" in header
    assert "日期：" in header


def test_invalid_synthesis_output_never_saves(tmp_path):
    calls = {"n": 0}

    def broken_synthesis(course_name, date, duration_text, chunk_summaries):
        calls["n"] += 1
        return "这不是一个有效的总结文档"

    provider = MockSummaryProvider(synthesis_output=broken_synthesis)
    client, database, runtime, session_id, status = run_flow_to_summary(
        tmp_path, summary_provider=provider
    )
    assert status == "RECOVERY_REQUIRED"
    with database.connect() as connection:
        saved = connection.execute(
            "SELECT summary_path, summary_saved_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert saved["summary_path"] is None
    assert saved["summary_saved_at"] is None
    job = asyncio.run(_job_of(database, session_id))
    assert job["state"] in ("SAVING", "SYNTHESIS", "FAILED")
    # No summary file may exist for the failed session.
    summaries = list((tmp_path / "data" / "summaries").glob("*.md"))
    assert summaries == []


def test_chunk_failure_prevents_summary(tmp_path):
    provider = MockSummaryProvider(
        chunk_outputs={
            0: ProviderFailure(
                ProviderError(
                    category=ProviderErrorCategory.AUTH,
                    retryable=False,
                    safe_message="provider rejected credentials",
                )
            )
        }
    )
    client, database, runtime, session_id, status = run_flow_to_summary(
        tmp_path, summary_provider=provider
    )
    assert status == "RECOVERY_REQUIRED"
    job = asyncio.run(_job_of(database, session_id))
    assert job["state"] == "FAILED"


def test_resume_reuses_completed_chunks(tmp_path):
    provider = MockSummaryProvider()
    client, database, runtime, session_id, status = run_flow_to_summary(
        tmp_path, summary_provider=provider
    )
    assert status == "SUMMARY_SAVED"
    chunk_calls_after_first = len(provider.chunk_calls)
    synth_calls_after_first = len(provider.synthesize_calls)

    # Re-running the summary on a SAVED session must verify, not regenerate.
    agent = make_summary_agent(tmp_path, database, provider)
    path = asyncio.run(agent.summarize_async(session_id))
    assert path.is_file()
    assert len(provider.chunk_calls) == chunk_calls_after_first
    assert len(provider.synthesize_calls) == synth_calls_after_first


def test_saved_summary_verification_detects_tampering(tmp_path):
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    assert status == "SUMMARY_SAVED"
    repository = SessionRepository(database)
    record = repository.get_session(session_id)
    Path(record.summary_path).write_text("tampered content", encoding="utf-8")
    agent = make_summary_agent(tmp_path, database)
    with pytest.raises(SummaryValidationError):
        asyncio.run(agent.summarize_async(session_id))


def test_summary_requires_drained_segments(tmp_path, database):
    """verify_drained refuses summary while any segment is not DONE."""
    import helpers_phase3 as h
    from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig

    session_id = h.make_session(database)
    blocks = h.envelope_blocks([("speech", 3.0), ("silence", 1.0)])
    h.write_chunks(database, tmp_path / "capture" / session_id, session_id, blocks)
    segmenter = SegmentAgent(
        database,
        tmp_path / "sessions" / session_id,
        session_id,
        SegmenterConfig(),
    )
    segmenter.process_available_chunks()
    segmenter.seal_tail()
    # Segment exists but has no ASR/translation yet: state is CAPTURED.
    agent = make_summary_agent(tmp_path, database)
    with pytest.raises(SummaryValidationError):
        agent.verify_drained(session_id)


def test_summary_write_is_atomic_no_temp_residue(tmp_path):
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    summaries = tmp_path / "data" / "summaries"
    assert status == "SUMMARY_SAVED"
    assert not list(summaries.glob("*.tmp"))
    assert len(list(summaries.glob("*.md"))) == 1


def test_summary_metadata_and_saved_state_roll_back_together_on_crash_boundary(tmp_path):
    """Inject a DB abort after the session UPDATE but before the audit insert.

    If metadata/state were separate commits, this leaves a false SUMMARY_SAVED.
    With one transaction, SQLite rolls the UPDATE back and purge stays closed.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post(
            "/api/sessions", json={"course_name": "Atomic Summary"}
        ).json()
        session_id = view["session"]["id"]
        with database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER inject_summary_commit_crash
                BEFORE INSERT ON state_transition_audits
                WHEN NEW.to_state = 'SUMMARY_SAVED'
                BEGIN
                    SELECT RAISE(ABORT, 'injected summary commit crash');
                END
                """
            )
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            record = SessionRepository(database).get_session(session_id)
            if record.status is SessionStatus.RECOVERY_REQUIRED:
                break
            time.sleep(0.05)

    record = SessionRepository(database).get_session(session_id)
    assert record.status is SessionStatus.RECOVERY_REQUIRED
    assert record.summary_path is None
    assert record.summary_hash is None
    assert len(list((tmp_path / "data" / "summaries").glob("*.md"))) == 1
    cleanup = CleanupAgent(
        database,
        summaries_root=tmp_path / "data" / "summaries",
        session_store_root=tmp_path / "data" / "sessions",
    )
    with pytest.raises(PurgeRefused, match="NO_VALID_SUMMARY"):
        cleanup.purge(session_id)


def test_summary_retry_recovers_temp_left_by_crash_before_rename(tmp_path, monkeypatch):
    import classroom_interpreter.agents.summary as summary_module

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    original_replace = summary_module._replace_summary_file
    injected = {"raised": False}

    def crash_before_rename(source, destination):
        if str(source).endswith(".md.tmp") and not injected["raised"]:
            injected["raised"] = True
            raise RuntimeError("injected crash before summary rename")
        return original_replace(source, destination)

    monkeypatch.setattr(summary_module, "_replace_summary_file", crash_before_rename)
    with client:
        view = client.post(
            "/api/sessions", json={"course_name": "Temp Crash"}
        ).json()
        session_id = view["session"]["id"]
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 200
        failed = wait_for_status(client, session_id, ("RECOVERY_REQUIRED",))
        assert failed["session"]["status"] == "RECOVERY_REQUIRED"
        temporary_files = list((tmp_path / "data" / "summaries").glob("*.md.tmp"))
        assert len(temporary_files) == 1

        monkeypatch.setattr(summary_module, "_replace_summary_file", original_replace)
        resumed = client.post(f"/api/recovery/{session_id}/resume")
        assert resumed.status_code == 200
        saved = wait_for_status(
            client, session_id, ("SUMMARY_SAVED", "RECOVERY_REQUIRED")
        )
        assert saved["session"]["status"] == "SUMMARY_SAVED"

    assert injected["raised"]
    assert not list((tmp_path / "data" / "summaries").glob("*.tmp"))
    record = SessionRepository(database).get_session(session_id)
    assert record.summary_path is not None and Path(record.summary_path).is_file()


def test_summary_temp_path_that_is_a_symlink_fails_closed(tmp_path, database):
    """A `.md.tmp` symlink must abort the write, not be followed.

    Issue 8's fix deletes a leftover regular temp file before retrying. The
    dangerous variant is a temp path that is a *symlink*: unlinking is fine,
    but opening or overwriting it would let anything that can create that
    name redirect a summary write to an arbitrary file. The guard must fail
    closed and leave the symlink's target untouched.

    Mutation that turns this red: drop the `temporary.is_symlink()` check --
    the unlink-then-`xb` path then quietly removes the symlink and the write
    succeeds, so no error is raised.
    """
    import os

    agent = make_summary_agent(tmp_path, database)
    summaries_root = tmp_path / "data" / "summaries"
    summaries_root.mkdir(parents=True, exist_ok=True)

    session_id = SessionRepository(database).create_session(
        "Symlink Probe", app_version="test"
    ).id
    final_path = agent._summary_path(session_id, "Symlink Probe")
    temporary = final_path.with_suffix(".md.tmp")

    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    os.symlink(victim, temporary)
    assert temporary.is_symlink()

    document = "# 课程名称：Symlink Probe\n\n" + "\n\n".join(
        f"## {index + 1}. {title}\n\n- probe" for index, title in enumerate(REQUIRED_SECTIONS)
    )

    with pytest.raises(SummaryValidationError, match="symlink"):
        agent._atomic_write(session_id, "Symlink Probe", document)

    # Fail closed: the symlink target is untouched and no summary was created.
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert temporary.is_symlink()
    assert not final_path.exists()
