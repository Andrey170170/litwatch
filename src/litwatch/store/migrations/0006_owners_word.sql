-- The owner's word beside the labels: thumbs, the Save toggle, and what follows from them,
-- each paper's effective grade per Watch topic and whether it is in the library.

-- The labelling page's labels are the owner's own choice of paper.
UPDATE labels SET origin = 'reader' WHERE origin = 'inbox';

-- A thumb on a paper for the topic it was shown under, or for none (topic NULL). One per
-- paper and topic, replaced on change. Never a label; see core.owners_word.
CREATE TABLE reactions (
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic       text,
    value       text NOT NULL CHECK (value IN ('up', 'down')),
    reason      text CHECK (reason IN ('not_for_me', 'unrelated', 'already_knew', 'overrated',
                                       'great_find')),
    reacted_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX reactions_paper_topic_idx ON reactions (paper_id, coalesce(topic, ''));

-- Each paper's effective grade per Watch topic, kept so that a change can be noticed and
-- every surface reads the same answer. A row exists while there is a verdict or a word.
CREATE TABLE effective_grades (
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic       text NOT NULL,
    grade       text NOT NULL CHECK (grade IN ('must_read', 'relevant', 'adjacent', 'none')),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic)
);
CREATE INDEX effective_grades_topic_idx ON effective_grades (topic, grade);

-- The owner's newest use of the Save toggle, and whether the paper is in the library now.
CREATE TABLE library (
    paper_id  text PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
    hand      text CHECK (hand IN ('save', 'unsave')),
    hand_at   timestamptz,
    saved     boolean NOT NULL DEFAULT false,
    saved_at  timestamptz
);
CREATE INDEX library_saved_idx ON library (saved_at DESC) WHERE saved;
