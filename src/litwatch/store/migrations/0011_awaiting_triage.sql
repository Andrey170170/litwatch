-- Papers triage keeps failing on (spec decision 24): after three failed nights triage stops
-- and the paper waits for the owner, who dismisses it, triages it by hand, or fixes its text.

-- A dismissed paper is dropped by litwatch whatever happens to its text later: it is never
-- triaged or graded again. It stays in the collection and the archive.
ALTER TABLE papers ADD COLUMN dismissed_at timestamptz;

-- Triage by hand: the owner's answers put the paper forward.
ALTER TABLE candidates ADD COLUMN by_owner boolean NOT NULL DEFAULT false;

-- When the owner corrected the title or abstract, and a fingerprint of the text arXiv had then.
-- The correction stands while arXiv keeps sending that text; different text replaces it,
-- whatever the version numbers say (they can be unknown).
ALTER TABLE papers ADD COLUMN corrected_at timestamptz, ADD COLUMN corrected_from text;
