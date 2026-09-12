-- The segmenter's voice-activity threshold was a fixed absolute RMS (300),
-- calibrated on clean file-fed audio. Measured against a real microphone in a
-- real room over a 60-minute lecture: a qualifying 700ms silence gap appeared
-- once every 36 seconds, which is longer than the 30s segment cap -- so
-- segments almost never closed on silence and instead ran to the ceiling
-- (observed p50 segment duration 29.0s against a 6s preferred length).
--
-- The threshold is now derived from a running estimate of the room's noise
-- floor. That estimate is part of the segmenter's exact resume state: without
-- it a restarted segmenter would re-derive the region after the last
-- checkpoint with a different threshold and could cut different segments,
-- breaking the crash-resume reproducibility guarantee.
ALTER TABLE pipeline_cursors
    ADD COLUMN noise_floor_rms REAL NOT NULL DEFAULT 0;
