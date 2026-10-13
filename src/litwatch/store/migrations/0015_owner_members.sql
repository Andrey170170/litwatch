-- A paper the owner put into a Radar or Pulse field themselves ("Important for <field>"):
-- a member like any other for the field's reviews and its page, marked as the owner's own
-- addition so triage never takes it away and the page can say so.
ALTER TABLE memberships ADD COLUMN by_owner boolean NOT NULL DEFAULT false;
