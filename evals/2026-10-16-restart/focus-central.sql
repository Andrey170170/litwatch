-- Every Focus paper at central in the restart (N6's check), with its Brief or what stands in for one:
-- the pass's state and error, or that no pass was queued. One row per paper and Focus topic.
--   docker exec -i litwatch-v1-dev psql -U postgres -d n6_restart --csv < evals/2026-10-16-restart/focus-central.sql
SELECT g.topic, g.paper_id, g.grade, p.title,
       b.id IS NOT NULL AS brief,
       b.text_source,
       f.state AS pass_state,
       f.failed_runs,
       f.last_error
FROM effective_grades g
JOIN topics t ON t.class = g.topic AND t.depth = 'focus'
JOIN papers p ON p.id = g.paper_id
LEFT JOIN LATERAL (
    SELECT id, text_source FROM briefs WHERE paper_id = g.paper_id AND paper_revision = p.revision
    ORDER BY id DESC LIMIT 1
) b ON true
LEFT JOIN LATERAL (
    SELECT state, failed_runs, last_error FROM fulltext_passes
    WHERE paper_id = g.paper_id AND paper_revision = p.revision AND kind = 'brief'
    ORDER BY id DESC LIMIT 1
) f ON true
WHERE g.fit = 'central'
ORDER BY g.topic, g.paper_id;
