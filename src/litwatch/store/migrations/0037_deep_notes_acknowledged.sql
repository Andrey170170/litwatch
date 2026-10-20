-- Acknowledge on Needs you's Deep notes (docs/plans/deep-open-web.md, P3; DESIGN.md, Needs you;
-- decision 82): the owner has seen the card's process notes. The mark holds for the card it was
-- given for: writing the card again (`put_card`) clears it, so a newer card's notes come back.
ALTER TABLE deep_cards ADD COLUMN notes_acknowledged_at timestamptz;
