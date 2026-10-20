-- The status document (failure-handling plan, P4; data.md, the status document).
--
-- What each model provider last said: its latest `account` error and its latest reply, as the
-- gateway or a batch saw them. A provider is failed for its account from the error until a
-- reply comes after it. An account error on a batch no call waits on (a dead run's, read later)
-- has no call of its own to journal it on, and a row there would distort the calls and their
-- cost, so the observations live here.
CREATE TABLE provider_observations (
    provider          text PRIMARY KEY,
    account_error     text,
    account_error_at  timestamptz,
    replied_at        timestamptz  -- any answer it sent, refused or invalid; never a reused one
);

-- From the journal so far: an account error is known only by when its call started.
INSERT INTO provider_observations (provider, account_error, account_error_at, replied_at)
SELECT via, (array_agg(error ORDER BY started_at DESC) FILTER (WHERE error LIKE 'account%'))[1],
       max(started_at) FILTER (WHERE error LIKE 'account%'),
       max(responded_at) FILTER (WHERE NOT coalesce((usage->>'reused')::boolean, false))
FROM (SELECT usage->>'via' AS via, * FROM inference_calls) calls
WHERE via IS NOT NULL GROUP BY via;

-- The replies a night had, whichever run's calls they settled (a dead run's batch, read later).
CREATE INDEX inference_calls_responded_idx ON inference_calls (responded_at)
    WHERE responded_at IS NOT NULL;

-- When a full-text pass last failed: a stop is counted in the night it happened, whenever its
-- try was claimed.
ALTER TABLE fulltext_passes ADD COLUMN failed_at timestamptz;
