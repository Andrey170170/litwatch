-- Calibrate (spec §8 M3, decision 20): sessions in which the owner gives their word on
-- purpose, the appraisals and take ratings they give there, and what was drawn for each.

CREATE TABLE calibration_sessions (
    id        bigserial PRIMARY KEY,
    seed      bigint NOT NULL,
    drawn_at  timestamptz NOT NULL DEFAULT now()
);

-- One paper of a session. A drawn paper was drawn for one Watch topic and one group (at or
-- above relevant, or below), and keeps the verdict current at the draw: precision judges
-- that verdict, not a later re-grade. Papers taken in afterwards say why.
CREATE TABLE calibration_items (
    session_id  bigint NOT NULL REFERENCES calibration_sessions (id) ON DELETE CASCADE,
    position    integer NOT NULL,
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic       text NOT NULL,
    why         text NOT NULL CHECK (why IN ('above', 'below', 'split', 'reaction')),
    verdict_id  bigint REFERENCES verdicts (id) ON DELETE SET NULL,
    -- The fit given here for the drawn topic: precision reads it, whatever label the paper
    -- had before or gets after.
    answer      text CHECK (answer IN ('relevant', 'adjacent', 'not_relevant', 'unsure')),
    answered_at timestamptz,
    PRIMARY KEY (session_id, position),
    UNIQUE (session_id, paper_id)
);
CREATE INDEX calibration_items_drawn_idx ON calibration_items (topic, why);

-- The owner's view of a paper whatever the topic. The newest one counts.
CREATE TABLE appraisals (
    id        bigserial PRIMARY KEY,
    paper_id  text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    value     text CHECK (value IN ('matters', 'fine', 'not_for_me')),
    note      text,
    at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX appraisals_paper_idx ON appraisals (paper_id, id DESC);

-- Whether a take says what the paper does, as the owner read it.
CREATE TABLE take_ratings (
    take_id   bigint PRIMARY KEY REFERENCES takes (id) ON DELETE CASCADE,
    accurate  boolean NOT NULL,
    at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
