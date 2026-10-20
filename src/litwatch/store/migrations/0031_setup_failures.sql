-- A failing `setup` is retried after a backoff and shown under Settings → Integrations, beside a
-- failing event (integrations.md, the outbox). Kept apart from the event's columns, which a
-- failure before a restart may still hold. `setup_failures` is 0 while setup is not failing.
ALTER TABLE integration_cursors
    ADD COLUMN setup_failures  integer NOT NULL DEFAULT 0,
    ADD COLUMN setup_error     text,
    ADD COLUMN setup_failed_at timestamptz,
    ADD COLUMN setup_retry_at  timestamptz;
