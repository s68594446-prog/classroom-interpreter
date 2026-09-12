-- `last_error_*` keeps only the most recent error, and the most recent error is
-- usually the consequence rather than the cause. A capture fault at t+23min
-- stops the recording; ending the class then fails the summary, because the
-- transcript is incomplete -- and the summary failure overwrites the capture
-- fault. What the user is shown is "summary failed", which is true, useless,
-- and hides the only thing that explains the session.
--
-- The first error is kept alongside the last one. Both are content-free: a
-- category and a safe message, never transcript, audio, prompt or key material.
ALTER TABLE sessions ADD COLUMN first_error_category TEXT;
ALTER TABLE sessions ADD COLUMN first_error_safe_message TEXT;
