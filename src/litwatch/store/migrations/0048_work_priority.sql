-- A work item's place in line among those due (docs/plans/a2-work-gate.md, P2): `take` runs the
-- due item of the lowest `priority` first, then the one due longest (`not_before`), so how
-- urgent an item is stays apart from when it may run. A pass's item carries its pass's priority,
-- the owner's first; other kinds leave it 0. A resubmission keeps the more urgent of the two.
ALTER TABLE work_items ADD COLUMN priority integer NOT NULL DEFAULT 0;
