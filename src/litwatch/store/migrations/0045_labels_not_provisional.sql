-- A label accepted in bulk, unread (A1, docs/notes/still-used.md D3): nothing has written one
-- since the first v1, and production holds none. A label left would turn firm, so the
-- migration stops instead; the provisional replay cohort (`evals/labels/`) is another thing.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM labels WHERE provisional) THEN
        RAISE EXCEPTION 'labels holds provisional labels; delete or confirm them first';
    END IF;
END $$;
ALTER TABLE labels DROP COLUMN provisional;
