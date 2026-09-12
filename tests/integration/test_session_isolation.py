"""One class's captions must never appear inside another class's record.

Both worker claims were global by design -- `claim_next_asr_segment` and
`claim_next_translation_segment` took "the next eligible segment across all
sessions", which is what makes a crashed session drainable later. The event
sink was not: it published whatever a worker produced under
`orchestrator.active_session`, whatever session the segment actually came from.

The two halves disagreed, and an abandoned class is the normal case -- any
session that ended in RECOVERY_REQUIRED leaves segments behind. The moment a
new class started, its workers picked that leftover work up and published it
into the *new* class's live transcript and into `websocket_events`, which is
the durable replay log. The reader saw last week's sentences appear
mid-lecture, and a reconnect replayed them again.

Reproduced before the fix: one staged sentence belonging to an abandoned
session was released and written into the live session's event log, with the
abandoned session's own log left empty.

Two independent guards, tested separately:

1. every event is published under the session its row belongs to;
2. a worker built for one class does not claim another class's work at all,
   so the live lecture never queues behind an abandoned backlog.
"""

from __future__ import annotations

import asyncio

from classroom_interpreter.agents.segmentation import SegmentAgent, SegmenterConfig
from classroom_interpreter.agents.transcription import TranscriptionAgent
from classroom_interpreter.agents.translation import TranslationSequencer
from classroom_interpreter.runtime.pipeline import SessionOrchestrator
from classroom_interpreter.storage import Database, SessionRepository

import helpers_phase3 as h
from test_live_api import FakeASR, FakeTranslation, make_settings
from test_translation_pipeline import build_pipeline


def two_sessions(tmp_path):
    """An abandoned class with work still queued, plus a fresh live class."""
    settings = make_settings(tmp_path)
    database = Database(tmp_path / "runtime.sqlite3")
    database.migrate()
    abandoned = h.make_session(database, "Last week")
    live = SessionRepository(database).create_session("Today", app_version="test").id
    runtime = SessionOrchestrator(
        settings,
        database,
        asr_provider=FakeASR(),
        translation_provider=FakeTranslation(),
    )
    runtime.active_session = live
    runtime._build_workers(live)
    return database, runtime, abandoned, live


def capture_segments(database, tmp_path, session_id, utterances=1):
    blocks = h.envelope_blocks(
        [("silence", 0.5), ("speech", 2.5), ("silence", 1.0)] * utterances
    )
    h.write_chunks(
        database, tmp_path / "capture" / session_id, session_id, blocks
    )
    segmenter = SegmentAgent(
        database, tmp_path / "sessions" / session_id, session_id, SegmenterConfig()
    )
    segmenter.process_available_chunks()
    segmenter.seal_tail()


def published(database, session_id, kind):
    with database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS n FROM websocket_events "
            "WHERE session_id = ? AND kind = ?",
            (session_id, kind),
        ).fetchone()["n"]


# ------------------------------------------------- guard 1: event routing


def test_a_released_translation_is_published_under_its_own_session(tmp_path):
    """Mutation that turns this red: drop the `released["session_id"]`
    argument in `TranslationSequencer.step` so `_event_sink` falls back to
    `self.active_session`.

    The sequencer here is deliberately unscoped, which is how the offline
    drain tools run it: routing must be correct on its own, not only because
    the claim happens to be confined.
    """
    database, runtime, abandoned, live = two_sessions(tmp_path)
    agent = build_pipeline(database, tmp_path, abandoned, utterances=1)
    while asyncio.run(agent.step()):
        pass  # staged durably, deliberately never released
    sequencer = TranslationSequencer(database, event_sink=runtime._event_sink)

    assert asyncio.run(sequencer.step()) is True

    assert published(database, abandoned, "subtitle.committed") == 1
    assert published(database, live, "subtitle.committed") == 0, (
        "a caption from an abandoned class was written into the live class's "
        "durable event log"
    )


def test_a_committed_transcript_is_published_under_its_own_session(tmp_path):
    """Mutation that turns this red: drop the `segment.session_id` argument in
    `TranscriptionAgent._process_claim`'s `event_sink` call."""
    database, runtime, abandoned, live = two_sessions(tmp_path)
    capture_segments(database, tmp_path, abandoned)
    worker = TranscriptionAgent(
        database, FakeASR(), worker_id="unscoped", event_sink=runtime._event_sink
    )

    assert asyncio.run(worker.step()) is True

    assert published(database, abandoned, "subtitle.current") == 1
    assert published(database, live, "subtitle.current") == 0, (
        "an English caption from an abandoned class was written into the live "
        "class's durable event log"
    )


# ------------------------------------------------- guard 2: claim scoping


def test_the_live_class_workers_ignore_an_abandoned_class(tmp_path):
    """Mutation that turns this red: stop passing `session_id=session_id` to
    the workers in `_build_workers`.

    Without it the live lecture's ASR worker spends its first calls -- and the
    user's money -- transcribing audio from a class nobody is watching, while
    the sentence being spoken right now waits behind it.
    """
    database, runtime, abandoned, live = two_sessions(tmp_path)
    capture_segments(database, tmp_path, abandoned, utterances=2)

    assert asyncio.run(runtime._asr_workers[0].step()) is False, (
        "the live class's ASR worker claimed an abandoned class's segment"
    )
    # Now take the abandoned class all the way to a staged, unreleased
    # translation, so the live sequencer has something it could wrongly
    # release.
    agent = build_pipeline(database, tmp_path, abandoned, utterances=1)
    while asyncio.run(agent.step()):
        pass
    assert asyncio.run(runtime._translation_workers[0].step()) is False
    assert asyncio.run(runtime._translation_sequencer.step()) is False, (
        "the live class's sequencer released an abandoned class's caption"
    )


def test_scoping_does_not_strand_the_class_being_recovered(tmp_path):
    """The confinement must follow whichever class the runtime is working on,
    or recovery could never drain.

    Mutation that turns this red: hard-code `session_id=None` -> some other
    session, or scope the workers to anything but the session `_build_workers`
    was given.
    """
    database, runtime, abandoned, live = two_sessions(tmp_path)
    capture_segments(database, tmp_path, abandoned)
    runtime.active_session = abandoned
    runtime._build_workers(abandoned)

    assert asyncio.run(runtime._asr_workers[0].step()) is True
    runtime._caption_gate.flush()
    assert published(database, abandoned, "subtitle.current") == 1
    assert published(database, live, "subtitle.current") == 0
