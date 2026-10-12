-- Papers triage keeps failing on (spec decision 24): after three failed nights triage stops
-- and the paper waits for the owner, who dismisses it, triages it by hand, or fixes its text.

ALTER TABLE triage DROP CONSTRAINT triage_state_check;
ALTER TABLE triage ADD CONSTRAINT triage_state_check
    CHECK (state IN ('done', 'failed', 'migrated', 'dismissed'));

-- Triage by hand: the owner's answers put the paper forward.
ALTER TABLE candidates ADD COLUMN by_owner boolean NOT NULL DEFAULT false;

-- When the owner corrected the title or abstract. The correction stands until intake records
-- a newer arXiv version; refetching the same version keeps it.
ALTER TABLE papers ADD COLUMN corrected_at timestamptz;
