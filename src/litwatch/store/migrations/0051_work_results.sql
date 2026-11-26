-- A CLI command's results (docs/plans/a2-work-gate.md, P5): the command writes its work item
-- and `serve` runs it; what the command prints and the lines of its output files come back as
-- rows here, in order, each written in a unit fenced on the item's lease, which the command
-- reads as they arrive (`store.work.results`). Additive: the image before ignores it.
CREATE TABLE work_results (
    id       bigserial PRIMARY KEY,
    work_id  bigint NOT NULL REFERENCES work_items (id),
    at       timestamptz NOT NULL DEFAULT now(),
    body     jsonb NOT NULL
);
CREATE INDEX work_results_item ON work_results (work_id, id);
