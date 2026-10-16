-- "Use the PDF" (N5b; docs/specs/v1/DESIGN.md, Needs you): the owner has a full-text pass that
-- waits on them read the paper's PDF instead of its LaTeX source, for a paper whose LaTeX reads
-- badly. It holds for every later try of that pass.
ALTER TABLE fulltext_passes ADD COLUMN use_pdf boolean NOT NULL DEFAULT false;
