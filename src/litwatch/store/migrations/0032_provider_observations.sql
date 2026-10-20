-- What each model provider last said (failure-handling plan, P4; data.md, the status document):
-- its latest `account` error and its latest reply, as the gateway saw them, whichever call or
-- batch they came from. A provider is failed for its account from the error until a reply
-- comes after it. An account error on a batch no call waits on (a dead run's, read later) has
-- no call of its own to journal it on, and a row there would distort the calls and their cost.
CREATE TABLE provider_observations (
    provider          text PRIMARY KEY,
    account_error     text,
    account_error_at  timestamptz,
    replied_at        timestamptz  -- any answer it sent, refused or invalid; never a reused one
);
