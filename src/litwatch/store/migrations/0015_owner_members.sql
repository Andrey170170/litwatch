-- A membership records who put the paper in the field: triage (`by_triage`, every row so
-- far) and the owner ("Important for <field>": `by_owner`, with when). Each writes only its
-- own flag; the row goes away only when neither holds. `owner_added_at` brings an older
-- member into the week of the field's next weekly review; a daily review takes every
-- addition still pending (`owner_reviewed_at` null) and marks the ones it included.
ALTER TABLE memberships
    ADD COLUMN by_triage boolean NOT NULL DEFAULT true,
    ADD COLUMN by_owner boolean NOT NULL DEFAULT false,
    ADD COLUMN owner_added_at timestamptz,
    ADD COLUMN owner_reviewed_at timestamptz;
