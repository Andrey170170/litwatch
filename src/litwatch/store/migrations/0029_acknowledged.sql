-- Acknowledge on Needs you (docs/specs/v1/DESIGN.md, Needs you; decision 72): the owner has seen
-- a stuck step, a full-text pass waiting on them or a failed digest, and it stops counting in the
-- run status. The mark is cleared when the item fails again after recovering.
ALTER TABLE step_failures ADD COLUMN acknowledged_at timestamptz;
ALTER TABLE fulltext_passes ADD COLUMN acknowledged_at timestamptz;
ALTER TABLE digests ADD COLUMN acknowledged_at timestamptz;
