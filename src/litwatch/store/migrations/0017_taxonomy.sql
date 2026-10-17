-- The taxonomy in the database (N2a): the class tree and the followed topics, each versioned
-- (docs/specs/v1/pipeline.md, section 10). Databases before the restart are disposable (build
-- plan, D1), so the old topics are dropped rather than carried over. Other tables keep naming
-- a topic by plain text: its class's slug.

DROP TABLE topic_versions;
DROP TABLE topics;

-- A class missing from a saved taxonomy is retired, not deleted: what was recorded against it
-- keeps its row, and saving the class again brings it back. `position` is the place in tree
-- order (parents before children, siblings in the owner's order).
CREATE TABLE classes (
    slug         text PRIMARY KEY,
    parent       text REFERENCES classes (slug),
    name         text NOT NULL,
    description  text NOT NULL,
    threshold    double precision CHECK (threshold > 0 AND threshold < 1),
    position     integer NOT NULL,
    version      text NOT NULL,                 -- content hash of the current revision
    revision     integer NOT NULL,
    retired_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- A hash says whether two versions differ, not which is newer; the revision orders them.
CREATE TABLE class_versions (
    class     text NOT NULL REFERENCES classes (slug),
    revision  integer NOT NULL,
    hash      text NOT NULL,
    content   jsonb NOT NULL,
    saved_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (class, revision)
);

-- A followed class. A class no longer followed loses its row; its versions stay, and following
-- it again goes on from the last of them.
CREATE TABLE topics (
    class       text PRIMARY KEY REFERENCES classes (slug),
    depth       text NOT NULL CHECK (depth IN ('focus', 'area', 'field')),
    subject     text NOT NULL,
    rubric      text,
    interests   text,
    keywords    text[] NOT NULL DEFAULT '{}',
    monogram    text NOT NULL,
    colour      text NOT NULL,
    position    integer NOT NULL,               -- the owner's order
    status      text NOT NULL CHECK (status IN ('draft', 'active', 'paused')),
    version     text NOT NULL,
    revision    integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE topic_versions (
    class     text NOT NULL REFERENCES classes (slug),
    revision  integer NOT NULL,
    hash      text NOT NULL,
    content   jsonb NOT NULL,
    saved_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (class, revision)
);
