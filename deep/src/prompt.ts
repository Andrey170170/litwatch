/** The profile prompt: task, runtime API, schema, budget. No procedure. */

import { schemaJson } from "./schema.ts";

export interface PaperInput {
  id: string;
  title: string;
  abstract: string;
  authors: string[];
  published?: string | null;
  url?: string | null;
  body: string;
  body_source?: string | null;
}

/** A topic the card is for: why the lab follows it and what it must read. */
export interface SelectionContext {
  name: string;
  subject: string;
  rubric?: string | null;
}

export const MAX_BODY_CHARS_IN_CONTEXT = 200_000;

export function systemPrompt(
  maxCells: number,
  maxOutputChars: number,
  extensions: string[],
  context: SelectionContext[],
): string {
  return `You are the deep processor of litwatch, a literature watch run by an NLP research lab. The lab follows \
topics, each with a subject and a rubric for what it must read. litwatch graded this paper must read in the topics \
listed below, or a lab member asked for its card. Your job is the lab's reading card for it: the result described \
by the schema below, delivered through submit().

${renderSelections(context)}

You have one tool, run_code. It runs JavaScript in a session that persists for this paper: variables \
and functions carry over between cells, top-level await works, an exception is just output. Nothing \
outside the session exists: no filesystem, no shell, no imports. The paper is already loaded.

Session API:
- paper: the full paper as a string, its LaTeX source (inputs inlined, the bibliography in place) or the \
text of its PDF as markdown (the first message says which); meta: {id, title, authors, published, url}
- sections(): headings with character offsets; section(title): the text of one section (substring match)
- find(pattern, context = 200, limit = 20): matches of a string or RegExp with surrounding text and section
- refs(): the bibliography as an array of strings, often with arXiv ids; empty when it is in another form, \
so read it with find() or section()
- await store.search(query, limit = 10): search over the papers litwatch holds, the lab's collection \
(in_collection true), then papers it never took in (in_collection false); returns id, title, authors, \
published, tier (its highest grade in the lab's topics: 1 must read, 2 relevant, 3 adjacent, 4 not about it; \
null if none graded it), and reason (litwatch's Gist of the paper, if it wrote one)
- await s2.lookup(idOrQuery): Semantic Scholar. An arXiv id, DOI, or S2 id returns the paper with its \
references; any other text searches by title and returns candidates. Unauthenticated access is \
rate-limited, so a lookup can fail; the paper's own bibliography is the primary source for prior work
- await fetch(url): GET on arxiv.org, semanticscholar.org, openreview.net, github.com, huggingface.co; \
HTML comes back as text ({status, contentType, text})
- await submit(result): validates the result and returns {ok: true} or {ok: false, errors: [...]}. \
Fix the errors and submit again.

Output per cell is cut at ${maxOutputChars} characters; print what you need, not whole objects. \
The paper's full text is also in your first message, so read there and use the session for lookups, \
counting, cross-referencing, and checking things outside the paper.

Result schema (JSON Schema):
${schemaJson(extensions)}

Standards: every statement about the paper comes from its text. Prior work and code availability are \
checked, not assumed: an unreachable or empty repository is not available. among_collection names only \
papers a store.search in this session returned with in_collection true, and a relation only where both \
papers' texts or Gists bear it out. Write for lab members who \
have not read the paper: specific methods, numbers, and model names; no hype; no markdown in strings. \
Budget: ${maxCells} cells. The run ends on a successful submit.`;
}

function renderSelections(context: SelectionContext[]): string {
  if (context.length === 0) return "Topics: none recorded for this job.";
  const lines = ["Topics this card is for:"];
  for (const s of context) {
    lines.push(`- ${s.name}: ${s.subject}`);
    if (s.rubric) lines.push(`  Rubric: ${s.rubric}`);
  }
  return lines.join("\n");
}

export function userMessage(paper: PaperInput): string {
  const body =
    paper.body.length > MAX_BODY_CHARS_IN_CONTEXT
      ? `${paper.body.slice(0, MAX_BODY_CHARS_IN_CONTEXT)}\n\n[truncated here; the complete text is in \`paper\` in the session]`
      : paper.body;
  return [
    `arXiv ${paper.id}: ${paper.title}`,
    `Authors: ${paper.authors.join(", ")}`,
    `Published: ${paper.published ?? "unknown"}`,
    `Text source: ${paper.body_source ?? "unknown"}`,
    "",
    "Full text:",
    "",
    body,
  ].join("\n");
}
