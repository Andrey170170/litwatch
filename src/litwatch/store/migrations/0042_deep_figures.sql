-- A Deep read's figures (docs/plans/deep-read.md, "Figures and equations"; docs/integrations.md,
-- deep's result): the paper's own figures the read shows, each under the name the read's `fig`
-- blocks cite, as the deep service sent it (WebP today). They belong to the paper's card:
-- `put_card` replaces them with each card it writes, in the card's transaction, and the paper
-- page reads them at /api/papers/{id}/deep/figures/{name}. A Deep card from before has none.
CREATE TABLE deep_figures (
    paper_id text NOT NULL REFERENCES deep_cards (paper_id) ON DELETE CASCADE,
    name     text NOT NULL,
    mime     text NOT NULL,
    data     bytea NOT NULL,
    PRIMARY KEY (paper_id, name)
);
