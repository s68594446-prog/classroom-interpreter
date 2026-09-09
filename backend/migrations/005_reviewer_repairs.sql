-- Migration 005: durable concurrent translation staging and first-attempt context.
CREATE TABLE translation_context_snapshots (
    segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    segment_sequence INTEGER NOT NULL CHECK (segment_sequence >= 0),
    prompt_version INTEGER NOT NULL CHECK (prompt_version > 0),
    context_json TEXT NOT NULL,
    context_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, segment_sequence),
    FOREIGN KEY (session_id, segment_sequence)
        REFERENCES segments(session_id, sequence) ON DELETE CASCADE
);

CREATE TABLE translation_staged_results (
    segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    segment_sequence INTEGER NOT NULL CHECK (segment_sequence >= 0),
    chinese_text TEXT NOT NULL CHECK (length(trim(chinese_text)) > 0),
    english_text_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_request_id TEXT,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    created_at TEXT NOT NULL,
    staged_at TEXT NOT NULL,
    UNIQUE (session_id, segment_sequence),
    FOREIGN KEY (session_id, segment_sequence)
        REFERENCES segments(session_id, sequence) ON DELETE CASCADE
);

CREATE INDEX idx_translation_staged_release
    ON translation_staged_results(session_id, segment_sequence);
