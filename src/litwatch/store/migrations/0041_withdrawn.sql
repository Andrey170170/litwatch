-- Withdrawn papers (pipeline.md, section 8): arXiv's comments field, which gives a withdrawal's
-- reason; when arXiv withdrew the paper (`core.withdrawn_at`: its newest version is an empty
-- one), set by intake; and `dropped`, the one rule for a paper litwatch no longer processes,
-- dismissed by the owner or withdrawn.
ALTER TABLE papers ADD COLUMN comments text;
ALTER TABLE papers ADD COLUMN withdrawn_at timestamptz;
ALTER TABLE papers ADD COLUMN dropped boolean
    GENERATED ALWAYS AS (dismissed_at IS NOT NULL OR withdrawn_at IS NOT NULL) STORED;

-- The papers already withdrawn, from the version histories stored.
UPDATE papers p SET withdrawn_at = w.at
FROM (
    SELECT q.id, (newest.e ->> 'date')::timestamptz AS at
    FROM papers q, LATERAL (
        SELECT e FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(q.version_history) = 'array' THEN q.version_history
                 ELSE '[]' END
        ) e
        ORDER BY (e ->> 'version')::int DESC NULLS LAST LIMIT 1
    ) newest
    WHERE newest.e ->> 'size' = '0kb' AND pg_input_is_valid(newest.e ->> 'date', 'timestamptz')
) w
WHERE w.id = p.id;

ANALYZE papers;
