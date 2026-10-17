-- A label of adjacent: a source of ideas for the topic without being about it.
ALTER TABLE labels DROP CONSTRAINT labels_value_check;
ALTER TABLE labels ADD CONSTRAINT labels_value_check
    CHECK (value IN ('relevant', 'adjacent', 'not_relevant', 'unsure'));
