-- Fallback lists (docs/plans/failure-handling.md, P2; models.md, roles). A step every model of
-- its role refused stops at once: `refused` names the roles whose whole list refused (for a
-- Focus grading, the side; one side alone keeps the grading pending while the other answers),
-- and `stopped_by_refusal` says that refusal, not three failed runs, stopped it. A full-text
-- pass's `unusable_runs` are its unusable answers in a row, apart from runs that found no text:
-- the next try starts that far down `brief`'s list. A Focus grade one side refused throughout
-- is marked one lab (pipeline.md, section 4). A digest records the writer model that wrote it
-- or, failed, the one its attempt started from: the next attempt of the topic starts one model
-- further down.
ALTER TABLE step_failures ADD COLUMN refused text[] NOT NULL DEFAULT '{}',
    ADD COLUMN stopped_by_refusal boolean NOT NULL DEFAULT false;
ALTER TABLE fulltext_passes ADD COLUMN refused boolean NOT NULL DEFAULT false,
    ADD COLUMN unusable_runs integer NOT NULL DEFAULT 0;
ALTER TABLE verdicts ADD COLUMN one_lab boolean NOT NULL DEFAULT false;
ALTER TABLE digests ADD COLUMN model text;
