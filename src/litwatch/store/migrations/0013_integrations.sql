-- Integrations: where each has read the outbox to, and the state each keeps of its own.

-- One row per integration, made the first time it is loaded at the outbox's end then, so an
-- integration enabled late is not woken by everything that happened before it.
CREATE TABLE integration_cursors (
    name        text PRIMARY KEY,
    position    bigint NOT NULL,             -- the last event handled, passed over or skipped
    failing     bigint,                      -- the event after it, while that one fails
    failures    integer NOT NULL DEFAULT 0,  -- how many times in a row it failed
    retry_at    timestamptz,                 -- the failing event is not tried before this
    last_error  text,                        -- kept after a recovery, for the record
    failed_at   timestamptz,
    skipped     integer NOT NULL DEFAULT 0,  -- events the owner skipped
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- zotero: the item that carries each paper. Seeded from 0.x's `papers.zotero_key`.
CREATE TABLE zotero_items (
    paper_id   text PRIMARY KEY REFERENCES papers(id),
    item_key   text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- webdav: the PDF attachment stored for a paper's Zotero item. Seeded from 0.x's
-- `papers.zotero_attachment_key`.
CREATE TABLE webdav_files (
    paper_id       text PRIMARY KEY REFERENCES papers(id),
    attachment_key text NOT NULL,
    stored_at      timestamptz NOT NULL DEFAULT now()
);

-- deep: one deep card per paper, or the last failure to get one. The card is shown on the
-- paper page whether or not the integration is still loaded.
CREATE TABLE deep_cards (
    paper_id     text PRIMARY KEY REFERENCES papers(id),
    card         jsonb,                     -- the validated result; null until one is written
    model        text,
    stats        jsonb NOT NULL DEFAULT '{}',
    trace        jsonb NOT NULL DEFAULT '[]',
    error        text,                      -- the last attempt's failure
    attempts     integer NOT NULL DEFAULT 0,
    attempted_at timestamptz,
    written_at   timestamptz
);

-- An adopted 0.x database brings its cards along on the paper rows.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'papers' AND column_name = 'enrichment') THEN
        INSERT INTO deep_cards (paper_id, card, model, stats, trace, error, attempts,
                                attempted_at, written_at)
        SELECT id, enrichment, enrichment_model, coalesce(deep_stats, '{}'),
               coalesce(deep_trace, '[]'), enrichment_error, deep_attempts, enriched_at,
               enriched_at
        FROM papers WHERE enrichment IS NOT NULL;
    END IF;
END $$;
