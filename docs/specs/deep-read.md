# Deep read

Status (2026-11-09): **shaped, not built.** This replaces the Deep card (`deep` integration, [v1
integrations.md](v1/integrations.md), "Changed: deep"; [DESIGN.md](v1/DESIGN.md), "The ladder", rung 3) with the Deep
read. The visual reference is [`deep-read/sketches/`](deep-read/sketches/index.html): R1 is the read on its own, R2
puts it in the paper page. Where this spec and the sketches differ, this spec decides.

## Why

The ladder promises, at rung 3, an account of a paper thorough enough to stand in for reading most of it. The Deep
card does not keep that promise. Its summary is capped at two to four paragraphs, about the size of a Brief, and the
rest is a metadata form: models, tasks, prior work, code, limitations. The owner is left with the general picture and
none of the detail needed to understand the method or judge the results, so the owner reads the paper anyway.

The card also reads every paper through topics it may have nothing to do with. Its schema dates from 0.14, when
litwatch watched only CoT papers: two hand-written extensions (CoT faithfulness, CoT monitoring) hang off two Focus
topics, and a card the owner asks for on a paper no Focus topic graded must read is written for every Focus topic. A
multi-agent paper (2610.21029) came back with "not a circuit analysis or a validated chain-of-thought detector" in its
summary and two extension blocks of "not applicable".

## What a Deep read is

The third rung, after the Gist (the point) and the Brief (enough to decide whether to go further). It is that further
reading: the paper's method and results at close to the paper's own detail, without the general background and
introduction the owner usually skips, but with the paper's reasons for doing things the way it did. It is organised
so any part can be found at a glance, and it is more sceptical than the authors: where the paper claims more than it
shows, the read says so and points at where.

For most papers the read replaces reading the paper. The owner opens the paper itself only in the few cases that
need every detail exactly, so the read accepts some LLM looseness of wording in exchange for detail and coverage. Its
length scales with the paper, roughly a third of the main text, and the method and results are never cut to save
length. R1's two reads (about 15 and 12 minutes) are the target.

## The shape of a read

**Sections.** A read is a sequence of sections. A few anchors sit in fixed places on every read; the middle follows
the paper's own story.

- First: **At a glance**. The key claim, three or four points that carry the paper's argument, and the facts (kind,
  models, task or data, methods, code and artifacts, reading time).
- The middle, as the paper needs: a short **problem and motivation** section (what earlier work left open), the
  **method** (setup, environment, taxonomy, whatever the paper builds), and the **findings**, one section each. A
  method paper might go Problem, Method, Experiments; a benchmark Construction, Results, What it measures; an analysis
  Question, then one section per finding.
- Then **Claims**, then one **lens** section per topic the paper is graded in.
- Last, in this order: **Limitations**, **Related work**, **Reproducibility**, then what the read was written from and
  when.

Each section kind has its own colour, the same on every read (At a glance accent, problem neutral, method cyan,
findings blue, claims amber, a lens its topic's colour, limitations red, related work purple, reproducibility green),
so the owner finds a part by colour and place before reading it.

**Blocks.** A section is made of typed blocks: paragraphs, subheadings, lists, equations, experiments, tables,
figures, diagrams, WHY boxes and CARD notes. Which blocks a section uses is the paper's call.

- **Experiment**: the unit of a finding. Its question, then *Did* (what was done), *Found* (the result, with its
  numbers), *Means* (what it shows) and *Not shown* (what it leaves open, including the controls that hold).
- **WHY box**: the paper's reason for a choice, at the choice it explains (why this benchmark, why mean ablation, why
  negotiation time does not count). The reasons the owner would otherwise have to dig out of the introduction live
  here.
- **CARD note**: the read's own sceptical remark, where the paper's text does not say it: a table that does not
  support the abstract, a test the paper does not report, an inconsistency. Every CARD note points at a place in the
  paper.
- **Equation**: rendered (KaTeX in R1), with a line on what it says and what is empirical about it.

**Claims.** One line per main claim of the paper, every claim shown, a verdict chip first: *holds*, *partly*, *not
shown*, *overclaimed*. Holds is quiet; the others are filled, so the eye goes to them. A claim's evidence, with the
section, figure or table it rests on, opens on a click and is out of sight until then. Every verdict cites where in the
paper it comes from.

**Lenses.** A read is written for the topics the paper is graded in, at any depth and with a grade above none: a
multi-agent paper is read as an Agents paper. Each such topic adds a short, objectively worded section on what the
paper means for that topic's question. A paper graded in none of the owner's topics has no lens section. CoT
faithfulness and CoT monitoring keep their richer questions (ground truth, monitor type, pressure and the rest) as
those topics' lens, asked only when the paper is graded in them; nothing is asked globally any more.

**Limitations** are marked as the paper's own or the read's. **Related work** is the closest prior work and the papers
of the collection the paper agrees with, contradicts, extends or supersedes (today's prior work and Among the
collection, kept). **Reproducibility** says what was checked, not assumed: the repository and what it actually holds,
data, models, compute, statistics. **Process notes** stay as they are (decisions 81–82).

## Visuals

- **The paper's own figures**, inline where the read discusses them, with the paper's figure number and a caption in
  the read's words; tap to zoom. They come from the LaTeX source's figure files, rendered to images; for a paper with
  only a PDF, cropped from its pages by caption.
- **Tables rebuilt** from the paper as real tables, cut to the rows that matter and said so in the caption. A cell can
  carry a short note shown on hover or tap; the column under the pointer is highlighted so a number reads against its
  header. A table may be heat-coloured where the values compare.
- **Diagrams drawn by the read**: the method, a pipeline, a circuit. A diagram is nodes, edges and optional lanes, each
  node and edge with a short explanation of what it does, the numbers behind it, and where the paper shows it. Clicking
  one lights it and its neighbours, dims the rest, and shows the explanation under the diagram. By default the app
  lays the diagram out automatically; when every node has coordinates (and edges optionally waypoints), they are used
  exactly, for the cases that need control. On a phone a diagram scrolls sideways.
- Interactivity stays at that level: tooltips and highlights. Interactive charts and step-by-step drawings of a process
  are later; the block format leaves room for them.

## On the paper page

The read is rung 3 of the ladder, named **Deep read**, with each of its sections as a rung of its own: the section's
name in the ladder's side column with its colour square hanging to its left, the section's body beside it (R2,
variant C). The side column is 128 px wide, so long section names wrap under themselves and keep the body's first
line. The layout is open: every section is laid out in full, nothing folded but a claim's evidence.

- A paper page with a Deep read is capped at 1200 px instead of 1080 px, so the reading column is wide enough for
  figures, tables and diagrams.
- On the desktop, the rail gains the read's contents under the quick actions: one row per section with its colour,
  the section in view lit, a click scrolling to it. It is dimmed while the read is not in view.
- On the phone, a sticky strip of section chips appears under the top bar while the read is in view, the current one
  outlined and scrolled into sight.
- A paper with a Deep card from before this change shows it as today, with today's component, until it is written
  again. "Write again" stays and writes a Deep read.

## Which papers get one

Unchanged: automatic for a Focus paper at an effective grade of must read and for an Area pick, and any paper the owner
asks for. What changes is only which topics it is written for (Lenses, above).

## How a read is made

The `deep` service's agent writes it, one run per paper, on the model configured for it (GPT-6.1 Sol today). Within
the run it works in phases of its own choosing (read the paper, outline, write, look, revise), in the session it has
today, with these additions:

- **A way to see.** A `render` tool returns screenshots of a block, a section or the whole read, at desktop and phone
  width, drawn by the web app's own Deep read components, so what the agent sees is what the owner sees. It uses them
  to fix figure sizing, diagram layout, table width and wording that only shows when rendered. It can also look at
  the paper's figures and PDF pages.
- **Checks in code.** `submit` keeps validating the document against the schema, and adds checks a model need not
  judge: every number in the read appears in the paper's text, every figure and table cited exists, every claim verdict
  and CARD note carries a location in the paper. A failed check goes back to the agent as today's schema errors do.
  The same checks are available to it before it submits.
- **A reviewer.** Once a submit passes the checks, a separate call on the same model reviews the read against the
  paper and the rubric below and returns its verdict. If it does not pass, its findings go back to the writer, which
  revises. The first write does not count; at most two revisions follow. The reviewer's findings come last in its
  answer, most important first. The writer triages them: it fixes the real problems and leaves the nitpicks, and does
  not grow the read to answer every small point (models flooded with nitpicks lose the real fixes in them, in prose
  as in code). A read that still fails after the second revision is kept, with a process note that names the failing
  scores, so it shows on Needs you. The reviews, scores and findings are kept with the attempt and read through
  `litwatch deep trace`; the web app shows nothing more.

**The rubric.** Scores from 1 to 5, each with findings that point at blocks:

- **Faithfulness**: numbers, claims and setups match the paper; nothing misread or invented.
- **Clarity**: a lab member could follow the method without the paper; terms defined before use; no bloated sentences.
- **Depth**: method and results come close to the paper's detail; the read goes past the Brief rather than restating
  it.
- **Calibration**: verdicts and CARD notes are fair to the paper, neither credulous nor contrarian for show.

and pass/fail checks on the structure the writer's prompt already asks for: the anchors in their places; findings as
experiments; the paper's reasons in WHY boxes; every CARD note and verdict citing a place; an objective voice (no "for
you", no hype, as everywhere in litwatch's prose); a length in proportion to the paper. A read passes when every score
is 4 or above and every check passes.

**The writer's standards** (its prompt): everything above under "What a Deep read is" and "The shape of a read";
specific methods, numbers and model names; no restating of the Brief beyond At a glance; every statement about the
paper from its text; prior work and code checked, not assumed.

## Beyond the web app

- **Notion export.** The Deep read callouts become a short form that stands on its own: the key claim and points, the
  claims with their verdicts, and the limitations, with the arXiv link the page already has. No link to the web app,
  which only the owner can open. The extension callouts (decision 74's Faithfulness and Monitoring) go. The export's
  reader is told the shape changed.
- **Zotero.** The item's note keeps the Gist and Brief and drops the Deep card. The item itself stays.
- **MCP.** `get_paper` gives the read's At a glance and its list of sections with one line each; a section's full text
  is read on request, as the paper's text is through its outline. Tables come as text, figures and diagrams as their
  captions and explanations.

## Existing cards

The 133 Deep cards written so far stay as they are and keep rendering as today. None are remade in bulk; the owner
names the ones to remake, through "Write again" or an agent, so no remake path is added to the UI.

## Out of scope

- Interactive charts, animations and step-by-step drawings beyond tooltips and highlights.
- Remaking old cards in bulk, or any remake flow besides "Write again".
- A budget or cap on Deep reads: they run on the `deep` service's own subscription (decision 58).
- A reviewer on a different model than the writer.
- Changing which papers get a read.

## Left to the build plan

How figure images are stored and served; how the Deep read components are shared between `web/` and `deep/` and how
the `deep` image runs a headless browser for `render`; the block schema in full; the automatic layout library (ELK is
the candidate); confirming end to end that the model accepts the screenshots (pi-ai declares image input for
`gpt-6.1-sol` on the subscription route).
