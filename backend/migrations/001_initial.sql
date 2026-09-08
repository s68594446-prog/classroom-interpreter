CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    course_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'NEW', 'RECORDING', 'PAUSED', 'FINALIZING', 'SUMMARIZING',
        'SUMMARY_SAVED', 'PURGING', 'PURGED', 'RECOVERY_REQUIRED'
    )),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    summary_path TEXT,
    summary_hash TEXT,
    summary_model TEXT,
    summary_saved_at TEXT,
    app_version TEXT NOT NULL,
    purged_at TEXT,
    last_error_category TEXT,
    last_error_safe_message TEXT,
    next_translation_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_translation_sequence >= 0),
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE purged_session_metadata (
    session_id TEXT PRIMARY KEY,
    course_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    summary_path TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    summary_model TEXT NOT NULL,
    app_version TEXT NOT NULL,
    purged_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PURGED' CHECK (status = 'PURGED')
);

CREATE TABLE capture_chunks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    capture_epoch INTEGER NOT NULL CHECK (capture_epoch >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    path TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    sha256 TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE TABLE segments (
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
        'TRANSLATION_FAILED', 'DONE'
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

CREATE TABLE asr_results (
    segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    segment_sequence INTEGER NOT NULL,
    english_text TEXT NOT NULL CHECK (length(trim(english_text)) > 0),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_request_id TEXT,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    audio_seconds REAL NOT NULL CHECK (audio_seconds >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, segment_sequence),
    FOREIGN KEY (session_id, segment_sequence)
        REFERENCES segments(session_id, sequence) ON DELETE CASCADE
);

CREATE TABLE translation_results (
    segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    segment_sequence INTEGER NOT NULL,
    chinese_text TEXT NOT NULL CHECK (length(trim(chinese_text)) > 0),
    english_text_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_request_id TEXT,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, segment_sequence),
    FOREIGN KEY (session_id, segment_sequence)
        REFERENCES segments(session_id, sequence) ON DELETE CASCADE
);

CREATE TABLE processing_attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('ASR', 'TRANSLATION', 'SUMMARY')),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_category TEXT,
    http_status INTEGER,
    provider_request_id TEXT,
    UNIQUE (segment_id, stage, attempt_number)
);

CREATE TABLE summary_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    completed_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_chunk_count >= 0),
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TEXT,
    last_error_category TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE agent_heartbeats (
    agent_name TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    safe_detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    metric_name TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE state_transition_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE websocket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_sequence INTEGER NOT NULL CHECK (event_sequence >= 0),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, event_sequence)
);

CREATE INDEX idx_segments_claim
    ON segments(state, asr_next_attempt_at, translation_next_attempt_at, lease_expires_at);
CREATE INDEX idx_segments_session_state
    ON segments(session_id, state, sequence);
CREATE INDEX idx_attempts_session_stage
    ON processing_attempts(session_id, stage, started_at);
CREATE INDEX idx_metrics_name_time
    ON metric_samples(metric_name, recorded_at);
