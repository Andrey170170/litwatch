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
