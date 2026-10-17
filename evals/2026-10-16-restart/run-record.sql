-- The restart's counts behind evals/2026-10-16-restart.md, read from n6_restart after the run; the output is
-- run-record.txt beside it, and every call is in run-calls.csv.gz.
--   docker exec -i litwatch-v1-dev psql -U postgres -d n6_restart < evals/2026-10-16-restart/run-record.sql

\echo 'Runs'
SELECT kind, count(*) AS runs, min(started_at)::timestamp(0) AS first_start, max(finished_at)::timestamp(0) AS last_end
FROM runs GROUP BY kind ORDER BY min(id);

\echo 'Calls by role (the journal)'
SELECT operation AS role, usage->>'via' AS via, count(*) AS calls, count(*) FILTER (WHERE error IS NOT NULL) AS failed,
       count(*) FILTER (WHERE usage->>'cost' IS NULL) AS no_cost, round(sum((usage->>'cost')::numeric), 4) AS cost,
       min(started_at)::timestamp(0) AS first, max(started_at)::timestamp(0) AS last
FROM inference_calls GROUP BY 1, 2 ORDER BY min(id);
SELECT usage->>'via' AS via, round(sum((usage->>'cost')::numeric), 4) AS cost FROM inference_calls GROUP BY 1 ORDER BY 1;

\echo 'Papers by listing day'
SELECT announced, count(*) AS papers FROM papers GROUP BY 1 ORDER BY 1;
SELECT (SELECT count(*) FROM classifications) AS classifications, (SELECT count(*) FROM gists) AS gists,
       (SELECT count(*) FROM verdicts) AS verdicts;

\echo 'Effective grades by depth'
SELECT t.depth, count(*) AS pairs, count(DISTINCT g.paper_id) AS papers,
       count(*) FILTER (WHERE g.grade = 'must_read') AS must_read
FROM effective_grades g JOIN topics t ON t.class = g.topic GROUP BY 1 ORDER BY 1;

\echo 'Focus fits'
SELECT g.topic, g.fit, count(*) FROM effective_grades g JOIN topics t ON t.class = g.topic AND t.depth = 'focus'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo 'Failed steps'
SELECT f.paper_id, f.step, f.topic, f.failed_runs, f.last_error, p.title
FROM step_failures f JOIN papers p ON p.id = f.paper_id ORDER BY f.step, f.paper_id;

\echo 'Full-text passes'
SELECT kind, reason, state, count(*) FROM fulltext_passes GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
SELECT f.paper_id, f.kind, f.state, f.failed_runs, f.last_error, p.title
FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id WHERE f.state <> 'done' ORDER BY f.kind, f.paper_id;

\echo 'Brief-role calls by the pass kind of their paper'
WITH k AS (SELECT paper_id, string_agg(DISTINCT kind, '+' ORDER BY kind) AS kinds
           FROM fulltext_passes WHERE kind <> 'deep' GROUP BY 1)
SELECT k.kinds, count(*) AS calls, round(sum((c.usage->>'cost')::numeric), 3) AS cost,
       round(avg((c.usage->>'cost')::numeric), 4) AS mean, round(max((c.usage->>'cost')::numeric), 3) AS max,
       round(avg((c.usage->>'prompt_tokens')::numeric)) AS input_tokens
FROM inference_calls c JOIN k USING (paper_id) WHERE c.operation = 'brief' GROUP BY 1;

\echo 'Briefs and Weights'
SELECT text_source, count(*) AS briefs, count(*) FILTER (WHERE overstatement IS NOT NULL) AS with_overstatement
FROM briefs GROUP BY 1;
SELECT count(*) FILTER (WHERE overstatement IS NOT NULL) AS focus_briefs_with_overstatement, count(*) AS focus_briefs
FROM briefs b WHERE EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.id = b.pass_id AND f.kind = 'brief');
SELECT source, weight, count(*) FROM weights GROUP BY 1, 2 ORDER BY 1, 2;
SELECT f.kind AS pass, w.weight, count(*) FROM weights w JOIN fulltext_passes f ON f.id = w.pass_id
GROUP BY 1, 2 ORDER BY 1, 2;

\echo 'Digests'
SELECT period, day, topic, state, counts FROM digests ORDER BY period, day, topic;

\echo 'Writer calls matched to their digests by time (a writer call names no paper)'
SELECT d.period, d.day, d.topic, (d.counts->>'papers')::int AS papers, (c.usage->>'prompt_tokens')::int AS input_tokens,
       round((c.usage->>'cost')::numeric, 3) AS cost
FROM digests d JOIN LATERAL (
    SELECT usage FROM inference_calls c WHERE c.operation = 'writer' AND c.responded_at <= d.written_at
    ORDER BY c.responded_at DESC LIMIT 1
) c ON true
WHERE d.state = 'written' ORDER BY d.period, d.day, d.topic;

\echo 'Deep cards'
SELECT count(*) AS attempts, count(*) FILTER (WHERE error IS NOT NULL) AS errors,
       round(avg((usage->>'seconds')::numeric)) AS mean_seconds, max((usage->>'seconds')::int) AS max_seconds,
       round(sum((usage->>'list_cost')::numeric), 2) AS list_cost
FROM deep_attempts;
SELECT count(*) AS cards FROM deep_cards WHERE card IS NOT NULL;
