-- Updated papers in digests (docs/plans/updated-papers.md; digests.md, updated papers; data.md).

-- Whether `announced` was read from the first version's submission time in the version history:
-- a known day, which a day guessed from other metadata (a `<created>` date, when the version
-- list failed to load) never replaces. The history itself is cleared when such a record comes,
-- so it cannot tell afterwards. The papers stored before now know their day while their history
-- still has the first version's time.
ALTER TABLE papers ADD COLUMN announced_known boolean NOT NULL DEFAULT false;
UPDATE papers p SET announced_known = true
WHERE EXISTS (
    SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.version_history) = 'array'
                                            THEN p.version_history ELSE '[]' END) e
    WHERE e->>'version' = '1' AND e->>'date' IS NOT NULL);

