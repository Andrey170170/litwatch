-- The reading side: topics' order and colour, takes, what is due for the writer and the next
-- issue, and the issues themselves.

-- The owner's order (sections, cards, the Papers table's take column) and each topic's colour.
ALTER TABLE topics ADD COLUMN position integer;
ALTER TABLE topics ADD COLUMN colour text;
UPDATE topics t SET position = s.n,
       colour = (ARRAY['#2f5d50', '#8a3b2e', '#3b4f8a', '#a2661e',
                       '#5a6b3a', '#7a4f8a', '#2f6b7a', '#8a6a3b'])[((s.n - 1) % 8) + 1]
FROM (SELECT slug, row_number() OVER (
          ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], kind), slug) AS n
      FROM topics) s
WHERE s.slug = t.slug;

-- Litwatch's own words on a paper for a Watch topic. Every take written is its own row, bound
-- to the text, rubric and grade it was written for; an issue points at the one it showed.
CREATE TABLE takes (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic           text NOT NULL,
    paper_revision  integer NOT NULL,
    topic_revision  integer NOT NULL,
    grade           text NOT NULL CHECK (grade IN ('must_read', 'relevant')),
    source          text NOT NULL CHECK (source IN ('verdict', 'evaluation', 'migrated', 'owner')),
    text            text NOT NULL,
    written_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX takes_paper_topic_idx ON takes (paper_id, topic, id DESC);

-- A (paper, Watch topic) pair the writer or the next daily issue still owes something:
-- a take, an entry, or a count in the fold line (an adjacent arrival).
CREATE TABLE due (
    paper_id     text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic        text NOT NULL,
    for_take     boolean NOT NULL DEFAULT false,
    for_issue    boolean NOT NULL DEFAULT false,
    fold         boolean NOT NULL DEFAULT false,
    failed_runs  integer NOT NULL DEFAULT 0,      -- the writer's, for this pair
    last_error   text,
    since        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic)
);

-- One daily issue per calendar date, one weekly issue per week (keyed by its Monday).
CREATE TABLE issues (
    id        bigserial PRIMARY KEY,
    kind      text NOT NULL CHECK (kind IN ('daily', 'weekly')),
    day       date NOT NULL,
    lede      text,
    folds     jsonb NOT NULL DEFAULT '{}',        -- topic slug -> adjacent arrivals
    built_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, day)
);

-- An issue is a record: entries stay as they were the day it was built.
CREATE TABLE issue_entries (
    issue_id  bigint NOT NULL REFERENCES issues (id) ON DELETE CASCADE,
    paper_id  text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic     text NOT NULL,
    grade     text NOT NULL CHECK (grade IN ('must_read', 'relevant')),
    take_id   bigint REFERENCES takes (id),        -- NULL when the writer gave up on it
    position  integer NOT NULL,
    PRIMARY KEY (issue_id, paper_id, topic)
);
CREATE INDEX issue_entries_pair_idx ON issue_entries (paper_id, topic);
