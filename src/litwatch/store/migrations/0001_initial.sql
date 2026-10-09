-- The collection, run records, the model call journal, intake state and the event outbox.
-- Written so that it also applies to an adopted 0.x database, whose tables of the same
-- name and shape are kept as they are (see store.adopt_0x).

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
    metadata_source  text NOT NULL DEFAULT 'arxiv',
    -- Explicit arXiv revisions; an absent version does not mean v1.
    version          integer CHECK (version > 0),
    version_history  jsonb NOT NULL DEFAULT '[]',
    doi              text,
    journal_ref      text,
    license          text,
    body             text,
    body_source      text,                       -- html | pdf
    body_fetched_at  timestamptz,
    search           tsvector GENERATED ALWAYS AS
                         (to_tsvector('english', title || ' ' || abstract)) STORED
);
CREATE INDEX IF NOT EXISTS papers_published_idx ON papers (published DESC);
CREATE INDEX IF NOT EXISTS papers_search_idx ON papers USING gin (search);

CREATE TABLE IF NOT EXISTS runs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    window_from  timestamptz,
    window_to    timestamptz,
    fetched      integer NOT NULL DEFAULT 0,
    inserted     integer NOT NULL DEFAULT 0,
    updated      integer NOT NULL DEFAULT 0,
    cost         numeric(10, 4) NOT NULL DEFAULT 0,
    error        text,
    intake       jsonb
);
-- Per-job counts (triaged, candidates, graded, failed, ...), free-form by job kind.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS counts jsonb NOT NULL DEFAULT '{}';

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
-- step fails. Only validated outputs are reusable. NULL cost means unknown.
-- `operation` is the gateway role.
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

-- Events are written in the same transaction as the data they describe. Integrations read
-- them in id order, each with its own cursor.
CREATE TABLE outbox (
    id bigserial PRIMARY KEY,
    type text NOT NULL,
    paper_id text,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
