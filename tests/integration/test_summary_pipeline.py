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
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockSummaryProvider
from classroom_interpreter.providers.summary_contract import REQUIRED_SECTIONS
from classroom_interpreter.domain.models import (
    ProviderError,
    ProviderErrorCategory,
    SessionStatus,
)
from classroom_interpreter.storage.repository import SessionRepository

from test_live_api import FakeASR, FakeListener, FakeTranslation, make_client


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
