-- Grading, effective Fit and grade, rarity (N2b; docs/specs/v1/pipeline.md, sections 4, 5 and 9;
-- data.md). No deployed database runs the branch (build plan, D1), so the first v1's verdicts and
-- effective grades are dropped rather than carried over: the restart grades again.

-- One row per grading, as before. The grade is derived from `fit` and the current Weight, never
-- stored here. `opinions` holds each grader's answer by role: {fit, reason, model} from a chat
-- grader, {holds, probability, model} from `area_check`. `topic_version` is the topic's version
-- hash it was graded under (topics.version); a verdict stays valid on its topic whatever version.
DROP TABLE verdicts CASCADE;
CREATE TABLE verdicts (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic           text NOT NULL,
    topic_version   text NOT NULL,
    paper_revision  integer NOT NULL,
    fit             text NOT NULL CHECK (fit IN ('central', 'related', 'adjacent', 'none')),
    reason          text NOT NULL,
    opinions        jsonb NOT NULL,
    split           boolean NOT NULL DEFAULT false,  -- two graders gave different Fits
    escalated       boolean NOT NULL DEFAULT false,  -- an Area grading asked `check` too
    recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX verdicts_paper_topic ON verdicts (paper_id, topic, paper_revision);
CREATE INDEX verdicts_topic_recorded ON verdicts (topic, recorded_at DESC);

-- The cascade took the calibration items' reference with it; the draws now point at nothing.
UPDATE calibration_items SET verdict_id = NULL;
ALTER TABLE calibration_items ADD FOREIGN KEY (verdict_id) REFERENCES verdicts (id)
    ON DELETE SET NULL;

-- The owner's word is a Fit (feedback.md). A first-v1 label of relevant, with or without a
-- grade, becomes central, as the evaluation set maps it (data.md); not relevant becomes none.
ALTER TABLE labels DROP CONSTRAINT labels_value_check;
UPDATE labels SET value = CASE value WHEN 'relevant' THEN 'central'
                                     WHEN 'not_relevant' THEN 'none' ELSE value END;
ALTER TABLE labels ADD CONSTRAINT labels_value_check
    CHECK (value IN ('central', 'related', 'adjacent', 'none', 'unsure'));
ALTER TABLE labels DROP COLUMN grade;

ALTER TABLE calibration_items DROP CONSTRAINT calibration_items_answer_check;
UPDATE calibration_items SET answer = CASE answer WHEN 'relevant' THEN 'central'
                                                  WHEN 'not_relevant' THEN 'none' ELSE answer END;
ALTER TABLE calibration_items ADD CONSTRAINT calibration_items_answer_check
    CHECK (answer IN ('central', 'related', 'adjacent', 'none', 'unsure'));

-- Each paper's effective Fit and grade per followed topic, of any depth, with the topic page's
-- rarity: kept current by `feedback.word.settle` so every surface reads one answer and lists
-- sort by it. A row exists while there is a verdict or a word.
DROP TABLE effective_grades;
CREATE TABLE effective_grades (
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic       text NOT NULL,
    fit         text NOT NULL CHECK (fit IN ('central', 'related', 'adjacent', 'none')),
    grade       text NOT NULL CHECK (grade IN ('must_read', 'relevant', 'adjacent', 'none')),
    rarity      text NOT NULL
                CHECK (rarity IN ('common', 'uncommon', 'rare', 'epic', 'legendary')),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic)
);
CREATE INDEX effective_grades_topic ON effective_grades (topic, grade);

-- A paper's level in a mixed list and the followed topic giving the highest level (none for a
-- paper no topic graded: its Weight alone gives the level).
CREATE TABLE rarities (
    paper_id    text PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
    level       text NOT NULL
                CHECK (level IN ('common', 'uncommon', 'rare', 'epic', 'legendary')),
    best_topic  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rarities_level ON rarities (level);

-- Grading fails per paper and topic; the other steps per paper, with topic ''.
ALTER TABLE step_failures DROP CONSTRAINT step_failures_pkey;
ALTER TABLE step_failures DROP CONSTRAINT step_failures_step_check;
ALTER TABLE step_failures ADD COLUMN topic text NOT NULL DEFAULT '';
ALTER TABLE step_failures ADD PRIMARY KEY (paper_id, step, topic);
ALTER TABLE step_failures ADD CONSTRAINT step_failures_step_check
    CHECK (step IN ('classification', 'analysis', 'grading') AND (step = 'grading') = (topic <> ''));
