-- The full-text queue (N3; docs/specs/v1/pipeline.md, section 6): the passes that read a paper's
-- text, the Briefs they write, and the pass each full-text Weight and Gist came from.

-- A pass is queued for one paper revision and records what it read: the arXiv version of the
-- text (NULL while unread, or when the archive could not say), its source and a hash. A new pass
-- is awaiting text until the archive has the text; `ready` is a pass to be tried again (after a
-- failure, or by the owner); `running` holds a lease, and one whose lease ran out is tried again.
-- `tried_at` is when a worker last took it, so passes still awaiting text take turns.
-- `retry_text`: tried again (a nightly retry, the owner's "run now"), the archive is to try
-- again the downloads it gave up on, at the pass's next read; kept until that is asked.
CREATE TABLE fulltext_passes (
    id               bigserial PRIMARY KEY,
    paper_id         text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision   integer NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('brief', 'weight', 'deep')),
    reason           text NOT NULL,
    state            text NOT NULL DEFAULT 'awaiting_text'
                     CHECK (state IN ('awaiting_text', 'ready', 'running', 'done', 'failed')),
    priority         integer NOT NULL,
    queued_at        timestamptz NOT NULL DEFAULT now(),
    tried_at         timestamptz,
    text_arrived_at  timestamptz,
    arxiv_version    integer,
    text_source      text CHECK (text_source IN ('latex', 'pdf')),
    text_hash        text,
    paper_updated    timestamptz,  -- the paper's arXiv update when the text was read for it
    lease_until      timestamptz,
    failed_runs      integer NOT NULL DEFAULT 0,
    retry_text       boolean NOT NULL DEFAULT false,
    last_error       text,
    finished_at      timestamptz
);
CREATE INDEX fulltext_passes_paper ON fulltext_passes (paper_id, paper_revision);
CREATE INDEX fulltext_passes_open ON fulltext_passes (priority, tried_at) WHERE state <> 'done';
-- One text pass (a Brief or a Weight re-check: the same call) open per paper revision.
CREATE UNIQUE INDEX fulltext_passes_one_open ON fulltext_passes (paper_id, paper_revision)
    WHERE state <> 'done' AND kind <> 'deep';

-- Every Brief a paper was given. The current one is from the pass queued last for the paper's
-- current revision; one from an older revision is kept, labelled with it, until a new one exists.
CREATE TABLE briefs (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision  integer NOT NULL,
    arxiv_version   integer,
    pass_id         bigint NOT NULL REFERENCES fulltext_passes (id) ON DELETE CASCADE,
    text            text NOT NULL,
    overstatement   text,
    text_source     text NOT NULL CHECK (text_source IN ('latex', 'pdf')),
    text_hash       text NOT NULL,
    model           text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX briefs_paper ON briefs (paper_id, paper_revision);

-- The pass a full-text Weight or Gist came from; NULL for those from the abstract. The current
-- one is the full-text pass queued last for the current revision, else the newest from the
-- abstract: `ORDER BY pass_id DESC NULLS LAST, id DESC`.
ALTER TABLE weights ADD COLUMN pass_id bigint REFERENCES fulltext_passes (id) ON DELETE CASCADE;
ALTER TABLE gists ADD COLUMN pass_id bigint REFERENCES fulltext_passes (id) ON DELETE CASCADE;
