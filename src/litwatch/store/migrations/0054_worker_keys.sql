-- What a process serves of a kind it serves for some keys alone (docs/plans/a2-work-gate.md, P7):
-- `keys` maps such a kind to its keys, as its gate was given them (`serve`'s deliveries, by
-- integration); a kind not in it is served for every key. An event wakes the deliveries a row
-- lists (`store.outbox.wake_all`), so a turn is made only for an integration a `serve` delivers.
-- Additive: the image before ignores it.
ALTER TABLE workers ADD COLUMN keys jsonb NOT NULL DEFAULT '{}';
