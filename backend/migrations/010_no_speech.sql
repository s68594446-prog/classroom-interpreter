-- A segment the recogniser found no speech in is not a failure.
--
-- Measured on a live session: of six segments that reached ASR, two came back
-- with an empty transcript. Empty is not a transient error, so retrying it
-- five times bought five paid calls and five round trips of latency, and then
-- the segment landed in ASR_FAILED -- the only terminal state available.
--
-- ASR_FAILED is terminal *and* fatal to the class: the finalization drain
-- refuses to advance a session with any failed segment (D-006), and
-- `verify_drained` requires every segment to be DONE. So two seconds of
-- silence, correctly transcribed as nothing, destroyed the summary of the
-- entire lecture.
--
-- D-006 is right about what it was written for: a segment whose audio we could
-- not transcribe is lost content, and a class missing content must not be
-- summarised as if it were complete. Silence is not lost content. NO_SPEECH
-- separates the two, and it is terminal, excluded from the summary transcript
-- (which reads DONE only), and counted as neither pending nor failed.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt.
--
-- The first version of this file said "PRAGMA foreign_keys = OFF" here. That
-- line did nothing: the migration runner wraps every script in a transaction,
-- where SQLite ignores the pragma, so DROP TABLE cascaded and deleted every
-- transcript, translation and attempt record while keeping the segments. The
-- directive below is honoured by the runner before the transaction opens, and
-- the runner checks foreign keys before it commits.
-- requires-foreign-keys-off

CREATE TABLE segments_rebuilt (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    overlap_ms INTEGER NOT NULL DEFAULT 0 CHECK (overlap_ms >= 0),
    audio_path TEXT NOT NULL,
    audio_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'CAPTURED', 'ASR_PENDING', 'ASR_PROCESSING', 'ASR_DONE',
        'ASR_RETRY', 'ASR_FAILED', 'TRANSLATION_PENDING',
        'TRANSLATION_PROCESSING', 'TRANSLATION_RETRY',
        'TRANSLATION_FAILED', 'DONE', 'NO_SPEECH'
    )),
    lease_owner TEXT,
    lease_expires_at TEXT,
    asr_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (asr_retry_count >= 0),
    asr_next_attempt_at TEXT,
    translation_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (translation_retry_count >= 0),
    translation_next_attempt_at TEXT,
    last_error_category TEXT,
    last_error_safe_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

INSERT INTO segments_rebuilt SELECT
    id, session_id, sequence, start_ms, end_ms, duration_ms, overlap_ms,
    audio_path, audio_sha256, state, lease_owner, lease_expires_at,
    asr_retry_count, asr_next_attempt_at, translation_retry_count,
    translation_next_attempt_at, last_error_category, last_error_safe_message,
    created_at, updated_at
FROM segments;

DROP TABLE segments;
ALTER TABLE segments_rebuilt RENAME TO segments;

CREATE INDEX idx_segments_claim
    ON segments(state, asr_next_attempt_at, translation_next_attempt_at, lease_expires_at);
CREATE INDEX idx_segments_session_state
    ON segments(session_id, state, sequence);

