-- Integrations: where each has read the outbox to, and the state each keeps of its own.

-- An event's place is (xid, id): the id of the transaction that wrote it, then its own id.
-- Ids are handed out in insert order, not commit order, so a reader going by id alone would
-- pass over an event whose transaction commits after a later one's. A reader takes only events
-- whose transaction is older than every transaction still running; any event yet to commit
-- then sorts after everything read. On an adopted database the rows already there share this
-- migration's xid.
ALTER TABLE outbox ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id();
CREATE INDEX outbox_order_idx ON outbox (xid, id);

-- One row per integration, made the first time it is loaded at the outbox's end then, so an
-- integration enabled late is not woken by everything that happened before it.
CREATE TABLE integration_cursors (
    name         text PRIMARY KEY,
    position_xid xid8 NOT NULL,               -- with `position`, the place of the last event
    position     bigint NOT NULL,             -- handled, passed over or skipped
    failing      bigint,                      -- the event after it, while that one fails
    failures     integer NOT NULL DEFAULT 0,  -- how many times in a row it failed
    retry_at     timestamptz,                 -- the failing event is not tried before this
    last_error   text,                        -- kept after a recovery, for the record
    failed_at    timestamptz,
    skipped      integer NOT NULL DEFAULT 0,  -- events the owner skipped
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- zotero: the item that carries each paper. Seeded from 0.x's `papers.zotero_key`.
CREATE TABLE zotero_items (
    paper_id   text PRIMARY KEY REFERENCES papers(id),
    item_key   text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- webdav: the PDF attachment of a paper's Zotero item. `pending` while litwatch's upload of
-- its file is not known to be complete; `owner` when the item already had a PDF litwatch did
-- not make, which it leaves alone. Seeded from 0.x's `papers.zotero_attachment_key`.
CREATE TABLE webdav_files (
    paper_id       text PRIMARY KEY REFERENCES papers(id),
    item_key       text NOT NULL,              -- the Zotero item it is attached to
    attachment_key text NOT NULL,
    state          text NOT NULL CHECK (state IN ('pending', 'stored', 'owner')),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- deep: one deep card per paper, or the last failure to get one. The card is shown on the
-- paper page whether or not the integration is still loaded. `job_id` is the deep service's
-- job while one is running, so a retry or a restart follows it instead of paying for another.
CREATE TABLE deep_cards (
    paper_id     text PRIMARY KEY REFERENCES papers(id),
    card         jsonb,                     -- the validated result; null until one is written
    model        text,
    stats        jsonb NOT NULL DEFAULT '{}',
    trace        jsonb NOT NULL DEFAULT '[]',
    error        text,                      -- the last attempt's failure
    job_id       text,
    job_since    timestamptz,               -- when that job was submitted
    job_revision integer,                   -- the paper revision and arXiv version it was
    job_version  integer,                   -- submitted for: a card for older text is not kept
    job_event    bigint,                    -- the event that asked, and whether it was
    job_automatic boolean,                  -- automatic, should the paper have to wait again
    written_at   timestamptz
);

-- deep: every submission that may have reached the service, for the attempt limits and the
-- daily cap on automatic cards. `automatic` is false for the owner's requests.
CREATE TABLE deep_attempts (
    id        bigserial PRIMARY KEY,
    paper_id  text NOT NULL REFERENCES papers(id),
    event_id  bigint NOT NULL,
    automatic boolean NOT NULL,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deep_attempts_paper_idx ON deep_attempts (paper_id);
CREATE INDEX deep_attempts_at_idx ON deep_attempts (at) WHERE automatic;

-- deep: papers whose text could not be had yet (no HTML rendering, the archive still fetching
-- the PDF, arXiv failing). Retried a few at a time while other events are handled, so one
-- paper never holds up the rest; given up after a day.
CREATE TABLE deep_waiting (
    paper_id  text PRIMARY KEY REFERENCES papers(id),
    event_id  bigint NOT NULL,           -- the event that asked, for its attempt count
    automatic boolean NOT NULL,
    since     timestamptz NOT NULL DEFAULT now(),
    tried_at  timestamptz NOT NULL DEFAULT now()
);

-- The paper revision and arXiv version a fetched full text belongs to: a rewrite or a new
-- version makes it stale (a new version can change the body without touching the abstract).
ALTER TABLE papers ADD COLUMN IF NOT EXISTS body_revision integer,
    ADD COLUMN IF NOT EXISTS body_version integer;
-- A text 0.x fetched belongs to the paper as it is when adopted; from here on it is stale
-- like any other once the paper is rewritten or gets a new version.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'papers' AND column_name = 'body') THEN
        UPDATE papers SET body_revision = coalesce(body_revision, revision), body_version = version
        WHERE body IS NOT NULL AND body_revision IS NULL;
    END IF;
END $$;

-- An adopted 0.x database brings its cards along on the paper rows.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'papers' AND column_name = 'enrichment') THEN
        INSERT INTO deep_cards (paper_id, card, model, stats, trace, error, written_at)
        SELECT id, enrichment, enrichment_model, coalesce(deep_stats, '{}'),
               coalesce(deep_trace, '[]'), enrichment_error, enriched_at
        FROM papers WHERE enrichment IS NOT NULL;
    END IF;
END $$;
