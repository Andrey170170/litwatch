# Deep read

Approved by the owner on 2026-11-09. This plan is the build reference for [`docs/specs/deep-read.md`](../specs/deep-read.md): the Deep card is replaced by the Deep read. The plan amends the spec's figure source and "Left to the build plan".

Where this plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

The Deep read gives a paper's method and results at close to the paper's own detail. It has a claims ledger, the paper's figures, rebuilt tables and diagrams, and sections laid out as rungs of the paper page. One run of the `deep` agent writes it. In that run the agent sees its draft through a render tool, `submit` runs code checks, and a reviewer on the same model sends the read back for at most two revisions.

The work comes in five stages, each its own PR (a hard change in `git-workflow`'s terms):

1. **P1, the lens fix:** a small PR that ships first and alone. It stops today's leak of the CoT extensions into unrelated papers.
2. **P2, the format and the web app,** built against fixture reads.
3. **P3, litwatch's consumers:** storage, figures, Notion, Zotero, MCP and trace.
4. **P4, the agent.**
5. **P5, a check on five real reads.** The owner's read is the gate. Deploying P4 and P5 together switches automatic reads.

The order means nothing produces a read before everything that consumes one is ready for it.

One Deep read run:

```text
litwatch → deep: main text + outline, graded topics (D4)
  writer session (run_code)
    read by section, outline, write
    render(read, target, width)   → screenshots via litwatch's /render page (D1)
    figure(name) / pdfPage(n) / crop(page, box)
                                  → e-print from arXiv, PDF via litwatch (D2)
    submit(read) → schema + code checks → errors back to the writer
  reviewer call (same model)      → pass, or findings → writer revises (≤ 2 times)
  pass, or still failing (a `review` process note)
deep → litwatch: deep_cards.card (format "read"), deep_figures, deep_attempts.reviews
  → web paper page · Notion short form · Zotero (no note) · MCP (glance + sections)
```

## Choices made without asking

**Storage and shape**

- **One table, a format tag.** A Deep read is stored in `deep_cards.card` with `"format": "read"`. An old card has no tag and renders with today's component. Passes, attempts, failures, "Write again" and Needs you carry over unchanged.
- **The card's shared fields keep their shapes.** `process_notes`, `among_collection` and `code_url` stay top-level fields of the read, in today's shapes. So the SQL behind Needs you (`store/integrations.py` `process_notes`, `_NOTED`, `deep_notes`), the API's collection rows (`api/integrations.py` `card()`) and the Code link (`Actions.tsx`, `Header.tsx`) work untouched. Related work renders from them.
- **One schema, typed end to end.** The read's TypeBox schema in `deep/` is the source. A script exports it as JSON Schema to `web/src/deepread/read.schema.json`, and the web types are generated from that file. CI fails when the file is stale, as it does for `web/openapi.json`. litwatch stores the read as JSON (`schemas.DeepCard.card` stays `dict[str, Any]`) and checks only the format tag.
- **The block set is the sketches'** (`docs/specs/deep-read/sketches/lib/deep.js`): `p`, `h3`, `list`, `why`, `note`, `eq`, `fig`, `xp`, `table` (cell notes, heat), `diagram`. Sections carry a kind, which sets their colour, and a takeaway. The two sketch reads (`cards/icl.js`, `cards/scarce.js`), converted to the schema, are the fixtures for tests and for the dev walk.

**Figures and equations**

- **Diagram layout is done once, in `deep/`.** elkjs lays a diagram out when the agent checks or renders a draft, and the stored read always carries coordinates, so the web app only draws. Nodes given exact coordinates keep them, and edges may carry waypoints.
- **Equations** are rendered with KaTeX in the web app. `submit` parses every equation with KaTeX and rejects any that fail.
- **Figures** are sent as WebP, at most 1600 px wide, with the result (the finished job's `figures`, beside `result`, as its `reviews` are: the read's schema takes no other fields; `docs/integrations.md`). litwatch keeps them in a new `deep_figures` table (paper, name, bytes), replaced on each write, and serves them at `/api/papers/{id}/deep/figures/{name}`, about 1 MB per read.
- **PDF-only papers:** the agent looks at a page and gives a crop box, and `crop` cuts the figure out. There is no caption-matching heuristic.

**The run**

- **Images ride on `run_code`.** The agent keeps its single tool. The new session functions (`render`, `figure`, `pdfPage`, `crop`) queue images, which come back with that cell's output: pi-ai's tool results carry `ImageContent`.
- **The render page takes no token.** It holds no data and fetches none. litwatch serves it like the app's other static files, Chromium hands it the draft (figures as data URLs), and the page makes no API call.
- **Chromium in the deep image:** `playwright-core` with Playwright's `chromium-headless-shell`, pinned together. The browser's network access is limited to the render page's origin, and every other request is refused.
- **The reviewer** is one model call on the same model, at high effort, with no session.
  - In: the paper's text, the read as JSON, the rubric, and as image content the figures the read cites for values read off a plot, so those values can be checked against the plot.
  - Out: a verdict through a single `review` tool that its schema validates: four scores, the structure checks, and findings sorted by importance.
  - Its findings go to the writer as a new message in the writer's own conversation, so the writer keeps its context while revising.
- **Budget:** 150 cells, a 120-minute wall clock (`DEEP_WALL_CLOCK_MIN`) and high effort (`DEEP_EFFORT`) for the writer. litwatch's `timeout_minutes` rises to 135, above that wall clock (the module docstring says why). All of these stay settings, and P5 measures them; it set 100 cells, 60 minutes and a timeout of 75 (`evals/2026-11-10-deep-reads.md`).
- **Stored reviews:** a new `deep_attempts.reviews` column (jsonb) holds each round's scores and findings, and `litwatch deep trace` prints them. A read still failing after two revisions gets a process note of a new kind, `review` (source `service`), naming the failing scores and checks. Needs you shows it like the others.
- **Prose rules:** the writer's prompt takes the READER rules from `src/litwatch/writing.py` and adds the spec's three prose faults. They are copied into `deep/src/prompt.ts`, with a comment pointing back to `writing.py`.

**The paper page**

- **Layout:** the side column is 128 px on every paper page, not only on pages with a read (today 104 px, `.rung` in `web/src/styles.css`). That keeps the Gist and Brief in the same place from paper to paper. The 1200 px cap (`.inner:has(.pg)`, today 1080) applies only to a page showing a Deep read.

**Other surfaces**

- **Notion:** a read exports as the spec's short form. An old card keeps today's callouts byte for byte, so its body hash is unchanged and `setup()` doesn't rewrite its page. The guide page (`_guide_blocks`) gets a line about the new shape. Only reads are guarded against the extension fields (`_section`).
- **Zotero:** for a read, no `litwatch:enrichment` note and no card tags. The guard covers the block in `handle` where `enrichment_note` and `enrichment_tags` are called. The inbox condition stays, and a read counts as ready. Old cards keep their notes.
- **MCP:** `get_paper` gives a read's At a glance and its sections with their takeaways. A new `read_deep(paper_id, section)` returns one section as text: tables as Markdown, figures and diagrams as their captions and node notes. Old cards are dumped as today (`_card`).
- **Stack smoke test:** the fake deep (`tests/stack/fake_deep.py`) returns a fixture read. The page walk covers a read, and keeps an old-card paper.

## Decisions

- **D1: litwatch serves the render page.**
  - What it is: Vite builds a second entry, `render.html`, which litwatch serves beside the app. The deep service's Chromium opens it at `LITWATCH_URL` and hands it the draft, so the agent sees exactly the components that are deployed.
  - Why: nothing about the deep image's build changes, while bundling would widen its build context to the repository root and couple the two builds.
  - Cost: render needs litwatch up and on the same version. They are already deployed together. If render fails, the run carries on with a process note.
- **D2: the deep service fetches the e-print itself.**
  - What it is: `figure()` downloads the paper's e-print from arXiv once per run, at the version litwatch read. The submission doesn't carry that version today: P3 adds `PaperText.version` to `Deep.submission()`, and P4 keeps it through the service's request, job and session types (`server.ts`, `jobs.ts`, `PaperInput`). When litwatch doesn't know the version (`PaperText.version` is None), the figures come from the latest e-print and a process note says so, with the contact User-Agent the service already sends. Figure PDFs and images are rendered with poppler; EPS and missing sources fall back to the PDF crop. The PDF comes through litwatch's `/api/papers/{id}/pdf`, added to the read token's `READABLE` list (`api/deps.py`).
  - Why: the archive drops figure files at ingest (`archive/src/arxiv_archive/source_text.py`) and keeps no original. Changing that would mean a contract change and fetching the source again for every paper it holds, all for about ten reads a day.
- **D3: the owner's read, plus the run's own record.**
  - What it is: five real reads on a dev instance, the two sketch papers and three of today's set of different kinds. Each read's reviewer scores, check rejections, rounds, time and usage go into an `evals/` report. The owner reads the five, and P5 merges once they pass.
  - Follow-ups: `docs/agents/verification.md`'s known gap is updated to say this, and a Deep quality harness (`litwatch eval deep`, with a judge from another lab) goes to the backlog.
  - Why: it costs nothing on the API, since the subscription pays, and a judge would have to be proven first.
- **D4: the first message carries the outline and the main text. The appendix and bibliography come on demand.**
  - What it is: the main text runs up to LaTeX's `\appendix`, or a PDF's Appendix heading, else the whole text up to today's 200k-character cap. The bibliography is cut out on its own as well, wherever it sits: the archive inlines it (`thebibliography`, or a References heading in a PDF's text), often before the appendix. The appendix and references are read through `section()`, `find()` and `refs()`, and the complete text stays in the session.
  - Why: a Deep read needs nearly all of the main text, so having it in the message saves cells. The appendix, often the larger half, enters the context only where the read needs it. The owner's point: the agent reads programmatically, so it should not be handed what it hasn't asked for.

## Risks

- **R1: the image route may not work on the subscription.** pi-ai declares image input for `gpt-6.1-sol`, but nothing has sent an image through the openai-codex route yet. P4 opens with a one-call spike that sends one image. If images are refused, the plan stops and comes back to the owner before anything else in P4 is built.
- **R2: the number check may reject honest reads.**
  - Papers write numbers in many forms (0.34 vs 34%, 1.2k, ±). The check normalises these forms.
  - A value read off a plot carries its figure cite and goes to the reviewer instead of being checked.
  - Each error names the number and its block. P5 records how often a submit is rejected.
  - If code proves too brittle, a cheap extractor pass (GPT-6-Luna, low effort) matches each number in the read to where it appears in the paper before the check runs. Only if that still fails does the check become a warning to the reviewer instead of a hard stop.
- **R3: longer runs could hit the subscription's limits.** A read is up to three writes and three reviews. The existing service-failure pause (`deep_attempts.service_failed_at`) handles a usage limit. P5 measures time and tokens per read, and the budget is settings.
- **R4: the writer's context may fill up.** Less likely after D4: the writer reads the appendix by section, and the reviewer is a fresh call. P5 records peak input tokens per read.
- **R5: consumers could break on the new shape.** Zotero (`enrichment_tags`, `enrichment_note`) and Notion (`_section`) read card fields without checking that they exist. A read would raise a `KeyError` on every Zotero event for that paper. P3 ships their guards before P4 can produce any read, and P3's tests run every consumer on a fixture read.

## Stages

Each stage is its own PR, started after the previous one merges.

| Stage | What it builds | Shows before the next starts |
|---|---|---|
| P1 | **Lenses for graded topics**, in today's card. `_topics` (`src/litwatch/integrations/deep.py`) returns the topics that graded the paper above none at any depth (Focus, Area and Field, by effective grade; today's `focus_grades` reads Focus only), with no fallback to every Focus topic. An Area pick still triggers a read but becomes a lens only through its grade. Which papers get a read is unchanged: this is only what a read is written for. The writer's prompt (`deep/src/prompt.ts`) stops saying the listed topics graded the paper must read, and names each one's grade instead. Extensions follow from those topics. A small PR, deployed at once. | V1. After deploy, a requested card on a non-CoT paper carries no CoT extension. |
| P2 | **Format and web app.** The TypeBox schema, its JSON Schema and generated types, and the fixture reads. The `DeepRead` components: sections as rungs; every block kind; the claims ledger; tables with cell notes and column highlight; diagrams with tips and highlight; figures with zoom; KaTeX. Also the rail contents, the phone strip of section chips, the widths, the format switch beside `DeepCard`, and the `render.html` entry (D1). | V2. Both fixture reads walked in the real app at both widths and in both themes. Old cards unchanged. |
| P3 | **litwatch's consumers.** The format tag. The `deep_figures` table, how the worker stores figures, and the figure endpoint. The `deep_attempts.reviews` migration and the trace output. The `review` note kind. The Notion short form and failed-review line. The Zotero guards. MCP `get_paper` and `read_deep`. The PDF on the read token. The fake deep returning a read. `integrations.md` and `verification.md` updated. | V3, V4, and the stack smoke test green. |
| P4 | **The deep agent.** The R1 spike first. Then: the new schema in `submit`; the code checks (numbers, figure and table cites, verdict and CARD-note locations, KaTeX); the prompt (writer's standards, lenses, three faults, the rubric for the reviewer); the D4 first message (main text without the appendix and bibliography); the session functions (`render`, `figure`, `pdfPage`, `crop`); Chromium and poppler in the image; the reviewer loop; the budget; the reviews returned to litwatch with the outcome. | V5, V6. |
| P5 | **Check on real papers, then switch.** Five reads and the `evals/` report (D3), with the budget tuned from the measurements. The owner's read is the merge gate. Deploying P4 and P5 together switches automatic reads. | V7. |

## Verification

By [`docs/agents/verification.md`](../agents/verification.md).

| Claim | Evidence |
|---|---|
| V1. A card is written only for the topics that graded the paper, and a requested card on an ungraded paper gets no topic context. | A pytest on `_topics` and `submission` (graded in Agents only; graded in an Area topic that didn't pick it; graded in a Field topic; no grade at all; graded in the CoT topics) that fails before the change and passes after. |
| V2. The paper page renders a Deep read as the spec and sketch variant C describe, and old cards still render. | Vitest: the contents list, the phone strip, the claims toggle, table and diagram tips, the format switch. A walk on the sandbox through T3's browser panel with both fixture reads and an old card: screenshots at 390 and 1440 px, dark and light, compared against the sketch. |
| V3. Every consumer handles a read and leaves old cards as they were. | Pytest on a fixture read and an old card. Notion: the short form, a failed-review line when the read failed, an old card's body hash unchanged. Zotero: no note or tags for a read, the inbox still set. MCP: glance and sections, `read_deep` by section. API: the figure endpoint, the PDF on the read token. Trace: reviews printed. Needs you: the `review` note. |
| V4. Migrations keep existing data intact. | The `reviews` and `deep_figures` migrations on a sandbox copied from production, with counts of cards, attempts and notes before and after. |
| V5. The run's loop does what the spec says. | `npm test` with a fake model. The code checks reject a number missing from the paper, a missing figure cite, a missing verdict location and broken TeX. A plot value with its figure cite passes. Also covered: the reviewer's pass rule (every score ≥ 4 and every check passing); the first write plus at most two revisions; a still-failing read kept with a `review` note; `render`, `figure` and `crop` returning images; the reviewer receiving the plot figures cited. The first message leaves out the appendix and the bibliography, with references before the appendix and with no appendix at all. |
| V6. The image builds, and a real run works end to end. | The stack smoke test. One real read by the newly built deep image (not a host Node process), run as a dev container on its own port and pointed at a dev `serve`, so its Chromium and poppler are the ones exercised. Its trace shows a draft rendered, a figure converted from the e-print and one cropped from the PDF, the review rounds and the stored read. A paper whose read version is older than its latest gets that version's figures. |
| V7. The reads are good enough to switch on. | Five reads, the `evals/` report, and the owner's read, listed as pending in the P5 PR until done. |

## Scope

**In:** everything in the spec. The topics fix ships first, ahead of the rest. `integrations.md` and `verification.md` are updated where the Deep card's description changes.

**Out:**
- The spec's out-of-scope list: interactive charts, bulk remake, a cap, a cross-model reviewer, a regen UI.
- The archive keeping figures (D2).
- A Deep quality harness, which goes to the backlog (D3).
- Sharing the prose rules between Python and TypeScript, also backlog.

## Notes for later stages

Found while building, owned by the stage named.

- **P3:** the figure endpoint answers 404 for a name the read doesn't have. litwatch checks a submitted read's format tag before storing it.
- **P4:**
  - `submit` validates against a variant of the read schema: process notes the model may write (no `review` kind, no `service` source), and diagram coordinates optional until elkjs lays the diagram out.
  - The code checks also cover edges that name existing nodes, unique ids, each table row's cell count against its columns, and verdict anchors.
  - Diagram nodes are at least 40 px tall, so they stay tappable on the phone.
  - The exported schema is about 29 KB; mind it in the prompt.
  - `render.html` draws the desktop read at the paper page's widest column (854 px, a 710 px body), whatever the viewport; render the phone at 390 px. A narrower desktop wraps more than the render shows.
  - elkjs spacing leaves each arrow at least 40 px of length between boxes, so it stays tappable on the phone.
  - `renderRead()` resolves before a figure that fails to decode has swapped to its placeholder. Render only figures `figure()` or `crop()` produced, or wait for the image's error before the screenshot.
- **P5 (the deploy):** the production stack (`/opt/homelab-config/stacks/litwatch`) overrides the budget with `DEEP_EFFORT` medium and `DEEP_MAX_CELLS` 40. Its `litwatch.toml` comment still names a 45-minute timeout over a 30-minute wall clock. Its deep container runs with a read-only root, 1 GiB of memory, 128 pids and a 64 MB `noexec` `/tmp`. V6 runs the dev container under those same limits, and the deploy updates the overrides, the comment and any limit V6 shows is too tight.
  - The service asks litwatch's `/api/papers/{id}/pdf` for its text's version (`?version=<n>`), which the archive is then asked for; V6 cropped a PDF-only paper's figures through it.
  - A refused place or number the writer checked can be stood by (`submit(read, [items])`); it goes to the reviewer by name and `litwatch deep trace` prints it on the submit. The `evals/` report counts the items stood by per read, and which were right, so overuse or a check that misfires shows.
  - Fix first: a PDF's Markdown text whose contents list sits in headings (`### References`, `### Appendix A` before `## 1 Introduction`) starts its cuts at the list, so the first message keeps little main text; the full text stays in the session. `pdfParts` guards only plain lines with the Introduction's start: guard the marked References and Appendix headings the same way, keeping an appendix before the references and a paper without an Introduction as they are (PR #148, CodeRabbit). Then check each PDF read's first message has its main text.
