-- Fallback lists (docs/plans/failure-handling.md, P2; models.md, roles). A step every model of
-- its role refused stops at once: `refused` names the roles whose whole list refused (for a
-- Focus grading, the side; one side alone keeps the grading pending while the other answers).
-- A Focus grade one side refused throughout is marked one lab (pipeline.md, section 4). A
-- digest records the writer model that wrote it or, failed, the one that failed: the next
-- attempt of the topic starts one model further down.
ALTER TABLE step_failures ADD COLUMN refused text[] NOT NULL DEFAULT '{}';
ALTER TABLE fulltext_passes ADD COLUMN refused boolean NOT NULL DEFAULT false;
ALTER TABLE verdicts ADD COLUMN one_lab boolean NOT NULL DEFAULT false;
ALTER TABLE digests ADD COLUMN model text;
