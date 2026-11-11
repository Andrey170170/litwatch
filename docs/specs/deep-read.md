# Deep read

Status (2026-11-11): **built through P5, not deployed**; the build plan is [`plans/deep-read.md`](../plans/deep-read.md).
Amended on 2026-11-11 so a read can be followed without the paper (its reader, story before detail, the blind
reader); P6 builds that, and automatic reads switch on with it. This replaces the Deep card (`deep` integration, [v1
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
reading: the paper's method and results at close to the paper's own detail, with the paper's reasons for doing things
the way it did. It is organised so any part can be found at a glance, and it is more sceptical than the authors: where
the paper claims more than it shows, the read says so and points at where.

**Its reader** knows the field broadly (language models, interpretability, the usual methods) and has not read the
paper. The read leaves out general background, and introduces what this paper needs: its question, its idea, its own
terms, and any specialised concept it goes deep into. A read is a story before it is a reference. The owner's read of
the first real reads (`evals/2026-11-10-deep-reads.md`) found them right for someone who had read the paper and hard
to follow for someone who had not: true facts one after another, in the paper's order, with nothing to say why the
reader needed each. So the read follows the paper's argument in the order a newcomer understands it, and every
section says what it is about before its details. A reader who knows the paper skims to the details, which are all
still there.

For most papers the read replaces reading the paper. The owner opens the paper itself only in the few cases that
need every detail exactly, so the read accepts some LLM looseness of wording in exchange for detail and coverage. Its
length scales with the paper, roughly a third of the main text, and the method and results are never cut to save
length; it comes from detail, never from a caveat restated. Code measures it as the read's text against the paper's main
text (without the appendix or bibliography; a LaTeX source from its first heading), and a read past 60% fails its length check: the
first five real reads, at 32–46%, were the right length to the owner (`evals/2026-11-10-deep-reads.md`). R1's two
reads (about 15 and 12 minutes) are the target.

## The shape of a read

**Sections.** A read is a sequence of sections. A few anchors sit in fixed places on every read; the middle follows
the paper's argument in the order a newcomer understands it, which need not be the order of the paper's sections.

- First: **At a glance**. The key claim, then the paper's arc in three or four points (the question, the idea, how it
  is tested, what it found), and the facts (kind, models, task or data, methods, code and artifacts, reading time).
- The middle, as the paper needs: a short **problem and motivation** section (what earlier work left open), the
  **method** (setup, environment, taxonomy, whatever the paper builds), and the **findings**, one section each. A
  method paper might go Problem, Method, Experiments; a benchmark Construction, Results, What it measures; an analysis
  Question, then one section per finding. Each section opens with a few sentences on what it covers and where it sits
  in the arc; its details follow in the order that story needs them. Settings and exceptions (a list of models, a
  setting changed for one model, which runs were left out) go in a table or at the section's end, not at its start.
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
- **WHY box**: the paper's reason for a choice a careful reader would question and the text around it leaves out
  (why this benchmark, why mean ablation, why one model is steered at a different depth). A reason the story needs to
  make sense is said in the text, where it is needed; boxes are few, so the reader stops for each one. A box is shown
  folded: its question in view, its answer opening on a click.
- **CARD note**: the read's own sceptical remark, where the paper's text does not say it: a table that does not
  support the abstract, a test the paper does not report, an inconsistency. Every CARD note points at a place in the
  paper. Notes are kept for remarks that bear on a claim's verdict or on how far a result can be trusted, and stay
  open and highlighted, so the eye goes to them.
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
  only a PDF, cropped from its pages by the agent, which looks at the page.
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
line. The layout is open: every section is laid out in full, nothing folded but a claim's evidence, a WHY box's answer and the process notes.

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
  judge: every number the read gives as the paper's (a result, a setting, a count) appears in the paper's text, the
  read's own figures such as reading time aside, and values read off a plot (cited to its figure) left to the
  reviewer; every figure and table cited exists, every claim verdict
  and CARD note carries a location in the paper. A failed check goes back to the agent as today's schema errors do.
  The same checks are available to it before it submits. Code reads the source and text by rule and can be wrong
  (a table a macro makes, a number spelled its own way), so no refusal of a place or a number is final: the agent may
  stand by an item it has checked, which then goes to the reviewer by name, and the trace keeps what it stood by.
- **A reviewer.** Once a submit passes the checks, a separate call on the same model reviews the read against the
  paper and the rubric below and returns its verdict. If it does not pass, its findings go back to the writer, which
  revises. The first write does not count; at most two revisions follow. The reviewer's findings come last in its
  answer, most important first. The writer triages them: it fixes the real problems and leaves the nitpicks, and does
  not grow the read to answer every small point (models flooded with nitpicks lose the real fixes in them, in prose
  as in code). A read that still fails after the second revision is kept, with a process note that names the failing
  scores and checks, so it shows on Needs you. The reviews, scores and findings are kept with the attempt and read through
  `litwatch deep trace`; the web app shows nothing more.
- **A blind reader.** The reviewer reads with the paper beside it, so it cannot see what a reader without the paper
  misses. A second call on the same model reads the draft without the paper: it gets the read as a reader meets it
  (its text in reading order, WHY boxes' answers, tables and diagram labels included, and the figures it shows as
  images) and the reader described under "What a Deep read is". It retells the paper's argument in a few sentences,
  lists every place it lost the thread (a term before its definition, a detail before its reason, a section whose
  point it could not tell, a reason the story needs that it found only in a folded WHY answer), and scores
  followability. It reads in the same round as the reviewer: their findings go
  to the writer together and it revises once, so a read still has at most two revisions. Its retelling and findings
  are kept in the trace with the reviews, so the owner can set what it understood against the paper.

**The rubric.** Scores from 1 to 5, each with findings that point at blocks:

- **Faithfulness**: numbers, claims and setups match the paper; nothing misread or invented.
- **Clarity**: none of the three prose faults below; terms defined before use. Whether the read can be followed as a
  whole is the blind reader's score, not this one. Each clarity finding names its fault, quotes the sentence and gives
  a rewrite. A 5 means a reader never rereads a sentence; a 4 allows isolated faults, none hiding a method step or a result; a 3 or below
  means faults recur across sections or hide one. A fault in an experiment's *Found* or *Means* or in the claims
  ranks above one in the prose around them.
- **Depth**: method and results come close to the paper's detail; the read goes past the Brief rather than restating
  it.
- **Calibration**: verdicts and CARD notes are fair to the paper, neither credulous nor contrarian for show.
- **Followability**, the blind reader's: a reader who knows the field broadly and has not read the paper follows the
  read's argument. A 5 means it never loses the thread and can retell the argument; a 4 allows isolated
  stumbles, none hiding what a section is for or what a finding shows; a 3 or below means it lost the thread in a
  section or could not tell what the paper found. The blind reader cannot tell whether its retelling matches the
  paper: the read's faithfulness is the reviewer's score, and the owner sets the retelling against the paper in the
  trace.

and pass/fail checks on the structure the writer's prompt already asks for: the anchors in their places; findings as
experiments; WHY boxes and CARD notes only where they are meant (a reason the text leaves out, a remark bearing on a
verdict or on a result's trust), with no quota; every CARD note and verdict citing a place; an objective voice (no "for
you", no hype, as everywhere in litwatch's prose); a length in proportion to the paper, which code sets from its
measure rather than the reviewer's estimate. A read passes when every score, the blind reader's included, is 4 or above and every check passes.

**The writer's standards** (its prompt): everything above under "What a Deep read is" and "The shape of a read",
its reader and story before detail first;
specific methods, numbers and model names; no restating of the Brief beyond At a glance; every statement about the
paper from its text; prior work and code checked, not assumed. And prose free of the three faults that make model
writing hard to read (after Williams, *Style: The Basics of Clarity and Grace*, as measured in
[Zonder Labs, "Why is AI slop so frustrating to read?"](https://zonderlabs.com/slop), 2026):

- **Hidden actors and actions.** The one who acts is the subject and the action is the verb: "the authors ablate
  head 4.3", not "an ablation of head 4.3 was performed". A sentence says whether the paper or the read is speaking.
- **Vague wording.** The field's ordinary terms and the paper's own names, no coined pairings or stacked nouns, and
  every "this", "it" or "the effect" pointing at one obvious thing.
- **Empty statements and misplaced emphasis.** The point first, in the sentence and in the paragraph; a sentence that
  only restates or sets up the next one is cut.

The target is these faults, not a list of words: avoiding "AI-sounding" words while keeping the faults leaves the
prose as hard to read.

## Beyond the web app

- **Notion export.** The Deep read callouts become a short form that stands on its own: the key claim and points, the
  claims with their verdicts, and the limitations, with the arXiv link the page already has. No link to the web app,
  which only the owner can open. The extension callouts (decision 74's Faithfulness and Monitoring) go. A read that
  failed its review says so in a line above the short form. A paper with a Deep card from
  before keeps today's callouts. The export's reader is told the shape changed.
- **Zotero.** The `litwatch:enrichment` note and the card's tags are no longer written; the ones already on items
  stay as they are. The item, its screen note, its grade tags and the inbox collection (which a ready Deep read still
  sets) stay.
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
- Folding a section's details behind its story. If story-first prose is not enough on P6's reads, this comes next.

## Settled by the build plan

[`plans/deep-read.md`](../plans/deep-read.md) settles what this spec left open: figures come from the paper's e-print,
fetched from arXiv by the `deep` service at the version litwatch read (the archive keeps no figures), and are stored
by litwatch with the read; `render` opens a page litwatch serves, built from the web app's own Deep read components;
the schema is TypeBox in `deep/`, exported for the web app; diagrams are laid out with ELK.
