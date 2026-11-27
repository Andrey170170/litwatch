-- A CLI command's item as `serve` runs it (docs/plans/a2-work-gate.md, P5). Its progress comes
-- back as rows here, in order, each written in a unit fenced on the item's lease, which the
-- command reads as they arrive (`store.work.results`); its outcome is the item's `result`,
-- written in the unit that ends it; `cancel_asked` is the owner's cancel, which the supervisor
-- owning the item acts on. Additive: the image before ignores it.
CREATE TABLE work_results (
    id       bigserial PRIMARY KEY,
    work_id  bigint NOT NULL REFERENCES work_items (id),
    at       timestamptz NOT NULL DEFAULT now(),
    body     jsonb NOT NULL
);
CREATE INDEX work_results_item ON work_results (work_id, id);
ALTER TABLE work_items
    ADD COLUMN result jsonb,
    ADD COLUMN cancel_asked timestamptz;
