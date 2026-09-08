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
