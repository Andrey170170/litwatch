-- notion: the database each `notion` integration (by its section's name) keeps under
-- `parent_page`. A start under another parent page drops the row and its pages' rows, so a copy
-- of one database never reuses another's ids or hashes. Null ids are things not made yet.
CREATE TABLE notion_databases (
    integration    text PRIMARY KEY,
    parent_page    text NOT NULL,
    database_id    text,
    data_source_id text,
    guide_page     text,                     -- "How to read this"
    guide_hash     text,                     -- of its body as last written
    all_view       text,                     -- All papers
    -- Every topic ever exported: slug -> {name, column (its grade column's property id), view
    -- (null until made)}. A topic no longer exported stays.
    topics         jsonb NOT NULL DEFAULT '{}',
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- notion: one Notion page per paper. `page_id` is null while a creation is under way: its reply
-- may be lost, so the page is looked for before another is made. The hashes are of what was last
-- written (null: write it again). `topics` holds, per topic the page is or was listed for, what
-- was last written: {grade (the effective grade), shown (the grade word), why, since}.
CREATE TABLE notion_pages (
    integration     text NOT NULL REFERENCES notion_databases(integration) ON DELETE CASCADE,
    paper_id        text NOT NULL REFERENCES papers(id),
    page_id         text,
    properties_hash text,
    body_hash       text,
    topics          jsonb NOT NULL DEFAULT '{}',
    added           date NOT NULL,             -- the day the paper entered the list: "Added"
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (integration, paper_id)
);
