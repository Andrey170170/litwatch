-- Radar and Pulse reviews, Watch recaps, and how often a topic is reviewed.

-- Every Radar and Pulse topic is reviewed weekly; a Radar topic with cadence `daily` is also
-- reviewed after each nightly run (spec decision 23).
ALTER TABLE topics ADD COLUMN cadence text NOT NULL DEFAULT 'weekly'
    CHECK (cadence IN ('weekly', 'daily'));

-- A piece of writing for one topic and one period: a Radar or Pulse review (themes and
-- picks), or a Watch topic's weekly recap. The period is a day, or a week keyed by its
-- Monday. A failed attempt is a row too, so its issue can say so; a re-run replaces it.
CREATE TABLE reviews (
    id              bigserial PRIMARY KEY,
    topic           text NOT NULL,
    period          text NOT NULL CHECK (period IN ('day', 'week')),
    day             date NOT NULL,
    topic_revision  integer,
    text            text,
    state           text NOT NULL CHECK (state IN ('written', 'failed', 'quiet')),
    error           text,
    members         integer NOT NULL DEFAULT 0,   -- papers it read
    written_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (topic, period, day)
);

-- The papers a review picked, in its order, each with a sentence on why.
CREATE TABLE review_picks (
    review_id  bigint NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    paper_id   text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    position   integer NOT NULL,
    why        text NOT NULL,
    PRIMARY KEY (review_id, paper_id)
);
CREATE INDEX review_picks_paper_idx ON review_picks (paper_id);
