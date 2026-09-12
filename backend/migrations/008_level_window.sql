-- The VAD threshold is now derived from a windowed minimum and maximum of the
-- frame level, replacing an exponentially-leaking noise-floor estimate that had
-- no upper bound. Over 54 seconds of real speech that leak drove the threshold
-- from 300 to 1743 -- above the speaker's own level -- and segmentation stopped
-- after the first sentence.
--
-- These are part of the segmenter's exact resume state for the same reason
-- noise_floor_rms was: a restarted segmenter re-derives the region after the
-- last checkpoint, and it must do so with the same threshold or it can seal
-- different segments than the first pass did.
ALTER TABLE pipeline_cursors ADD COLUMN level_block_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_cursors ADD COLUMN level_block_min REAL NOT NULL DEFAULT 0;
ALTER TABLE pipeline_cursors ADD COLUMN level_block_max REAL NOT NULL DEFAULT 0;
ALTER TABLE pipeline_cursors ADD COLUMN level_previous_min REAL NOT NULL DEFAULT 0;
ALTER TABLE pipeline_cursors ADD COLUMN level_previous_max REAL NOT NULL DEFAULT 0;
