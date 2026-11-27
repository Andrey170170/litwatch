-- Release links (docs/specs/architecture/a2-work-gate.md, "Release links"; docs/plans/a2-work-gate.md,
-- P6): a paper's released code and weights, with their access state, as its latest find answered
-- them. `version` is the arXiv version that find was for (null when unknown); `checked_at` when the
-- link was last fetched; `found_by` what set its state: the finder (an automatic find), the owner's
-- action, or a re-check. `rechecks` counts the re-checks a placeholder has had, which stop at four.
-- The finds themselves are work items of kind `links`, keyed by paper and version (an automatic
-- one, held for good) or fresh (the owner's), their paper in `scope`; the index finds a paper's.
-- Additive: the image before ignores it.
CREATE TABLE release_links (
    id          bigserial PRIMARY KEY,
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    version     integer,
    url         text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('code', 'weights')),
    state       text NOT NULL CHECK (state IN ('public', 'gated', 'placeholder', 'missing')),
    license     text,
    checked_at  timestamptz NOT NULL,
    found_by    text NOT NULL CHECK (found_by IN ('finder', 'owner', 'recheck')),
    rechecks    smallint NOT NULL DEFAULT 0,
    UNIQUE (paper_id, url, kind)
);
CREATE INDEX work_items_finds ON work_items ((scope ->> 'paper_id'), id) WHERE kind = 'links';
