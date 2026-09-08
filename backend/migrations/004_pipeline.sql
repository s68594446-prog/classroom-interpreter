-- Migration 004: processing pipeline support (Phase 3+).
-- pipeline_cursors: durable consumer position + exact VAD resume state so a
-- restarted segmenter continues without re-sealing or duplicating segments.
-- summary_chunks / purge_journal are schema preparation for Phases 6/7; unused
-- until their phases implement behavior.
CREATE TABLE pipeline_cursors (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    next_chunk_sequence INTEGER NOT NULL DEFAULT 0 CHECK (next_chunk_sequence >= 0),
    consumed_native_frames INTEGER NOT NULL DEFAULT 0 CHECK (consumed_native_frames >= 0),
    consumed_normalized_frames INTEGER NOT NULL DEFAULT 0 CHECK (consumed_normalized_frames >= 0),
    in_utterance INTEGER NOT NULL DEFAULT 0 CHECK (in_utterance IN (0, 1)),
    voiced_run INTEGER NOT NULL DEFAULT 0 CHECK (voiced_run >= 0),
    silence_run INTEGER NOT NULL DEFAULT 0 CHECK (silence_run >= 0),
    utterance_voiced_frames INTEGER NOT NULL DEFAULT 0 CHECK (utterance_voiced_frames >= 0),
    utterance_start_ms INTEGER NOT NULL DEFAULT 0 CHECK (utterance_start_ms >= 0),
    last_voiced_end_ms INTEGER NOT NULL DEFAULT 0 CHECK (last_voiced_end_ms >= 0),
    current_overlap_ms INTEGER NOT NULL DEFAULT 0 CHECK (current_overlap_ms >= 0),
    current_epoch INTEGER NOT NULL DEFAULT 0 CHECK (current_epoch >= 0),
    pending_pcm BLOB NOT NULL DEFAULT x'',
    preroll_pcm BLOB NOT NULL DEFAULT x'',
    carry_pcm BLOB NOT NULL DEFAULT x'',
    discarded_utterances INTEGER NOT NULL DEFAULT 0 CHECK (discarded_utterances >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE summary_chunks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES summary_jobs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    start_sequence INTEGER NOT NULL CHECK (start_sequence >= 0),
    end_sequence INTEGER NOT NULL CHECK (end_sequence >= start_sequence),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    section_summary TEXT,
    provider TEXT,
    model TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, chunk_index)
);

CREATE TABLE purge_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, step, target)
);
