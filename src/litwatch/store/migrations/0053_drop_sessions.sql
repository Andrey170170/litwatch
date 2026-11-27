-- The old liveness columns go (docs/plans/a2-work-gate.md, P7): runs and job claims are alive by
-- their work item's lease since P3 (`store.runs.ALIVE`), and these were written only for the image
-- before the Work gate. One-way: an image before this one still writes them; its rollback step
-- re-adds them (the plan's "Rolling back from P7").
ALTER TABLE runs DROP COLUMN backend_pid, DROP COLUMN backend_start;
ALTER TABLE jobs DROP COLUMN backend_pid, DROP COLUMN backend_start;

-- The P3 image's CLI command items (kind `command`, run in the command's own process): none is
-- made since P5, and the take-back no longer ends one left open, so any left end here.
UPDATE work_items SET state = 'ended', outcome = 'failed', ended_at = now(),
    error = 'its process stopped', lease_token = NULL, lease_until = NULL, resubmitted = false
    WHERE kind = 'command' AND state <> 'ended';
