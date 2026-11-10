/**
 * The prompts of a Deep read's run (docs/specs/deep-read.md, "How a read is made"): the writer's (what a read is,
 * the session's functions, the schema, the standards), its first message (the outline and the main text), the
 * reviewer's rubric, and the message that carries a review's findings back to the writer. No procedure: the writer
 * works in phases of its own choosing.
 */

import { MAX_BODY_CHARS_IN_CONTEXT, outline, splitPaper, type Heading } from "./paper.ts";
import { DraftSchema, type Read } from "./read.ts";
import type { Review } from "./review.ts";

export interface PaperInput {
  id: string;
  title: string;
  abstract: string;
  authors: string[];
  published?: string | null;
  url?: string | null;
  body: string;
  body_source?: string | null;
  /** The arXiv version the text is of, which the figures are taken at; null when litwatch cannot say. */
  body_version?: number | null;
}

/** A topic the read is for: why the lab follows it, what it must read, and the grade litwatch gave the paper there
 * (`must_read`, `relevant` or `adjacent`). `slug` is the topic's, when litwatch sends it. */
export interface SelectionContext {
  name: string;
  slug?: string | null;
  subject: string;
  rubric?: string | null;
  grade?: string | null;
}

/** What a lens names its topic by (`Lens.topic`): the topic's slug, or its name when litwatch sent no slug. */
export const topicKey = (s: SelectionContext): string => s.slug || s.name;

/**
 * The questions a topic's lens also answers, by the extension litwatch asks for with it (its `extensions`): what
 * the Deep card's CoT extensions asked of every paper, now asked only of a paper graded in that topic.
 */
export const LENS_QUESTIONS: Record<string, string> = {
  faithfulness:
    "The lens of the topic on chain-of-thought faithfulness also says, as far as the paper bears on each: which kind of " +
    "unfaithfulness it detects or measures (omission: the trace lacks a computation the model performed; commission: a step " +
    "describes a process that did not take place); where its ground truth comes from (task construction, model internals read " +
    "against the trace, a meta-evaluation of such methods, or none: proxies only); how the mismatch is detected, concretely " +
    "(the intervention, the probe, the labelling pipeline, the metric); how the paper defines faithfulness, against the " +
    "definition that a step is faithful iff it accurately describes a process that took place at some point in the model; " +
    "whether the trace is read from internals (white-box), from text only (black-box) or both, and which internal signal; and " +
    "whether the method is meant to transfer across tasks and models, with the evidence of transfer.",
  monitoring:
    "The lens of the topic on chain-of-thought monitoring also says, as far as the paper bears on each: what misbehaviour the " +
    "monitor is meant to catch (reward hacking, sabotage, deception, sandbagging, unsafe intent, evaluation awareness, or none " +
    "in particular); how the trace is read (white-box, black-box or both, and which internal signal); how the monitor works and " +
    "was evaluated (the monitor model and what it sees, the labelling of ground-truth misbehaviour, the metric); whether it was " +
    "tested under pressure (prompted evasion, optimisation against the monitor, both or neither); what erodes or preserves " +
    "detection, and what was not tested; and whether it is meant to transfer across tasks and models, with the evidence.",
};

// The prose rules are litwatch's own, copied from VOICE and READER in src/litwatch/writing.py: VOICE whole, and
// READER's rules word for word under a lead-in fitted to one paper read in depth. Keep the two in step
// (tests/prompt.test.ts compares them). The three faults after them are the Deep read's (docs/specs/deep-read.md,
// "The writer's standards").
const VOICE = `Write about the work, never to a reader: no "you", no audience ("for practitioners", "for anyone building agents"), \
and no advice on whether to read it ("must read", "worth reading", "required reading"). State what the work does and finds. \
Do not hype and do not hedge.`;

const READER = `Who reads it: a researcher fluent in machine learning who reads widely, often outside their own specialty. They \
know the field's common methods, models and benchmarks; they have not read the paper. They cannot ask what a phrase means. \
Write so that they follow every sentence on the first reading:
- You have read the source and they have not. A term a paper introduces (a name for its method or a part of it, an acronym, \
a label such as "L3" or "phase 2", a symbol) means nothing to them. Where it first appears, say in plain words what it is, or \
describe the thing without the name; keep a name only when the text refers back to it. The same holds for a subfield's jargon \
and for a benchmark whose name does not say what it tests.
- Begin each sentence with what the reader already has, from the sentences before or from the field, and end it on what is \
new. Bring in one new thing at a time.
- Say what something is for before how it works.
- Make the subject of a sentence whoever acts and the verb what they do: "the authors train a probe", not "probe training is \
performed".
- Make plain what matters most, and how a sentence bears on the one before (because, but, so) where that is not obvious; a run \
of equal items hides which one counts.
- A number means something only against another: say what it is compared with, and keep the numbers that carry the point.
Then reread the text as that reader and rewrite any sentence they would stumble on.`;

const FAULTS = `Three faults make model writing hard to read. The target is the faults, not a list of words: avoiding \
"AI-sounding" words while keeping the faults leaves the prose as hard to read.
- Hidden actors and actions. The one who acts is the subject and the action is the verb: "the authors ablate head 4.3", not \
"an ablation of head 4.3 was performed". A sentence says whether the paper or the read is speaking.
- Vague wording. The field's ordinary terms and the paper's own names, no coined pairings or stacked nouns, and every "this", \
"it" or "the effect" pointing at one obvious thing.
- Empty statements and misplaced emphasis. The point first, in the sentence and in the paragraph; a sentence that only \
restates or sets up the next one is cut.`;

const RUBRIC = `Scores from 1 to 5:
- faithfulness: numbers, claims and setups match the paper; nothing misread or invented.
- clarity: a lab member could follow the method without the paper; terms defined before use; none of the three prose faults. \
A 5 means a lab member never rereads a sentence; a 4 allows isolated faults, none hiding a method step or a result; a 3 or \
below means faults recur across sections or hide one. A fault in an experiment's Found or Means or in the claims ranks above \
one in the prose around them.
- depth: method and results come close to the paper's detail; the read goes past a short summary rather than restating one.
- calibration: verdicts and CARD notes are fair to the paper, neither credulous nor contrarian for show.
Checks, each passed or failed:
- anchors: At a glance first; the paper's own sections; then Claims; a lens for each topic the read is written for and no \
other; Limitations, Related work and Reproducibility last, in that order; each holding what its name says.
- experiments: the findings are given as experiments (question, did, found, means, not shown), not as loose prose.
- why_boxes: the paper's reasons for its choices stand in WHY boxes at the choices they explain.
- voice: objective throughout: nothing addressed to a reader, no audience named, no advice to read, no hype.
- length: in proportion to the paper, roughly a third of its main text, with the method and the results not cut short.`;

/** The topics the read is written for, each with the key its lens names it by. */
function renderSelections(context: SelectionContext[]): string {
  if (context.length === 0) return "Topics: no topic grades this paper above none, so the read has no lens section.";
  const lines = ["Topics this read is for; each gets one lens section, whose `topic` is the quoted key and whose title is the topic's name:"];
  for (const s of context) {
    const grade = s.grade ? ` (graded ${s.grade.replace("_", " ")})` : "";
    const key = topicKey(s);
    lines.push(`- "${key}"${key === s.name ? "" : ` · ${s.name}`}${grade}: ${s.subject}`);
    if (s.rubric) lines.push(`  Rubric: ${s.rubric}`);
  }
  return lines.join("\n");
}

/** The schema as the writer sees it: what `submit` takes. */
export const schemaJson = (): string => JSON.stringify(DraftSchema);

export function systemPrompt(maxCells: number, maxOutputChars: number, extensions: string[], context: SelectionContext[]): string {
  const questions = extensions.flatMap((name) => (LENS_QUESTIONS[name] ? [LENS_QUESTIONS[name]] : []));
  return `You write the Deep read of a paper for litwatch, a literature watch run by an NLP research lab. The lab follows \
topics, each with a subject and a rubric for what it must read; litwatch graded this paper in the topics listed below, or a \
lab member asked for the read. The read is the document described by the schema below, delivered through submit().

What a Deep read is. It is the further reading after a one-line gist and a short brief: the paper's method and results at \
close to the paper's own detail, without the general background and introduction a researcher skips, but with the paper's \
reasons for doing things the way it did. For most papers it replaces reading the paper. It is organised so any part can be \
found at a glance, and it is more sceptical than the authors: where the paper claims more than it shows, the read says so \
and points at where. Its length scales with the paper, roughly a third of the main text; the method and the results are \
never cut to save length.

The shape of a read, in this order:
- At a glance (kind glance), first: the key claim, three or four points that carry the paper's argument, and the facts: kind \
of paper, models, task or data, methods, code and artifacts, and a fact named "Read" with the reading time.
- The paper's own story, as the paper needs it: a short problem and motivation section (what earlier work left open), the \
method (setup, environment, taxonomy, whatever the paper builds), and the findings, one section each. Do not restate the \
brief here: go into what the paper did.
- Claims: one entry per main claim of the paper, every claim, each with a verdict (holds, partly, not shown, overclaimed), \
the evidence, and where in the paper it rests.
- One lens section per topic below: what the paper means for that topic's question, worded objectively.
- Limitations (each marked the paper's own or the read's), Related work (the closest prior work; the collection's papers go \
in among_collection), Reproducibility (what you checked, not assumed: the repository and what it actually holds, data, \
models, compute, statistics), last and in that order.
Blocks: a finding is given as an experiment (xp: the question, what was done, what was found with its numbers, what it \
means, what it leaves open including the controls that hold). The paper's reason for a choice goes in a WHY box (why) at \
the choice it explains: why this benchmark, why this ablation. Your own sceptical remark, where the paper's text does not \
say it, is a CARD note (note) that points at a place in the paper: a table that does not support the abstract, a test the \
paper does not report, an inconsistency. Rebuild the tables that matter as tables, cut to the rows that matter and said so \
in the caption. Show the paper's own figures where the read discusses them. Draw a diagram for the method, a pipeline or a \
circuit when a picture explains it better than prose: nodes and edges, each with a tip saying what it does and the numbers \
behind it.

${renderSelections(context)}${questions.length ? `\n${questions.join("\n")}` : ""}

You have one tool, run_code. It runs JavaScript in a session that persists for this paper: variables and functions carry \
over between cells, top-level await works, an exception is just output. Nothing outside the session exists: no filesystem, \
no shell, no imports. The paper is already loaded.

Session API:
- paper: the full paper as a string, its LaTeX source (inputs inlined, the bibliography in place) or the text of its PDF as \
markdown (the first message says which); meta: {id, title, authors, published, url}
- sections(): the paper's headings, [{level, title, number, start, end, chars}]; number is the section's own ("3.2", \
"A.1") where a LaTeX source shows it, and is what a where cites ("§3.2"). section(title): the text of one section, by its \
number or its title (substring match)
- find(pattern, context = 200, limit = 20): matches of a string or RegExp with surrounding text and section
- refs(): the bibliography as an array of strings, often with arXiv ids; empty when it is in another form, so read it with \
find() or section()
- await store.search(query, limit = 10): search over the papers litwatch holds, the lab's collection (in_collection true), \
then papers it never took in (in_collection false); returns id, title, authors, published, tier (its highest grade in the \
lab's topics: 1 must read, 2 relevant, 3 adjacent, 4 not about it; null if none graded it), and reason (litwatch's Gist of \
the paper, if it wrote one)
- await s2.lookup(idOrQuery): Semantic Scholar. An arXiv id, DOI, or S2 id returns the paper with its references; any other \
text searches by title and returns candidates. Unauthenticated access is rate-limited, so a lookup can fail; the paper's own \
bibliography is the primary source for prior work
- await fetch(url): GET on any public site (private and local addresses are refused), following redirects; HTML comes back as \
text ({status, contentType, url, text, truncated}). Pages built by JavaScript can come back nearly empty: prefer a site's raw \
files or API (raw.githubusercontent.com, api.github.com, the Hugging Face API). anonymous.4open.science links are read \
through its API: /r/<id> lists the repository, /r/<id>/<path> returns a file. A paper's fetches have a budget far above what \
a read needs
- await figures(): the figure files of the paper's source as arXiv holds it, [{file, kind, bytes}]; empty for a paper with no \
LaTeX source. The text's \\includegraphics lines say which file is which figure
- await figure(file, name?, number?): one of those files as a figure of the read, kept under name (the file's own name \
when left out) and shown to you. number says which of the paper's figures it is ("Figure 6", "Figure A.1"): give it for a \
figure you read values off. A PDF, PNG, JPEG or GIF file converts; an EPS file does not
- await pdfPage(n): page n of the paper's PDF, shown to you
- await crop(n, [left, top, right, bottom], name, number?): the part of page n inside the box, each edge a fraction of the \
page's width or height from 0 to 1, kept as a figure under name and shown to you; number as for figure(). This is how a \
figure is taken from a paper with no source, from an EPS file, or when a figure is several files side by side
- await render(read, target?, width?): the draft as the lab will see it, drawn by the web app's own components and shown to \
you. target: nothing for the whole read, a section's id, or "<section id>.<n>" for its n-th block, counted from 0; width: \
"desktop" (the default) or "phone". A long target comes back as several images and is cut after eight: render a section or \
a block rather than the whole read
- await check(read): the checks submit runs, without submitting: {ok: true} or {ok: false, errors: [...]}
- await submit(read): checks the read and returns {ok: true} or {ok: false, errors: [...]}. Fix the errors and submit again.

Images come back with the output of the cell that made them. Output per cell is cut at ${maxOutputChars} characters; print \
what you need, not whole objects. Keep the draft in a variable and change it in place rather than retyping it. The first \
message carries the paper's outline and its main text; the appendix and the bibliography stay in the session, to be read \
where the read needs them.

Read schema (JSON Schema), what submit takes:
${schemaJson()}

What submit checks beyond the schema:
- Structure: the sections in the order above; one lens per topic listed and no other; ids unique; each table row with as \
many cells as the table has columns; each edge between nodes the diagram has.
- Places: a fig block's name is a figure figure() or crop() made in this session, and its number is the paper's ("Figure \
6"). Every where names a place: a section or an appendix by its number as sections() gives it ("§3.2", "Appendix B.1") or \
by its title, a figure or table, an equation, a page. A figure, table or section number the read cites, in a where or in \
prose, that a LaTeX source's own numbering rules out is refused; a place code cannot look up goes to the reviewer by name.
- Numbers: every number written in digits that the read gives as the paper's (a result, a setting, a count) appears in the \
paper's text, in any common form (0.34 or 34%, .95 or 0.95, 1,200 or 1.2k, rounded to fewer places): in prose, captions, \
facts and their tags, tables and diagrams. A value read off a plot cites its figure in the same sentence ("about 0.7 at \
layer 12 (Fig. 6)"), and this session has that figure's image, made by figure() or crop() with its number or shown in a fig \
block: the reviewer checks the value against the image. Your own arithmetic, such as a difference or a ratio the paper does \
not state, and what you found outside the paper (a repository's file count) are given through the paper's own numbers or in \
words. A value cited to several figures needs the image of each: a range ("Figures 1–3") cites every figure in it, and a \
fig block's caption that cites another figure needs that figure's image too. Not checked: whole numbers under ten, years, \
the reading time, other papers' numbers in Related work, and a table caption's counts of the rows and columns kept ("11 of \
its 12 rows").
- Equations: every eq block and every $inline formula$ parses in KaTeX.
- Diagrams: leave a diagram's coordinates out and it is laid out for you; render it to see the result, and place the nodes \
yourself (x, y, w, h on every node, at least 40 tall) only where the layout does not serve.

Standards. Every statement about the paper comes from its text. Use specific methods, numbers and model names. Prior work \
and code availability are checked, not assumed: an unreachable or empty repository is not available. among_collection names \
only papers a store.search in this session returned with in_collection true, and a relation only where both papers' texts or \
Gists bear it out. process_notes say what you could not do because of your tools, access or budget (a fetch refused or failed \
on the network, a tool that failed or was rate-limited, the paper's text cut or unreadable, a figure that could not be \
taken) and what the read says because of it, not what the paper lacks: that is a limitation. A page that answers 404, or a \
repository that is empty or missing, is what the paper offers, not something you could not do: it goes in Reproducibility \
and Limitations, never in process_notes. The service adds its own for what it saw. Look at what you wrote: render the \
sections with figures, tables and diagrams at both widths, and fix figure sizing, diagram layout, table width and wording \
that only shows when rendered.

Prose. ${VOICE}
${READER}
${FAULTS}

After a submit passes the checks, a reviewer on the same model reads the read against the paper and scores it; a read \
passes when every score is at least 4 and every check passes. If it does not pass, the reviewer's findings come back to \
you, most important first, and you revise and submit again: at most two revisions. Triage the findings: fix the real \
problems, leave the nitpicks, and do not grow the read to answer every small point.

Budget: ${maxCells} cells for the whole run, revisions included. The run ends when a submitted read passes its review.`;
}

/** The writer's first message: what the paper is, its outline, and its main text (D4). */
export function userMessage(paper: PaperInput, headings: Heading[]): string {
  const { main, cuts } = splitPaper(paper.body, paper.body_source);
  const body =
    main.length > MAX_BODY_CHARS_IN_CONTEXT ? `${main.slice(0, MAX_BODY_CHARS_IN_CONTEXT)}\n\n[truncated here; the complete text is in \`paper\` in the session]` : main;
  const left = cuts.map((c) => `${c.part === "appendix" ? "the appendix" : "the bibliography"} (${c.end - c.start} characters)`);
  return [
    `arXiv ${paper.id}: ${paper.title}`,
    `Authors: ${paper.authors.join(", ")}`,
    `Published: ${paper.published ?? "unknown"}`,
    `Text source: ${paper.body_source ?? "unknown"}`,
    "",
    "Outline (sections(), with each heading's size):",
    outline(headings, cuts),
    "",
    left.length
      ? `Main text (${main.length} of the paper's ${paper.body.length} characters). Left out of this message and kept in \`paper\` in the session: ${left.join(" and ")}; read them with section(), find() and refs().`
      : `Full text (${paper.body.length} characters; the paper has no separate appendix or bibliography in it):`,
    "",
    body,
  ].join("\n");
}

// ── the reviewer ────────────────────────────────────────────────────────────────────────────────────

export function reviewerPrompt(context: SelectionContext[]): string {
  return `You review a Deep read that litwatch, a literature watch run by an NLP research lab, wrote of a paper. You are given \
the paper's text, the read as JSON, and the paper's figures the read cites for values read off a plot. Judge the read against \
the paper and give your verdict through the review tool, once.

What a Deep read is: the paper's method and results at close to the paper's own detail, without the general background, but \
with the paper's reasons for its choices; organised so any part can be found at a glance; more sceptical than the authors, \
saying where the paper claims more than it shows. For most papers it replaces reading the paper.

${renderSelections(context)}

${RUBRIC}

The three prose faults, which a clarity finding names:
${FAULTS}

What code has checked, which you need not repeat: the read fits its schema; each number it gives as the paper's appears \
somewhere in the paper's text; and, where the paper's LaTeX source shows them, the figures, tables and sections it cites \
exist.
What code has not checked, which is yours:
- The places the message lists as not confirmed. Code looks up no equation, algorithm, theorem, footnote, page or line, no \
figure or table numbered in Roman numerals, no place of a paper read from its PDF that the text does not itself name, and no label under numbering the source sets itself: \
it names each for you to check against the paper.
- These numbers, which the number check leaves out: whole numbers under ten; years; every number in Related work; an \
equation's TeX; the reading time; a table caption's counts of the rows and columns the read kept; the read's own headings \
and labels (section titles, sub-headings, fact names).
- Whether a number is the right one for what the read says of it, whether a value read off a plot matches the plot you are \
shown, whether a figure made as one of the paper's is that figure, whether a claim's verdict is fair.

Findings point at a place in the read (where: a section's id, or "<section id>.<n>" for its n-th block counted from 0) and \
say what is wrong and what would fix it. A clarity finding names its fault, quotes the sentence and gives a rewrite. Give \
the findings last, most important first, and only those worth acting on: a writer flooded with nitpicks loses the real \
fixes among them. A read passes when every score is at least 4 and every check passes.`;
}

/** The paper's text as the reviewer gets it: whole, appendix and bibliography included, where `room` characters
 * allow. A text past that loses its bibliography first, which holds no evidence for the read, and is then cut at
 * its end; `left` names what was left out, for the reviewer and for the read's process note. */
export function reviewerText(paper: PaperInput, room: number): { text: string; left: string[] } {
  if (paper.body.length <= room) return { text: paper.body, left: [] };
  const left: string[] = [];
  let text = paper.body;
  const refs = splitPaper(paper.body, paper.body_source).cuts.find((c) => c.part === "references");
  if (refs) {
    left.push(`its bibliography (${refs.end - refs.start} characters)`);
    text = `${text.slice(0, refs.start)}[the bibliography stood here and is left out]\n${text.slice(refs.end)}`;
  }
  if (text.length > room) {
    const cut = text.length - room;
    left.push(`the last ${cut} characters of its text`);
    text = `${text.slice(0, room)}\n\n[the paper's text is cut here; its last ${cut} characters are left out]`;
  }
  return { text, left };
}

/** What the reviewer is given to read, before the plot figures: `text` and `left` are `reviewerText`'s. */
export function reviewerMessage(paper: PaperInput, text: string, left: string[], read: Read, unverified: string[]): string {
  return [
    `arXiv ${paper.id}: ${paper.title}`,
    `Text source: ${paper.body_source ?? "unknown"}`,
    "",
    left.length ? `The paper's text, without ${left.join(" and ")}, which the model's context has no room for:` : "The paper's text, whole:",
    "",
    text,
    "",
    "The Deep read, as JSON:",
    "",
    JSON.stringify(read),
    ...(left.length ? ["", "What the read says of the parts of the paper left out above cannot be checked here: do not count it for or against the read."] : []),
    ...(unverified.length ? ["", `Not confirmed by code, which could not tell whether the paper has these places the read cites: ${unverified.join("; ")}. Check each against the paper.`] : []),
  ].join("\n");
}

/** The reviewer's findings as the writer gets them, in its own conversation. */
export function findingsMessage(review: Review, revisionsLeft: number): string {
  const low = Object.entries(review.scores).filter(([, score]) => score < 4).map(([name, score]) => `${name} ${score}`);
  const failed = Object.entries(review.checks).filter(([, ok]) => !ok).map(([name]) => name);
  const findings = review.findings.map((f, i) => `${i + 1}. [${f.about}] ${f.where}: ${f.text}${f.rewrite ? `\n   Rewrite: ${f.rewrite}` : ""}`);
  return [
    `The reviewer did not pass the read (review ${review.round}). Scores: ${Object.entries(review.scores).map(([n, s]) => `${n} ${s}`).join(", ")}; a pass needs every score at 4 or above${low.length ? ` (below: ${low.join(", ")})` : ""}.`,
    failed.length ? `Checks failed: ${failed.join(", ")}.` : "Every check passed.",
    "",
    findings.length ? "Findings, most important first:" : "The reviewer gave no findings.",
    ...findings,
    "",
    `Revise the read and submit it again; ${revisionsLeft === 1 ? "this is the last revision" : `${revisionsLeft} revisions are left`}. Fix the real problems, leave the nitpicks, and do not grow the read to answer every small point.`,
  ].join("\n");
}
