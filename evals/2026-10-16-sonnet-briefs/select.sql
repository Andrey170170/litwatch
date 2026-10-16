-- P1's papers from n6_restart: N6's three refusals, then Focus Briefs and Weight re-checks
-- stratified by Opus's full-text Weight and overstatement line, picked in md5(paper_id) order.
-- One JSON row a paper: the area its pass judged Weight in and Opus's answer, where there is one.
WITH done AS (
    SELECT f.id, f.paper_id, f.kind, w.weight, w.reason, b.text AS brief, b.overstatement,
           g.text AS gist, a.primary_class AS area,
           row_number() OVER (PARTITION BY f.kind, w.weight, b.overstatement IS NOT NULL
                              ORDER BY md5(f.paper_id)) AS n
    FROM fulltext_passes f
    JOIN weights w ON w.pass_id = f.id
    JOIN briefs b ON b.pass_id = f.id
    LEFT JOIN gists g ON g.pass_id = f.id
    JOIN analyses a ON a.paper_id = f.paper_id AND a.paper_revision = f.paper_revision
    WHERE f.state = 'done'
), quota(kind, weight, over, take) AS (VALUES
    ('brief', 'incremental', false, 2), ('brief', 'incremental', true, 3),
    ('brief', 'solid', false, 4), ('brief', 'solid', true, 3),
    ('weight', 'landmark', false, 1), ('weight', 'incremental', false, 3),
    ('weight', 'incremental', true, 6), ('weight', 'solid', false, 9),
    ('weight', 'solid', true, 6)
), picked AS (
    SELECT d.* FROM done d JOIN quota q
      ON q.kind = d.kind AND q.weight = d.weight AND q.over = (d.overstatement IS NOT NULL)
    WHERE d.n <= q.take
), refused AS (
    SELECT DISTINCT ON (a.paper_id) a.paper_id, a.primary_class AS area
    FROM analyses a
    WHERE a.paper_id IN ('2610.17907', '2610.13116', '2610.21200')
    ORDER BY a.paper_id, a.paper_revision DESC
)
SELECT json_build_object('id', paper_id, 'kind', kind, 'area', area, 'weight', weight,
                         'weight_reason', reason, 'brief', brief, 'overstatement', overstatement,
                         'gist', gist)
FROM picked
UNION ALL
SELECT json_build_object('id', paper_id, 'kind', 'refused', 'area', area) FROM refused;
