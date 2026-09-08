-- Legacy Phase 1 rows remain distinguishable by NULL Phase 2 fields.
ALTER TABLE capture_chunks ADD COLUMN start_monotonic_ns INTEGER;
ALTER TABLE capture_chunks ADD COLUMN end_monotonic_ns INTEGER;
ALTER TABLE capture_chunks ADD COLUMN wall_clock_start TEXT;
ALTER TABLE capture_chunks ADD COLUMN wall_clock_end TEXT;
ALTER TABLE capture_chunks ADD COLUMN sample_rate INTEGER;
ALTER TABLE capture_chunks ADD COLUMN channels INTEGER;
ALTER TABLE capture_chunks ADD COLUMN sample_format TEXT;
ALTER TABLE capture_chunks ADD COLUMN frame_count INTEGER;
ALTER TABLE capture_chunks ADD COLUMN status TEXT CHECK(status = 'AUDIO_CAPTURED');
CREATE TABLE capture_events (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('audio_gap','pause','fault','restart','orphan')),
    reason TEXT NOT NULL,
    start_monotonic_ns INTEGER NOT NULL,
    end_monotonic_ns INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
