-- Migration 006: per-course glossary terms.
--
-- Why this exists: lecture audio is dense with domain vocabulary that generic
-- ASR gets subtly wrong -- measured on synthesized finance/econometrics audio,
-- the residual error was almost entirely term spelling ("heteroscedasticity"
-- for "heteroskedasticity") rather than misheard speech. Those errors land in
-- the English transcript, which is the factual source for the summary, so they
-- propagate into the translation and the final notes.
--
-- Terms are keyed by course_name, not session_id: the same course recurs weekly
-- and its vocabulary should carry over without re-entry. A glossary is user
-- configuration, NOT classroom content -- it holds terms the user typed, never
-- anything derived from a recording -- so it deliberately survives purge and is
-- not listed in CONTENT_TABLES.
CREATE TABLE glossary_terms (
    id TEXT PRIMARY KEY,
    course_name TEXT NOT NULL,
    term TEXT NOT NULL CHECK (length(trim(term)) > 0),
    -- Optional preferred Chinese rendering. When set, the translator is told to
    -- use exactly this, which keeps terminology stable across a whole course
    -- instead of drifting between synonyms segment to segment.
    preferred_chinese TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (course_name, term)
);

CREATE INDEX idx_glossary_course ON glossary_terms(course_name);
