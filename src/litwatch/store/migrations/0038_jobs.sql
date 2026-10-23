-- The owner's queue (docs/plans/jobs.md; DESIGN.md, Jobs): work litwatch runs outside the
-- nightly run, a chunk at a time under the run lock, giving way to the nightly.
--
-- `scope` names the job's papers (a Process job: {"papers": [id]}); `papers` is their count when
-- the job was made and `done` how many of them its chunks have worked; `counts` what its steps
-- recorded since the owner last queued it. `state`: offered and stopped wait on the owner (Not
-- run); tonight waits for a nightly run to finish after `queued_at`; queued waits for the worker;
-- running is the session's in `backend_pid` and `backend_start`, and is taken back once that
-- session is gone, as a run's is; failed and done; discarded is hidden.
CREATE TABLE jobs (
    id             bigserial PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('process')),
    scope          jsonb NOT NULL,
    state          text NOT NULL CHECK (state IN ('offered', 'stopped', 'tonight', 'queued',
                                                  'running', 'failed', 'done', 'discarded')),
    papers         integer NOT NULL,
    done           integer NOT NULL DEFAULT 0,
    counts         jsonb NOT NULL DEFAULT '{}',
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    queued_at      timestamptz NOT NULL DEFAULT now(),
    changed_at     timestamptz NOT NULL DEFAULT now(),
    backend_pid    integer,
    backend_start  timestamptz
);
CREATE INDEX jobs_open ON jobs (queued_at) WHERE state IN ('tonight', 'queued', 'running');
-- One Process job not finished per paper, so Process asked again finds it.
CREATE UNIQUE INDEX jobs_one_process ON jobs ((scope -> 'papers'))
    WHERE kind = 'process' AND state NOT IN ('done', 'discarded');

-- Each time the worker runs a job, from claim to stop, is one run of kind `job`.
ALTER TABLE runs ADD COLUMN job_id bigint REFERENCES jobs (id);
