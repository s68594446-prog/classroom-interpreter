-- add-column-if-missing capture_chunks monotonic_clock_id
-- Separate migration upgrades either Phase 2 experimental v2 schema variant.
ALTER TABLE capture_chunks ADD COLUMN monotonic_clock_id TEXT;
