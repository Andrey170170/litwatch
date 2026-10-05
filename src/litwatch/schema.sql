-- Applied idempotently at startup. Additive changes only; keep it re-runnable.

CREATE TABLE IF NOT EXISTS papers (
    id               text PRIMARY KEY,           -- version-stripped arXiv id
    title            text NOT NULL,
    abstract         text NOT NULL,
    authors          jsonb NOT NULL DEFAULT '[]',
    categories       text[] NOT NULL DEFAULT '{}',
    primary_category text,
    published        timestamptz,
    updated          timestamptz,
    url              text,
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    keyword_hit      boolean NOT NULL DEFAULT false,
    screened_at      timestamptz,
    screen           jsonb,                      -- ScreenResult as JSON
    tier             text,
    prompt_version   text,
    screen_error     text,
    search           tsvector GENERATED ALWAYS AS
                         (to_tsvector('english', title || ' ' || abstract)) STORED
);

CREATE INDEX IF NOT EXISTS papers_tier_idx ON papers (tier);
CREATE INDEX IF NOT EXISTS papers_published_idx ON papers (published DESC);
CREATE INDEX IF NOT EXISTS papers_pending_idx ON papers (fetched_at) WHERE screened_at IS NULL;
CREATE INDEX IF NOT EXISTS papers_search_idx ON papers USING gin (search);

CREATE TABLE IF NOT EXISTS runs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,                  -- daily | backfill | manual
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    window_from  timestamptz,
    window_to    timestamptz,
    fetched      integer NOT NULL DEFAULT 0,
    inserted     integer NOT NULL DEFAULT 0,
    updated      integer NOT NULL DEFAULT 0,
    screened     integer NOT NULL DEFAULT 0,
    rescreened   integer NOT NULL DEFAULT 0,
    failed       integer NOT NULL DEFAULT 0,
    cost         numeric(10, 4) NOT NULL DEFAULT 0,
    tiers        jsonb,
    error        text
);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS confirmed integer NOT NULL DEFAULT 0;
-- Routing/deep failures that did not stop the run; `error` stays reserved for a failed run.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stage_errors text;

-- Routing state (phase 3): which Zotero item carries the paper, and at which tier it was routed.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_key text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_attachment_key text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS routed_at timestamptz;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS routed_tier text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS route_error text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS routed integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS route_failed integer NOT NULL DEFAULT 0;

-- Digest prose for the run (phase 3), written after routing.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS digest_model text;

-- Paper text and deep processing (phase 4). The trace is the material for evolving the profile.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS body text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS body_source text;          -- html | pdf
ALTER TABLE papers ADD COLUMN IF NOT EXISTS body_fetched_at timestamptz;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS enrichment jsonb;          -- validated deep result
ALTER TABLE papers ADD COLUMN IF NOT EXISTS enriched_at timestamptz;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS enrichment_model text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS enrichment_error text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS deep_trace jsonb;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS deep_stats jsonb;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS deep_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS zotero_enriched_at timestamptz;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS processed integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS process_failed integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS deep_list_cost numeric(10, 4) NOT NULL DEFAULT 0;

-- Selections (phase 5): human-written topic specs synced from Notion, versioned by content hash,
-- and one verdict row per (paper, selection). `best_tier` is the paper's best verdict across
-- active selections; NULL means the paper has not been judged under selections yet. The legacy
-- `tier` column stops being written and can be dropped once every row in use has a best_tier.
CREATE TABLE IF NOT EXISTS selections (
    slug        text PRIMARY KEY,
    name        text NOT NULL,
    owner       text,
    status      text NOT NULL,                  -- draft | active | paused
    subject     text NOT NULL,
    rubric      text,
    keywords    text[] NOT NULL DEFAULT '{}',
    version     text NOT NULL,                  -- content hash of name, subject, rubric, keywords
    notion_id   text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    synced_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verdicts (
    paper_id    text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    selection   text NOT NULL,
    tier        smallint NOT NULL,
    confidence  text NOT NULL,
    reason      text NOT NULL,
    version     text NOT NULL,                  -- selection version the verdict was judged under
    model       text,
    screened_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, selection)
);

CREATE INDEX IF NOT EXISTS verdicts_selection_tier_idx ON verdicts (selection, tier, screened_at DESC);
ALTER TABLE papers ADD COLUMN IF NOT EXISTS best_tier smallint;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS selections_version text;
CREATE INDEX IF NOT EXISTS papers_best_tier_idx ON papers (best_tier, screened_at DESC)
    WHERE best_tier IS NOT NULL;

ALTER TABLE selections ADD COLUMN IF NOT EXISTS digest boolean NOT NULL DEFAULT false;

-- One digest per run and source: 'general' across the feed, or a selection slug.
CREATE TABLE IF NOT EXISTS digests (
    run_id      bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    source      text NOT NULL,
    title       text NOT NULL,
    body        text NOT NULL,                  -- markdown: ##, ###, - bullets, [title](url), **bold**
    model       text,
    cost        numeric(10,4),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source)
);

-- Inputs are frozen before generation; failed batches can be retried without re-screening.
CREATE TABLE IF NOT EXISTS digest_batches (
    run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    source text NOT NULL,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source)
);

-- A paper can be covered once in each digest stream, including count-only members.
CREATE TABLE IF NOT EXISTS digest_coverage (
    source text NOT NULL,
    paper_id text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    PRIMARY KEY (source, paper_id)
);
CREATE INDEX IF NOT EXISTS digest_coverage_run_idx ON digest_coverage (run_id);
CREATE INDEX IF NOT EXISTS digest_coverage_paper_idx ON digest_coverage (paper_id);
CREATE TABLE IF NOT EXISTS digest_history_migrations (
    name text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Digest counts describe its frozen membership, not every paper screened in the run.
ALTER TABLE digests ADD COLUMN IF NOT EXISTS tiers jsonb;

-- Intake completeness is independent of successful screening/digest production.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS intake jsonb;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS metadata_source text NOT NULL DEFAULT 'arxiv';
CREATE TABLE IF NOT EXISTS intake_checkpoints (
    category text PRIMARY KEY,
    discovery_from date NOT NULL,
    checkpoint date,
    checked_at timestamptz,
    run_id bigint REFERENCES runs(id)
);
-- Raw daily feeds survive processing failures and are included in the normal DB backup.
CREATE TABLE IF NOT EXISTS intake_snapshots (
    category text NOT NULL,
    sha256 text NOT NULL,
    body bytea NOT NULL,
    generated_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (category, sha256)
);

-- Each received response is charged to its originating run, even if parsing or a later
-- stage fails. Only validated screening outputs are reusable. NULL cost means unknown.
CREATE TABLE IF NOT EXISTS inference_calls (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES runs(id),
    operation text NOT NULL,
    paper_id text,
    request_key text,
    usage jsonb NOT NULL,
    output jsonb,
    error text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    responded_at timestamptz,
    invalidated_at timestamptz
);
CREATE INDEX IF NOT EXISTS inference_calls_run_idx ON inference_calls(run_id);
CREATE INDEX IF NOT EXISTS inference_calls_cache_idx ON inference_calls(request_key, id)
    WHERE output IS NOT NULL AND invalidated_at IS NULL;
CREATE INDEX IF NOT EXISTS inference_calls_paper_idx ON inference_calls(paper_id);

-- Explicit arXiv revisions are independent of selection/prompt versions.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS version integer CHECK (version>0);
ALTER TABLE papers ADD COLUMN IF NOT EXISTS version_history jsonb NOT NULL DEFAULT '[]';
ALTER TABLE papers ADD COLUMN IF NOT EXISTS doi text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS journal_ref text;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS license text;

-- No paper FK: replacement-only feed observations must not create curated candidates.
CREATE TABLE IF NOT EXISTS archive_outbox (
    id bigserial PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    paper_id text NOT NULL,
    version integer CHECK (version>0),
    kind text NOT NULL CHECK(kind IN ('metadata','pdf','source')),
    payload jsonb NOT NULL,
    priority integer NOT NULL DEFAULT 100,
    status text NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','delivered','available','unavailable','error')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_token text,
    lease_until timestamptz,
    remote_job_id text,
    error text,
    run_id bigint REFERENCES runs(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
CREATE INDEX IF NOT EXISTS archive_outbox_due_idx ON archive_outbox(priority,next_attempt_at,id)
    WHERE status IN ('pending','error','delivered');
CREATE INDEX IF NOT EXISTS archive_outbox_paper_idx ON archive_outbox(paper_id);

-- Coverage means observations + intents are durable, not that all files are downloaded.
CREATE TABLE IF NOT EXISTS archive_intake_checkpoints (
    category text PRIMARY KEY,
    policy text NOT NULL,
    activated_at timestamptz NOT NULL DEFAULT now(),
    discovery_from date NOT NULL,
    checkpoint date,
    checked_at timestamptz,
    run_id bigint REFERENCES runs(id)
);

-- Set when title, abstract, authors or published change, or a selection's status moves the
-- best tier without a screening, so the projection re-syncs the row.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS metadata_changed_at timestamptz;

-- Set when a member paper drops to tier 4, cleared when it is a member again, so the
-- projection can archive the page it made.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS demoted_at timestamptz;
CREATE INDEX IF NOT EXISTS papers_demoted_idx ON papers (demoted_at) WHERE demoted_at IS NOT NULL;

-- arXiv's canonical <created> can be a later revision's date; v1 is the first posting.
UPDATE papers SET published = (version_history->0->>'date')::timestamptz,
                  metadata_changed_at = now()
WHERE version_history->0->>'version' = '1'
  AND (published IS NULL OR published > (version_history->0->>'date')::timestamptz);

-- The version a stream covered is the baseline for revision notes. Rows that predate the
-- column take the current version once, so past revisions are not announced retroactively.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'digest_coverage'
                     AND column_name = 'version') THEN
        ALTER TABLE digest_coverage ADD COLUMN version integer;
        UPDATE digest_coverage c SET version = p.version FROM papers p WHERE p.id = c.paper_id;
    END IF;
END $$;

-- A newer version of a covered tier 1/2 paper is noted once per stream and version.
CREATE TABLE IF NOT EXISTS digest_revisions (
    source text NOT NULL,
    paper_id text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    version integer NOT NULL,
    run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    PRIMARY KEY (source, paper_id, version)
);
CREATE INDEX IF NOT EXISTS digest_revisions_run_idx ON digest_revisions (run_id);

-- How far the Notion projection has got: the (change time, paper id) of the last member row
-- it wrote. One row; absent until the first acknowledgement.
CREATE TABLE IF NOT EXISTS projection_cursor (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    changed_at timestamptz NOT NULL,
    paper_id text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- True when the latest screening was a backlog re-screen under an edited selection: the paper
-- was judged again, not newly analyzed, so the digest does not announce it.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS screened_quietly boolean NOT NULL DEFAULT false;

-- False until the backlog drain has taken a new selection through its whole window. Until
-- then the selection is still new, however many of its papers already have a verdict.
ALTER TABLE selections ADD COLUMN IF NOT EXISTS window_screened boolean NOT NULL DEFAULT true;
