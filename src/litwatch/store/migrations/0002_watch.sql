-- Topics, what triage and grading say about papers, the owner's labels, and evaluation.

CREATE TABLE topics (
    slug        text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('watch', 'radar', 'pulse')),
    subject     text NOT NULL,
    rubric      text,
    interests   text,
    keywords    text[] NOT NULL DEFAULT '{}',
    status      text NOT NULL CHECK (status IN ('draft', 'active', 'paused')),
    version     text NOT NULL,                  -- content hash of the current revision
    revision    integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- A hash says whether two versions differ, not which is newer; the revision orders them.
CREATE TABLE topic_versions (
    topic     text NOT NULL REFERENCES topics (slug) ON DELETE CASCADE,
    revision  integer NOT NULL,
    hash      text NOT NULL,
    content   jsonb NOT NULL,
    saved_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic, revision)
);

-- One row per paper that has been through triage. A paper without a `done` row is untriaged;
-- a rewritten revision deletes the row. `answers` keeps every topic's answer, yes or no.
CREATE TABLE triage (
    paper_id       text PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
    paper_version  integer,
    state          text NOT NULL CHECK (state IN ('done', 'failed', 'migrated')),
    answers        jsonb NOT NULL DEFAULT '{}',   -- topic slug -> {yes, why}
    revisions      jsonb NOT NULL DEFAULT '{}',   -- topic slug -> revision asked about
    model          text,
    failed_runs    integer NOT NULL DEFAULT 0,
    last_error     text,
    triaged_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX triage_failed_idx ON triage (paper_id) WHERE state = 'failed';

-- Triage placed the paper in a Radar or Pulse topic's field.
CREATE TABLE memberships (
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic           text NOT NULL,
    topic_revision  integer NOT NULL,
    paper_version   integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic)
);
CREATE INDEX memberships_topic_idx ON memberships (topic, created_at DESC);

-- A paper put forward for a Watch topic, by triage, the keyword audit or both. The row
-- carries its grading state, since the call journal knows only the paper.
CREATE TABLE candidates (
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic           text NOT NULL,
    topic_revision  integer NOT NULL,
    paper_version   integer,
    by_triage       boolean NOT NULL DEFAULT false,
    by_keyword      boolean NOT NULL DEFAULT false,
    reason          text,                          -- triage's sentence, or the keyword matched
    state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'graded')),
    failed_runs     integer NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic, topic_revision)
);
CREATE INDEX candidates_pending_idx ON candidates (topic) WHERE state = 'pending';

-- Each grading is its own row, so a re-run over the same versions adds one. Which row is
-- current is decided by version first and time second (core.current_verdict).
CREATE TABLE verdicts (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic           text NOT NULL,
    topic_revision  integer NOT NULL,
    paper_version   integer,
    grade           text NOT NULL CHECK (grade IN ('must_read', 'relevant', 'adjacent', 'none')),
    split           boolean NOT NULL DEFAULT false,
    judge           jsonb,
    check_opinion   jsonb,
    migrated        boolean NOT NULL DEFAULT false,
    recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX verdicts_paper_topic_idx ON verdicts (paper_id, topic);
CREATE INDEX verdicts_topic_recorded_idx ON verdicts (topic, recorded_at DESC);

-- The owner's word on a paper for a Watch topic: the only ground truth there is.
CREATE TABLE labels (
    paper_id     text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic        text NOT NULL,
    value        text NOT NULL CHECK (value IN ('relevant', 'not_relevant', 'unsure')),
    grade        text CHECK (grade IN ('must_read', 'relevant', 'adjacent', 'none')),
    origin       text NOT NULL,                  -- how the paper was chosen
    provisional  boolean NOT NULL DEFAULT false, -- accepted in bulk, not read
    note         text,
    labelled_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, topic)
);

-- Model output kept to catch regressions: 0.14's verdicts and the silver part of the 0.x
-- label file. Not ground truth, and it contains none of the papers 0.14 itself missed.
CREATE TABLE regression (
    paper_id  text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic     text NOT NULL,
    tier      smallint NOT NULL CHECK (tier BETWEEN 1 AND 4),
    source    text NOT NULL,                     -- legacy | silver
    PRIMARY KEY (paper_id, topic)
);

-- Replays, topic previews, samples and miss traces. Dry runs: the same triage, audit and
-- grading as production, written here and nowhere else.
CREATE TABLE eval_runs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,                  -- replay | sample | preview | miss
    run_id       bigint REFERENCES runs (id),    -- carries the cost
    note         text,
    models       jsonb NOT NULL,                 -- role -> provider, model, effort
    topics       jsonb NOT NULL,                 -- topic slug -> revision
    params       jsonb NOT NULL DEFAULT '{}',    -- seed, pool, strata, ...
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE TABLE eval_results (
    eval_run_id    bigint NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    paper_id       text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic          text NOT NULL,
    paper_version  integer,
    triage_yes     boolean,                      -- NULL when triage failed
    triage_why     text,
    keyword        text,                         -- the pattern matched, Watch topics only
    judge          jsonb,
    check_opinion  jsonb,
    grade          text,
    split          boolean,
    error          text,
    PRIMARY KEY (eval_run_id, paper_id, topic)
);

-- Papers drawn for the owner to label, in the order drawn.
CREATE TABLE eval_samples (
    eval_run_id  bigint NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    paper_id     text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    stratum      text NOT NULL,
    position     integer NOT NULL,
    PRIMARY KEY (eval_run_id, paper_id)
);
