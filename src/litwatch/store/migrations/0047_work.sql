-- The Work gate's queue (docs/specs/architecture/a2-work-gate.md; docs/plans/a2-work-gate.md):
-- one row per work item, one attempt at one unit of work, written when it is submitted.
--
-- `kind` names the Worker that runs it and `key` the work, within the kind: a submission whose
-- key is still open finds the same item, and a `lasting` key (a scheduled fire's) holds for
-- good, its item ended or not. `admission` is what one item at a time is admitted for: its kind,
-- or a delivery's integration. `state`: pending waits for `not_before` and its admission;
-- running is held by the lease `lease_token` until `lease_until`, which the worker's heartbeat
-- renews, and is taken back once that passes; ended has its `outcome`. A submission that finds
-- its key running sets `resubmitted` (and `not_before` to when it is wanted again): that run's
-- end puts the item back to pending instead of ending it. `attempts` counts its leases. `cost` is in dollars, NULL when unknown. A job's chunks name the job's run (`run_id`);
-- a standalone run names its item (`runs.work_id`).
CREATE TABLE work_items (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,
    key           text NOT NULL,
    lasting       boolean NOT NULL DEFAULT false,
    admission     text NOT NULL,
    scope         jsonb NOT NULL DEFAULT '{}',
    state         text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'ended')),
    not_before    timestamptz NOT NULL DEFAULT now(),
    lease_token   uuid,
    lease_until   timestamptz,
    resubmitted   boolean NOT NULL DEFAULT false,
    attempts      integer NOT NULL DEFAULT 0,
    submitted_at  timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    ended_at      timestamptz,
    outcome       text CHECK (outcome IN ('done', 'failed')),
    error         text,
    cost          numeric(10, 4),
    parent_id     bigint REFERENCES work_items (id),
    run_id        bigint REFERENCES runs (id),
    CHECK ((state = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((state = 'ended') = (outcome IS NOT NULL))
);
-- One open item per key, and one item for good per lasting key.
CREATE UNIQUE INDEX work_items_key ON work_items (kind, key) WHERE lasting OR state <> 'ended';
-- One running item per admission, whichever process leased it.
CREATE UNIQUE INDEX work_items_admitted ON work_items (admission) WHERE state = 'running';
CREATE INDEX work_items_pending ON work_items (not_before) WHERE state = 'pending';

-- One row per process serving work items: the kinds it runs and its heartbeat, so a command
-- can tell whether a worker for its kind is there. Removed when the process stops.
CREATE TABLE workers (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL,
    kinds         text[] NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE runs ADD COLUMN work_id bigint REFERENCES work_items (id);
ALTER TABLE inference_calls ADD COLUMN work_id bigint REFERENCES work_items (id);
