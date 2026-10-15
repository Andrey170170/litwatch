-- Digests (N4; docs/specs/v1/digests.md, data.md). What a digest still has to cover is not
-- stored: a paper is due for a topic while it is eligible there and in no published digest of
-- the topic that clears it, found from the database when a digest is written. The papers a
-- digest covered are its record.

-- The date of the arXiv listing that first showed the paper (`core.digests.announced`): the day
-- a daily digest takes it on, and the one it is shown with when it arrives late.
ALTER TABLE papers ADD COLUMN announced date;
CREATE INDEX papers_announced ON papers (announced);

-- The papers stored before now, by the same rule: the first version's submission time in US
-- Eastern (else its date, as before that day's deadline), the deadline it made (14:00 on a
-- weekday), and the listing on the weekday after.
WITH submitted AS (
    SELECT p.id, v.at, p.published
    FROM papers p LEFT JOIN LATERAL (
        SELECT (e->>'date')::timestamptz AS at
        FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.version_history) = 'array'
                                       THEN p.version_history ELSE '[]' END) e
        WHERE e->>'version' = '1' AND e->>'date' IS NOT NULL LIMIT 1) v ON true
), local AS (
    SELECT id,
           coalesce((at AT TIME ZONE 'America/New_York')::date,
                    (published AT TIME ZONE 'UTC')::date) AS day,
           coalesce((at AT TIME ZONE 'America/New_York')::time >= '14:00', false) AS late
    FROM submitted
), deadline AS (
    SELECT id, CASE WHEN extract(isodow FROM day) >= 6 OR late
                    THEN day + CASE extract(isodow FROM day) WHEN 5 THEN 3 WHEN 6 THEN 2 ELSE 1 END
                    ELSE day END AS day
    FROM local WHERE day IS NOT NULL
)
UPDATE papers p
SET announced = d.day + CASE extract(isodow FROM d.day) WHEN 5 THEN 3 WHEN 6 THEN 2 ELSE 1 END
FROM deadline d WHERE d.id = p.id;

-- One topic's digest of one day, or of one week keyed by its Monday. Published means written
-- or nothing_new, and is final; a failed one is written again, over the papers due then.
CREATE TABLE digests (
    id           bigserial PRIMARY KEY,
    topic        text NOT NULL,
    period       text NOT NULL CHECK (period IN ('day', 'week')),
    day          date NOT NULL CHECK (period = 'day' OR extract(isodow FROM day) = 1),
    state        text NOT NULL CHECK (state IN ('written', 'nothing_new', 'failed')),
    gist         text,
    text         text,
    -- papers, picks, central, related, must_read: what its Mark counts
    counts       jsonb NOT NULL DEFAULT '{}',
    best_rarity  text CHECK (best_rarity IN ('common', 'uncommon', 'rare', 'epic', 'legendary')),
    error        text,                          -- why a failed one could not be written
    run_id       bigint REFERENCES runs (id) ON DELETE SET NULL,
    written_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (topic, period, day),
    CHECK ((state = 'written') = (text IS NOT NULL AND gist IS NOT NULL))
);
CREATE INDEX digests_day ON digests (day, period);

-- A published digest's papers, each frozen as it was shown. `graded_on` is the topic whose grade
-- it showed (the digest's own, or a followed topic under it; NULL for an ungraded paper), which
-- is where a later set-aside is read.
CREATE TABLE digest_papers (
    digest_id       bigint NOT NULL REFERENCES digests (id) ON DELETE CASCADE,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('pick', 'listed', 'mentioned')),
    position        integer NOT NULL,
    why             text,                       -- a pick's reason
    paper_revision  integer NOT NULL,
    gist            text,
    fit             text CHECK (fit IN ('central', 'related', 'adjacent', 'none')),
    grade           text CHECK (grade IN ('must_read', 'relevant', 'adjacent', 'none')),
    rarity          text NOT NULL
                    CHECK (rarity IN ('common', 'uncommon', 'rare', 'epic', 'legendary')),
    graded_on       text,
    PRIMARY KEY (digest_id, paper_id)
);
CREATE INDEX digest_papers_paper ON digest_papers (paper_id);

-- A set-aside is read live from the paper's effective grade (digests.md, a digest is a record).
DROP TABLE set_asides;
