-- A run records the database session of the process running it, so a run whose process died
-- (SIGKILL, OOM) without closing its record reads as failed rather than running: the session
-- ends with the process (docs/plans/v1-deploy.md, telemetry).
ALTER TABLE runs ADD COLUMN backend_pid integer, ADD COLUMN backend_start timestamptz;
