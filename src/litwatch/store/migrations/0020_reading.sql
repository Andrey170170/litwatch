-- The reading API (N2b; docs/specs/v1/DESIGN.md, Pages): reaction reasons as feedback.md names
-- them, the current Weight beside the rarity it sets, and search over the authors too.

-- A reason points at the stage that would have made the mistake (feedback.md, reactions).
ALTER TABLE reactions DROP CONSTRAINT reactions_reason_check;
UPDATE reactions SET reason = CASE reason WHEN 'not_for_me' THEN 'not_interesting'
                                          WHEN 'unrelated' THEN 'wrong_topic' ELSE reason END;
ALTER TABLE reactions ADD CONSTRAINT reactions_reason_check
    CHECK (reason IN ('wrong_topic', 'overrated', 'not_interesting', 'already_knew',
                      'underrated', 'great_find'));

-- The paper's current Weight, which sets its floor (pipeline.md, section 5), kept with the level
-- by `feedback.word.settle`: lists show and filter by it, and a topic page colours an ungraded
-- paper by it, without looking up every paper's newest Weight. Null until analysed.
ALTER TABLE rarities ADD COLUMN weight text CHECK (weight IN ('landmark', 'solid', 'incremental'));
UPDATE rarities r SET weight = (
    SELECT w.weight FROM weights w JOIN papers p ON p.id = w.paper_id
    AND p.revision = w.paper_revision WHERE w.paper_id = r.paper_id ORDER BY w.id DESC LIMIT 1);

-- Title over authors over abstract, for "Best match".
ALTER TABLE papers DROP COLUMN search;
ALTER TABLE papers ADD COLUMN search tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', title), 'A')
    || setweight(to_tsvector('english', authors::text), 'B')
    || setweight(to_tsvector('english', abstract), 'C')) STORED;
CREATE INDEX papers_search_idx ON papers USING gin (search);

-- The status line counts what the latest run classified.
CREATE INDEX classifications_at ON classifications (classified_at);
