-- Published versions (spec section 6). A row says a paper was looked up: `record` is the
-- published record found, or null when there was none. An owner's choice (a record, or
-- "none") is `by_owner` and is never overwritten by a lookup.
CREATE TABLE publications (
    paper_id          text PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
    record            jsonb,
    by_owner          boolean NOT NULL DEFAULT false,
    first_checked_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    checked_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
