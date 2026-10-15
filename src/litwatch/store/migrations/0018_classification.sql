-- Classification and analysis as nightly steps (N2a; docs/specs/v1/pipeline.md, sections 2, 3
-- and 9; data.md). Every record names the paper revision it was made from, and a rewrite makes
-- the paper due for both steps again. Classifications, analyses, weights and gists keep every
-- earlier row as history; memberships are only the current projection.

-- One row per classification of a paper: the classifier's whole answer, kept whatever the owner
-- says, so a threshold change can be replayed without new calls. `taxonomy_revision` is the hash
-- of the classes' versions in tree order, which is what the request was built from
-- (`core.Taxonomy.classes_version`). A classification by hand has `model` 'owner' and no response.
CREATE TABLE classifications (
    id                 bigserial PRIMARY KEY,
    paper_id           text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision     integer NOT NULL,
    taxonomy_revision  text NOT NULL,
    primary_class      text NOT NULL REFERENCES classes (slug),
    response           jsonb,          -- {probabilities: {class: p}, choices: {primary_class: {option, probability}}}
    model              text NOT NULL,
    classified_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX classifications_paper ON classifications (paper_id, paper_revision);

-- Direct memberships only; inherited ones follow from the tree. One row per paper, class and
-- source: the current projection, not history. A classification replaces the paper's
-- classifier, uncertain and keyword rows (their history is `classifications`, every probability
-- kept, so a threshold change replays); owner and miss rows are the owner's and stay. `member` is false only for the owner's class label taking
-- the paper out of a class, which outranks every other source for that class. `class_version` is
-- the class's hash when the membership was made (classes.version).
CREATE TABLE memberships (
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    class           text NOT NULL REFERENCES classes (slug),
    source          text NOT NULL
                    CHECK (source IN ('classifier', 'uncertain', 'keyword', 'owner', 'miss')),
    member          boolean NOT NULL DEFAULT true CHECK (member OR source = 'owner'),
    probability     double precision,   -- what brought it in, for classifier and uncertain
    class_version   text NOT NULL,
    paper_revision  integer NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, class, source)
);
CREATE INDEX memberships_class ON memberships (class);

-- One row per analysis from the abstract. `primary_class` is the area the Weight was judged in.
CREATE TABLE analyses (
    id                bigserial PRIMARY KEY,
    paper_id          text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision    integer NOT NULL,
    primary_class     text NOT NULL REFERENCES classes (slug),
    gist              text NOT NULL,
    weight            text NOT NULL CHECK (weight IN ('landmark', 'solid', 'incremental')),
    weight_reason     text NOT NULL,
    contribution      text NOT NULL CHECK (contribution IN
                          ('method', 'analysis', 'benchmark', 'theory', 'position', 'survey')),
    releases_code     boolean NOT NULL,
    releases_weights  boolean NOT NULL,
    model             text NOT NULL,
    analysed_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX analyses_paper ON analyses (paper_id, paper_revision);

-- Every Weight a paper was given. Analysis writes source 'abstract'; the full-text queue (N3)
-- adds 'full_text' rows and the pass that wrote them.
CREATE TABLE weights (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision  integer NOT NULL,
    weight          text NOT NULL CHECK (weight IN ('landmark', 'solid', 'incremental')),
    source          text NOT NULL CHECK (source IN ('abstract', 'full_text')),
    reason          text NOT NULL,
    model           text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX weights_paper ON weights (paper_id, paper_revision);

-- Every Gist a paper was given, as Weight. `arxiv_version` is the version the text was read
-- from, null when unknown.
CREATE TABLE gists (
    id              bigserial PRIMARY KEY,
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    paper_revision  integer NOT NULL,
    arxiv_version   integer,
    text            text NOT NULL,
    source          text NOT NULL CHECK (source IN ('abstract', 'full_text')),
    model           text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gists_paper ON gists (paper_id, paper_revision);

-- A step that failed on a paper's current text, by consecutive runs; a success deletes the row
-- and a new paper revision starts the count again. A paper whose classification failed three
-- runs is awaiting classification; analysis failures are listed after three.
CREATE TABLE step_failures (
    paper_id        text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    step            text NOT NULL CHECK (step IN ('classification', 'analysis')),
    paper_revision  integer NOT NULL,
    failed_runs     integer NOT NULL,
    last_error      text NOT NULL,
    failed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, step)
);
