/**
 * The contracts the deep service shares with litwatch (docs/plans/a2-work-gate.md, D2): what a job is sent
 * (`POST /process`), the answer to a submission, the job as `GET /jobs/<id>` reports it, and the names of a read that
 * litwatch reads its stored reads by. These schemas are the one source of each: the service checks a submission
 * against `JobInputSchema`, and `npm run contracts` exports every schema in `CONTRACTS` to
 * `contracts/deep.schema.json`, from which litwatch's models are generated (`scripts/contracts.sh` at the repository's
 * root regenerates everything). A new contract is one schema here, added to `CONTRACTS`, and one regenerate.
 *
 * The read itself is `read.ts`'s: litwatch keeps it as JSON and checks only its format tag, so a job's `result` is
 * declared here by that tag alone.
 */

import { Type, type Static, type TSchema } from "typebox";
import { FIGURE_NAME, LIMITATION_BY, READ_NOTE_KINDS, SECTION_KINDS } from "./read.ts";
import { enumOf, RELATIONS } from "./schema.ts";

const nullable = <T extends TSchema>(schema: T) => Type.Optional(Type.Union([schema, Type.Null()]));

// ── what a job is sent ──────────────────────────────────────────────────────────────────────────────

export const SectionSchema = Type.Object(
  {
    title: Type.String({ description: "The heading's title as the source writes it, comments dropped and spaces collapsed." }),
    depth: Type.Integer({ minimum: 0, description: "Its depth below the source's top level of headings, from 0." }),
    number: Type.Optional(Type.String({ description: 'Its number where LaTeX\'s defaults give it ("3.2", "A.1"); left out where the source numbers its sections itself.' })),
    start: Type.Integer({ minimum: 0, description: "Where the heading starts in the paper's text." }),
    end: Type.Integer({ minimum: 0, description: "Where its section ends: at the next heading of its depth or above." }),
  },
  { additionalProperties: false, description: "One heading of a LaTeX source: a sectioning command, the abstract or the bibliography." },
);

export const OutlineSchema = Type.Object(
  {
    sections: Type.Array(SectionSchema, { description: "The headings in the order of the text." }),
    references: Type.Optional(
      Type.Object(
        { start: Type.Integer({ minimum: 0 }), end: Type.Integer({ minimum: 0 }) },
        { additionalProperties: false, description: "The bibliography's environment, from its \\begin to its \\end." },
      ),
    ),
    appendix: Type.Optional(Type.Integer({ minimum: 0, description: "Where the appendix starts: \\appendix, or a section named for it. It runs to the end." })),
  },
  {
    additionalProperties: false,
    description: "A LaTeX source's outline, as litwatch parses it (src/litwatch/core/outline.py); the service reads a LaTeX paper by it.",
  },
);

export const PaperSchema = Type.Object(
  {
    id: Type.String({ minLength: 1 }),
    title: Type.String({ minLength: 1 }),
    abstract: Type.String(),
    authors: Type.Array(Type.String()),
    published: nullable(Type.String()),
    url: nullable(Type.String()),
    body: Type.String({ description: "The paper's full text, as the archive gives it." }),
    body_source: nullable(enumOf(["latex", "pdf"] as const, "Where the text comes from: the paper's LaTeX source, or its PDF.")),
    body_version: nullable(Type.Integer({ minimum: 1, description: "The arXiv version the text is of, which the figures are taken at; null when litwatch cannot say." })),
    outline: Type.Optional(OutlineSchema),
  },
  { additionalProperties: false, description: "The paper. A LaTeX source comes with its outline, and nothing else does." },
);

export const SelectionSchema = Type.Object(
  {
    name: Type.String({ minLength: 1 }),
    slug: nullable(Type.String()),
    subject: Type.String(),
    rubric: nullable(Type.String()),
    grade: nullable(enumOf(["must_read", "relevant", "adjacent"] as const, "The grade litwatch gave the paper in the topic.")),
  },
  { additionalProperties: false, description: "A topic the read is for: why the lab follows it, what it must read, and the paper's grade there." },
);

export const JobInputSchema = Type.Object(
  {
    paper: PaperSchema,
    extensions: Type.Optional(Type.Array(Type.String(), { description: "The lens questions asked for, by name (prompt.ts, LENS_QUESTIONS)." })),
    selections: Type.Optional(Type.Array(SelectionSchema, { description: "The topics the read is written for." })),
  },
  { additionalProperties: false, description: "What POST /process is sent: one paper to read." },
);

// ── what a job reports ──────────────────────────────────────────────────────────────────────────────

export const JOB_STATUSES = ["queued", "running", "done", "error"] as const;
const JobStatus = enumOf(JOB_STATUSES, "Where the job is.");

export const JobRefSchema = Type.Object(
  { id: Type.String(), status: JobStatus },
  { description: "A job as a submission's answer and GET /keys/<key> name it." },
);

const count = Type.Integer({ minimum: 0 });
export const StatsSchema = Type.Object(
  { cells: count, turns: count, input: count, output: count, reasoning: count, cache_read: count, list_cost: Type.Number({ minimum: 0 }), seconds: count },
  { description: "A job's usage: REPL cells, model turns, tokens, the cost at list prices in dollars, and its seconds." },
);

export const FigureSchema = Type.Object(
  {
    name: Type.String({ pattern: FIGURE_NAME, description: "The name the read's fig blocks cite it by." }),
    mime: Type.Literal("image/webp"),
    data: Type.String({ description: "The image, base64." }),
  },
  { description: "One of the read's figures." },
);

export const FAILURES = ["service", "paper"] as const;

export const ResultSchema = Type.Object(
  { format: Type.Literal("read") },
  { additionalProperties: true, description: "The Deep read (read.ts; its JSON Schema is web/src/deepread/read.schema.json), told by its format tag." },
);

export const JobSchema = Type.Object(
  {
    id: Type.String(),
    paper_id: Type.String(),
    title: Type.String(),
    extensions: Type.Array(Type.String()),
    status: JobStatus,
    created_at: Type.String(),
    started_at: Type.Optional(Type.String()),
    finished_at: Type.Optional(Type.String()),
    model: Type.Optional(Type.String()),
    stats: StatsSchema,
    last_event: Type.Optional(Type.String()),
    last_activity: Type.String(),
    result: Type.Optional(ResultSchema),
    figures: Type.Optional(Type.Array(FigureSchema, { description: "The figures the read shows; absent when it shows none." })),
    reviews: nullable(Type.Array(Type.Unknown(), { description: "The review rounds in order (review.ts); null when no review gave a verdict." })),
    trace: Type.Optional(Type.Array(Type.Unknown(), { description: "What the job did, event by event (agent.ts, TraceEvent)." })),
    error: Type.Optional(Type.String()),
    failure: Type.Optional(enumOf(FAILURES, "Whose failure an error job's was: the service's, or the paper's.")),
    key: Type.Optional(Type.String()),
  },
  { description: "A job as GET /jobs/<id> reports it; a finished one carries what litwatch stores of it." },
);

// ── a read's names ──────────────────────────────────────────────────────────────────────────────────

const SectionKind = enumOf(SECTION_KINDS, "A read section's kind.");
const NoteKind = enumOf(READ_NOTE_KINDS, "A read's process note's kind.");
const Relation = enumOf(RELATIONS, "How a paper stands to one of the collection's.");
const LimitationBy = enumOf(LIMITATION_BY, "Whose a limitation is: the paper's own, or the read's.");

/** Every contract, by the name its definition has in `contracts/deep.schema.json`. A schema nested in another that
 * is one of these is exported as a reference to it. */
export const CONTRACTS: Record<string, TSchema> = {
  JobInput: JobInputSchema,
  Paper: PaperSchema,
  Outline: OutlineSchema,
  Section: SectionSchema,
  Selection: SelectionSchema,
  JobRef: JobRefSchema,
  Job: JobSchema,
  Stats: StatsSchema,
  Figure: FigureSchema,
  Result: ResultSchema,
  SectionKind,
  NoteKind,
  Relation,
  LimitationBy,
};

export type JobInput = Static<typeof JobInputSchema>;
export type PaperInput = Static<typeof PaperSchema>;
export type Outline = Static<typeof OutlineSchema>;
export type SelectionContext = Static<typeof SelectionSchema>;
export type Stats = Static<typeof StatsSchema>;
export type ReturnedFigure = Static<typeof FigureSchema>;
export type Failure = (typeof FAILURES)[number];
export type JobStatus = (typeof JOB_STATUSES)[number];
