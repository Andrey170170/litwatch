-- Every Deep attempt keeps its trace (docs/plans/deep-open-web.md, P2; data.md; decision 81).
-- Until now the card kept one, the last job's: `put_card`, or `card_failed` with a trace, wrote
-- it, and only an attempt whose job ran gave one. So a card's trace is its paper's latest ended
-- attempt with a job, and that attempt's `usage.turns` must count the trace's assistant turns.
-- A trace that has no such attempt, or fails the count, stops the migration, which rolls back
-- whole: it is settled by hand before the next try. NULL on an attempt: no trace was kept (its
-- job never ran, was lost or never ended, or ran before this).
ALTER TABLE deep_attempts ADD COLUMN trace jsonb;

DO $$
DECLARE
    c record;
    a record;
    turns integer;
BEGIN
    FOR c IN SELECT paper_id, trace FROM deep_cards
             WHERE jsonb_typeof(trace) = 'array' AND jsonb_array_length(trace) > 0
             ORDER BY paper_id LOOP
        SELECT id, usage INTO a FROM deep_attempts
        WHERE paper_id = c.paper_id AND ended_at IS NOT NULL AND job_id IS NOT NULL
        ORDER BY id DESC LIMIT 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'deep card % has a trace but no ended attempt with a job', c.paper_id;
        END IF;
        SELECT count(*) INTO turns FROM jsonb_array_elements(c.trace) e
        WHERE e->>'type' = 'assistant';
        IF (a.usage->>'turns')::integer IS DISTINCT FROM turns THEN
            RAISE EXCEPTION 'deep card %: its trace has % turns, its attempt % has %',
                c.paper_id, turns, a.id, coalesce(a.usage->>'turns', 'none');
        END IF;
        UPDATE deep_attempts SET trace = c.trace WHERE id = a.id;
    END LOOP;
END $$;

ALTER TABLE deep_cards DROP COLUMN trace;
