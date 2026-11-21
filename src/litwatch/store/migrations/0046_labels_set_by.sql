-- Who last wrote a label's value: the owner on a page or in Calibrate (`reader`), or a miss
-- report (`miss`). `origin` keeps how the paper was first chosen, which Calibrate and the
-- cohorts read; `set_by` is what set the word now, which the paper's rail names. Labels from
-- before are taken to be set as their origin says: a miss's by the miss, the rest by the owner.
ALTER TABLE labels ADD COLUMN set_by text NOT NULL DEFAULT 'reader'
    CHECK (set_by IN ('reader', 'miss'));
UPDATE labels SET set_by = 'miss' WHERE origin = 'miss';
